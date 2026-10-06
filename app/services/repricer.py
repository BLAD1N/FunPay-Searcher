"""Переоценка: пересчёт цены наших лотов при изменении цены исходника и обновление рекомендуемой цены кандидатов.

Цена исходного объявления (``Found.listing.price``) меняется только при поиске (``Storage.upsert_found`` обновляет
запись и пишет историю цен), поэтому ``run()`` имеет смысл вызывать сразу после каждого поиска и дополнительно
по таймеру монитора (на случай, если пользователь поменял правило наценки в профиле).

Поток работы ``run()``:
  1. Наши лоты (active / draft / deactivated): если цена исходника изменилась не меньше чем на
     ``monitor.reprice_min_change_percent`` процентов относительно ``OurLot.source_price`` — считаем новую цену
     по правилу наценки профиля. При ``monitor.auto_reprice`` цена лота обновляется (и отправляется на FunPay для
     активных лотов через ``PublisherService.update``), иначе — только запись в журнал и напоминание в Telegram
     (один раз на каждую новую цену исходника, чтобы не спамить).
     Снятые (deactivated) лоты переоцениваются «тихо»: без запроса к FunPay и без уведомления.
  2. Кандидаты без лота: ``Found.suggested_price`` пересчитывается, если расходится с правилом наценки на 1 ₽ и более.
"""
from __future__ import annotations

import threading
from typing import Optional

from ..models import FoundStatus, LotStatus, OurLot, Profile, utcnow
from ..notify import esc, fmt_money, trim
from ..pricing import calculate_price
from .context import AppContext
from .publisher import PublisherService

# статусы лотов, у которых отслеживаем цену исходника
_TRACKED = (LotStatus.ACTIVE, LotStatus.DRAFT, LotStatus.DEACTIVATED)


def _pct(old: float, new: float) -> str:
    """Относительное изменение со знаком: 15000 -> 13500 => "-10%"; 0 -> x => "—"."""
    try:
        old, new = float(old), float(new)
    except (TypeError, ValueError):
        return "—"
    if old <= 0:
        return "—"
    value = (new - old) / old * 100.0
    text = f"{value:+.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return text + "%"


def _link(url: Optional[str], label: Optional[str] = None) -> str:
    if not url:
        return esc(label or "")
    return f'<a href="{esc(url)}">{esc(label or url)}</a>'


def format_reprice(lot: OurLot, old_source: float, new_source: float, old_price: float, new_price: float,
                   currency: str = "RUB", applied: bool = True) -> str:
    """HTML-сообщение для Telegram об изменении цены исходника.

    :param applied: True — наш лот уже переоценён (new_price — новая цена лота);
        False — только напоминание (new_price — рекомендуемая цена, лот нужно пересчитать вручную).
    """
    arrow = "📈" if float(new_source) > float(old_source) else "📉"
    title = "Лот переоценён" if applied else "Цена исходника изменилась"
    lines = [f"{arrow} <b>{title}</b>", esc(trim(lot.title_ru or f"лот #{lot.id}", 120))]
    lines.append(f"Исходник: {fmt_money(old_source, currency)} → <b>{fmt_money(new_source, currency)}</b>"
                 f" ({_pct(old_source, new_source)})")
    if applied:
        lines.append(f"Наша цена: {fmt_money(old_price, currency)} → <b>{fmt_money(new_price, currency)}</b>")
    else:
        lines.append(f"Наша цена: {fmt_money(old_price, currency)}, рекомендуемая: <b>{fmt_money(new_price, currency)}</b>")
    if lot.source_url:
        lines.append("Исходник: " + _link(lot.source_url))
    if lot.funpay_url:
        lines.append("Наш лот: " + _link(lot.funpay_url))
    elif lot.id:
        lines.append(f"Наш лот: #{lot.id}")
    if not applied:
        lines.append("❗ Пересчитайте лот — автопереоценка выключена в настройках")
    elif lot.status == LotStatus.DRAFT:
        lines.append("Черновик: цена обновлена только в базе")
    return "\n".join(lines)


