"""Автоподнятие наших лотов на FunPay («Поднять предложения» в каждой игре).

FunPay разрешает поднимать лоты раз в несколько часов на категорию (игру); при слишком ранней попытке
отвечает текстом с временем ожидания. Сервис группирует подкатегории наших активных лотов по играм,
вызывает ``funpay.raise_lots`` для каждой и запоминает, до какого момента категорию поднимать бесполезно.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

from ..models import LotStatus, utcnow
from .context import AppContext


class RaiserService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._busy = threading.Lock()
        self._next_allowed: dict[int, float] = {}  # category_id -> time.time(), раньше которого не поднимать
        self._category_names: dict[int, str] = {}
        self.last_run: str | None = None
        self.last_result: dict = {}
        self.last_raised: dict[int, str] = {}  # category_id -> ISO-время последнего успешного поднятия

    # ------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._busy.locked()

    def status(self) -> dict:
        now = time.time()
        waiting = []
        for cat_id, ts in sorted(self._next_allowed.items()):
            if ts > now:
                waiting.append(
                    {
                        "category_id": cat_id,
                        "category": self._category_names.get(cat_id, str(cat_id)),
                        "wait_seconds": int(ts - now),
                        "next_allowed": datetime.fromtimestamp(ts, tz=UTC).isoformat(),
                    }
                )
        hours = float(self.ctx.settings.monitor.auto_raise_hours or 0)
        return {
            "running": self.running,
            "enabled": hours > 0,
            "interval_hours": hours,
            "last_run": self.last_run,
            "last_result": self.last_result,
            "last_raised": {self._category_names.get(k, str(k)): v for k, v in self.last_raised.items()},
            "waiting": waiting,
        }

    # ------------------------------------------------------------- plan
    def _groups(self) -> tuple[dict[int, dict], list[int]]:
        """Сгруппировать подкатегории активных лотов по категориям FunPay.

        Возвращает ({category_id: {"name", "subcategory_ids"}}, [неизвестные subcategory_id]).
        """
        lots = self.ctx.storage.list_lots(status=[LotStatus.ACTIVE.value], limit=100000)
        sub_ids = sorted({int(lot.subcategory_id) for lot in lots if lot.subcategory_id})
        if not sub_ids:
            return {}, []
        categories = self.ctx.funpay.categories() or []
        sub_to_cat: dict[int, tuple[int, str]] = {}
        for cat in categories:
            try:
                cat_id = int(cat.get("id"))
            except (TypeError, ValueError):
                continue
            name = str(cat.get("name") or cat_id)
            self._category_names[cat_id] = name
            for sub in cat.get("subcategories") or []:
                try:
                    sub_to_cat[int(sub.get("id"))] = (cat_id, name)
                except (TypeError, ValueError):
                    continue
        groups: dict[int, dict] = {}
        unknown: list[int] = []
        for sid in sub_ids:
            found = sub_to_cat.get(sid)
            if not found:
                unknown.append(sid)
                continue
            cat_id, name = found
            g = groups.setdefault(cat_id, {"name": name, "subcategory_ids": []})
            g["subcategory_ids"].append(sid)
        return groups, unknown

    # -------------------------------------------------------------- run
    def run(self) -> dict:
        """Поднять лоты во всех категориях, где у нас есть активные лоты. Никогда не выбрасывает исключений."""
        result: dict = {"raised": [], "waiting": [], "errors": [], "skipped": []}
        if not self.ctx.settings.funpay.golden_key:
            result["errors"].append("не задан golden_key FunPay в настройках")
            return result
        if not self._busy.acquire(blocking=False):
            result["errors"].append("поднятие уже выполняется")
            return result
        try:
            try:
                groups, unknown = self._groups()
            except Exception as e:
                msg = f"не удалось получить список категорий FunPay: {e}"
                result["errors"].append(msg)
                self.ctx.log("raise", msg, level="error")
                return result
            for sid in unknown:
                result["errors"].append(f"подкатегория {sid} не найдена в списке категорий FunPay")
            if not groups:
                self.ctx.log(
                    "raise",
                    "нет активных лотов для поднятия"
                    if not unknown
                    else "активные лоты есть, но их категории не найдены",
                    level="info",
                )
                return result

            for cat_id, g in groups.items():
                name = g["name"]
                now = time.time()
                next_allowed = self._next_allowed.get(cat_id)
                if next_allowed and now < next_allowed:
                    wait = int(next_allowed - now)
                    result["skipped"].append({"category": name, "wait_seconds": wait})
                    result["waiting"].append({"category": name, "wait_seconds": wait})
                    continue
                try:
                    resp = self.ctx.funpay.raise_lots(cat_id, g["subcategory_ids"]) or {}
                except Exception as e:
                    msg = f"{name}: {e}"
                    result["errors"].append(msg)
                    self.ctx.log("raise", f"ошибка поднятия лотов «{name}»: {e}", level="error")
                    continue
                message = str(resp.get("message") or "")
                if resp.get("ok"):
                    self._next_allowed.pop(cat_id, None)
                    self.last_raised[cat_id] = utcnow().isoformat()
                    result["raised"].append(name)
                    self.ctx.log(
                        "raise",
                        f"лоты «{name}» подняты" + (f": {message}" if message else ""),
                        data={"category_id": cat_id, "subcategory_ids": g["subcategory_ids"]},
                    )
                    continue
                wait_seconds = resp.get("wait_seconds")
                if wait_seconds:
                    wait_seconds = int(wait_seconds)
                    self._next_allowed[cat_id] = time.time() + wait_seconds
                    result["waiting"].append({"category": name, "wait_seconds": wait_seconds, "message": message})
                    self.ctx.log(
                        "raise",
                        f"«{name}»: поднять можно через {timedelta(seconds=wait_seconds)}"
                        + (f" ({message})" if message else ""),
                    )
                else:
                    result["errors"].append(f"{name}: {message or 'FunPay отказал без пояснения'}")
                    self.ctx.log(
                        "raise", f"«{name}»: не удалось поднять лоты: {message or 'без пояснения'}", level="warning"
                    )

            self.ctx.log(
                "raise",
                f"поднятие лотов: поднято {len(result['raised'])}, ожидание {len(result['waiting'])}, "
                f"ошибок {len(result['errors'])}",
                level="warning" if result["errors"] else "info",
                data={"raised": result["raised"], "waiting": result["waiting"]},
            )
            return result
        finally:
            self.last_run = utcnow().isoformat()
            self.last_result = result
            self._busy.release()