class RepricerService:
    """Пересчёт цен наших лотов и рекомендуемых цен кандидатов. Все публичные методы «тихие»."""

    def __init__(self, ctx: AppContext, publisher: PublisherService, notifier=None):
        self.ctx = ctx
        self.publisher = publisher
        self._notifier = notifier               # None — брать ctx.notifier при каждой отправке
        self._busy = threading.Lock()
        self._notified: dict[tuple[int, float], str] = {}   # (lot_id, цена исходника) -> ISO-время напоминания
        self._reported: set[str] = set()        # ошибки данных, о которых уже писали в журнал (чтобы не повторять)
        self.last_run: Optional[str] = None
        self.last_result: dict = {}

    # ------------------------------------------------------------ state
    @property
    def notifier(self):
        return self._notifier if self._notifier is not None else self.ctx.notifier

    @property
    def running(self) -> bool:
        return self._busy.locked()

    def status(self) -> dict:
        s = self.ctx.settings.monitor
        return {
            "running": self.running,
            "enabled": bool(s.auto_reprice),
            "mode": "auto" if s.auto_reprice else "notify",
            "min_change_percent": float(s.reprice_min_change_percent or 0),
            "last_run": self.last_run,
            "last_result": self.last_result,
        }

    # ---------------------------------------------------------- helpers
    def _error(self, result: dict, message: str, once: bool = False, level: str = "error") -> None:
        """Записать ошибку в результат и журнал. ``once`` — в журнал только при первом появлении."""
        result["errors"].append(message)
        if once:
            if message in self._reported:
                return
            self._reported.add(message)
        self.ctx.log("reprice", message, level=level)

    def _profile(self, profile_id: str, cache: dict[str, Profile]) -> Profile:
        if profile_id not in cache:
            cache[profile_id] = self.ctx.profiles.get(profile_id)   # KeyError/ValueError — профиль не найден
        return cache[profile_id]

    def _notify(self, text: str, key: tuple[int, float]) -> bool:
        """Отправить уведомление вида "price" один раз на (лот, цена исходника)."""
        if key in self._notified:
            return False
        self._notified[key] = utcnow().isoformat()
        try:
            return bool(self.notifier.send(text, kind="price"))
        except Exception as e:  # noqa: BLE001
            self.ctx.log("reprice", f"не удалось отправить уведомление о цене лота #{key[0]}: {e}", level="warning")
            return False

    # -------------------------------------------------------------- run
    def run(self, max_candidates: int = 500) -> dict:
        """Переоценить лоты и обновить рекомендуемые цены кандидатов. Никогда не выбрасывает исключений."""
        result: dict = {"checked": 0, "repriced": 0, "notified": 0, "suggested_updated": 0, "skipped": 0,
                        "errors": []}
        if not self._busy.acquire(blocking=False):
            result["errors"].append("переоценка уже выполняется")
            return result
        try:
            lots: list[OurLot] = []
            try:
                lots = self.ctx.storage.list_lots(limit=100000)
                self._reprice_lots(lots, result)
            except Exception as e:  # noqa: BLE001
                self._error(result, f"ошибка переоценки лотов: {e}")
            try:
                self._refresh_candidates(lots, result, max_candidates)
            except Exception as e:  # noqa: BLE001
                self._error(result, f"ошибка обновления рекомендуемых цен: {e}")

            self.last_run = utcnow().isoformat()
            self.last_result = result
            if result["repriced"] or result["notified"] or result["suggested_updated"] or result["errors"]:
                self.ctx.log(
                    "reprice",
                    f"переоценка: проверено лотов {result['checked']}, пересчитано {result['repriced']}, "
                    f"напоминаний {result['notified']}, обновлено рекомендаций {result['suggested_updated']}"
                    + (f", ошибок {len(result['errors'])}" if result["errors"] else ""),
                    level="warning" if result["errors"] else "info",
                    data={k: v for k, v in result.items() if k != "errors"},
                )
            return result
        finally:
            self._busy.release()

    # ------------------------------------------------------------- lots
    def _reprice_lots(self, lots: list[OurLot], result: dict) -> None:
        profiles: dict[str, Profile] = {}
        seen: set[int] = set()
        for lot in lots:
            if lot.status not in _TRACKED or lot.id is None:
                continue
            seen.add(lot.id)
            result["checked"] += 1
            try:
                self._reprice_lot(lot, profiles, result)
            except Exception as e:  # noqa: BLE001
                self._error(result, f"ошибка переоценки лота #{lot.id}: {e}")
        # напоминания по удалённым лотам больше не нужны
        self._notified = {k: v for k, v in self._notified.items() if k[0] in seen}

    def _reprice_lot(self, lot: OurLot, profiles: dict[str, Profile], result: dict) -> None:
        s = self.ctx.settings.monitor
        storage = self.ctx.storage

        found = storage.get_found(lot.found_id)
        if found is None:
            result["skipped"] += 1
            self._error(result, f"лот #{lot.id}: исходное объявление #{lot.found_id} не найдено в базе",
                        once=True, level="warning")
            return
        current = float(found.listing.price or 0)
        base = float(lot.source_price or 0)
        if current <= 0 or abs(current - base) < 0.01:
            result["skipped"] += 1
            return
        change_pct = abs(current - base) / base * 100.0 if base > 0 else 100.0
        if change_pct < float(s.reprice_min_change_percent or 0):
            result["skipped"] += 1
            return

        try:
            profile = self._profile(lot.profile_id, profiles)
        except Exception:  # noqa: BLE001
            result["skipped"] += 1
            self._error(result, f"лот #{lot.id}: профиль «{lot.profile_id}» не найден — цена не пересчитана",
                        once=True)
            return
        new_price = calculate_price(current, profile.pricing)
        old_price = float(lot.price)
        currency = found.listing.currency or "RUB"
        key = (lot.id, current)
        quiet = lot.status == LotStatus.DEACTIVATED   # снятый лот: без FunPay и без уведомлений

        # ---- режим напоминаний: цену не трогаем, напоминаем один раз на каждую новую цену исходника
        if not s.auto_reprice:
            if key in self._notified:
                result["skipped"] += 1
                return
            self.ctx.log(
                "reprice",
                f"лот #{lot.id}: цена исходника изменилась {fmt_money(base, currency)} → {fmt_money(current, currency)}"
                f" ({_pct(base, current)}), пересчитайте лот: рекомендуемая цена {fmt_money(new_price, currency)}"
                f" (сейчас {fmt_money(old_price, currency)})",
                level="warning",
                data={"lot_id": lot.id, "found_id": found.id, "old_source": base, "new_source": current,
                      "price": old_price, "suggested": new_price},
            )
            result["notified"] += 1
            if quiet:
                self._notified[key] = utcnow().isoformat()
            else:
                self._notify(format_reprice(lot, base, current, old_price, new_price, currency, applied=False), key)
            return

        # ---- автопереоценка
        if abs(new_price - old_price) < 0.5:
            # правило наценки даёт ту же цену — просто запоминаем новую цену исходника
            lot.source_price = current
            storage.save_lot(lot)
            result["skipped"] += 1
            return

        lot = self.publisher.update(lot, {"price": new_price})   # сохраняет; для активных — отправляет на FunPay
        if lot.status == LotStatus.ACTIVE and lot.funpay_lot_id and lot.error:
            # FunPay не принял новую цену: возвращаем прежнюю (в базе — то, что реально на FunPay),
            # source_price не трогаем — повторим в следующий раз; lot.error остаётся виден в UI
            lot.price = old_price
            storage.save_lot(lot)
            self._error(result, f"лот #{lot.id}: не удалось отправить новую цену {fmt_money(new_price, currency)}"
                                f" на FunPay, оставлена {fmt_money(old_price, currency)}: {lot.error}")
            return
        lot.source_price = current
        storage.save_lot(lot)
        result["repriced"] += 1
        self.ctx.log(
            "reprice",
            f"лот #{lot.id}: исходник {fmt_money(base, currency)} → {fmt_money(current, currency)}"
            f" ({_pct(base, current)}), наша цена {fmt_money(old_price, currency)} → {fmt_money(new_price, currency)}"
            + (" (лот снят, FunPay не обновлялся)" if quiet else ""),
            data={"lot_id": lot.id, "found_id": found.id, "old_source": base, "new_source": current,
                  "old_price": old_price, "new_price": new_price, "status": lot.status.value},
        )
        if not quiet:
            self._notify(format_reprice(lot, base, current, old_price, new_price, currency, applied=True), key)

    # ------------------------------------------------------- candidates
    def _refresh_candidates(self, lots: list[OurLot], result: dict, limit: int) -> None:
        storage = self.ctx.storage
        with_lot = {l.found_id for l in lots}
        profiles: dict[str, Profile] = {}
        for found in storage.list_found(status=[FoundStatus.CANDIDATE.value], limit=limit, order="last_seen DESC"):
            if found.id in with_lot:
                continue
            try:
                try:
                    profile = self._profile(found.profile_id, profiles)
                except Exception:  # noqa: BLE001
                    self._error(result, f"профиль «{found.profile_id}» не найден — рекомендуемые цены кандидатов"
                                        f" не обновлены", once=True)
                    continue
                price = float(found.listing.price or 0)
                if price <= 0:
                    continue
                suggested = calculate_price(price, profile.pricing)
                if found.suggested_price is None or abs(float(found.suggested_price) - suggested) >= 1.0:
                    storage.update_found_suggested_price(found.id, suggested)
                    result["suggested_updated"] += 1
            except Exception as e:  # noqa: BLE001
                self._error(result, f"кандидат #{found.id}: не удалось пересчитать рекомендуемую цену: {e}")

    # ------------------------------------------------------------ trend
    def price_trend(self, found_id: int, limit: int = 100) -> dict:
        """История цены исходника: {history: [{ts, price}], min, max, first, last, change_percent}."""
        storage = self.ctx.storage
        history = storage.price_history(found_id, limit)
        prices = [float(h["price"]) for h in history]
        if not prices:
            found = storage.get_found(found_id)
            if found is not None:
                prices = [float(found.listing.price)]
        if not prices:
            return {"history": [], "min": None, "max": None, "first": None, "last": None, "change_percent": 0.0}
        first, last = prices[0], prices[-1]
        return {
            "history": history,
            "min": min(prices),
            "max": max(prices),
            "first": first,
            "last": last,
            "change_percent": round((last - first) / first * 100.0, 1) if first else 0.0,
        }
