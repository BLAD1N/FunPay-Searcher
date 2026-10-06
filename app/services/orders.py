"""Сервис заказов: синхронизация продаж с FunPay, сопоставление с нашими лотами, уведомления.

Поток работы ``sync()``:
  1. ``ctx.funpay.get_sales(...)`` — список заказов (оплаченные/закрытые/возвраты);
  2. каждый заказ сопоставляется с нашим лотом (точное название → нормализованное → уникальная цена);
  3. ``storage.upsert_order``; для НОВЫХ оплаченных заказов — уведомление в Telegram и пометка лота как проданного.
FunPay сам снимает лот после продажи (deactivate_after_sale), поэтому запросов к FunPay здесь нет.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Optional

from ..models import LotStatus, Order, OrderStatus, OurLot, utcnow
from ..notify import TelegramNotifier
from .context import AppContext

# повторная попытка уведомить о не доставленном заказе — только пока заказ «свежий»
RENOTIFY_WINDOW = timedelta(hours=24)

# предпочтение статусов лота при сопоставлении (меньше — лучше)
_STATUS_RANK = {
    LotStatus.ACTIVE: 0,
    LotStatus.DEACTIVATED: 1,
    LotStatus.SOLD: 2,
    LotStatus.ERROR: 3,
    LotStatus.DRAFT: 4,
}


def _normalize(text: Optional[str]) -> str:
    """casefold + схлопывание пробелов — для нестрогого сравнения названий."""
    return " ".join((text or "").casefold().split())


def _parse_status(value: Any) -> OrderStatus:
    """Статус заказа FunPay -> OrderStatus (неизвестные значения трактуем по ключевым словам)."""
    if isinstance(value, OrderStatus):
        return value
    v = str(value or "").strip().lower()
    try:
        return OrderStatus(v)
    except ValueError:
        pass
    if "refund" in v or "возврат" in v:
        return OrderStatus.REFUNDED
    if "clos" in v or "закрыт" in v or "выполн" in v:
        return OrderStatus.CLOSED
    return OrderStatus.PAID


def _parse_date(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


class OrdersService:
    def __init__(self, ctx: AppContext, notifier: TelegramNotifier):
        self.ctx = ctx
        self.notifier = notifier
        self._busy = threading.Lock()
        self.last_sync: Optional[str] = None
        self.last_result: dict = {}

    # ------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._busy.locked()

    def status(self) -> dict:
        s = self.ctx.settings.monitor
        return {"running": self.running, "last_sync": self.last_sync, "last_result": self.last_result,
                "interval_minutes": s.orders_check_minutes, "enabled": s.orders_check_minutes > 0}

    # ---------------------------------------------------------- convert
    @staticmethod
    def to_order(raw: dict) -> Order:
        """Словарь от FunPaySource.get_sales -> Order."""
        order_id = str(raw.get("order_id") or "").strip()
        if not order_id:
            raise ValueError("у заказа нет order_id")
        try:
            price = float(raw.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        return Order(
            funpay_order_id=order_id,
            status=_parse_status(raw.get("status")),
            title=str(raw.get("title") or "").strip(),
            subcategory_name=raw.get("subcategory_name") or None,
            price=price,
            currency=str(raw.get("currency") or "RUB"),
            buyer_name=raw.get("buyer_name") or None,
            buyer_id=(str(raw["buyer_id"]) if raw.get("buyer_id") is not None else None),
            buyer_url=raw.get("buyer_url") or None,
            order_url=str(raw.get("order_url") or f"https://funpay.com/orders/{order_id}/"),
            order_date=_parse_date(raw.get("date")),
        )

    # ------------------------------------------------------------ match
    @staticmethod
    def match_lot(order: Order, lots: list[OurLot]) -> Optional[OurLot]:
        """Найти наш лот по заказу: точное название → нормализованное → уникальная цена среди активных."""
        if not lots:
            return None

        def best(candidates: list[OurLot]) -> Optional[OurLot]:
            if not candidates:
                return None
            # list_lots уже отсортирован по updated_at DESC — при равном статусе берём самый свежий
            return min(candidates, key=lambda l: _STATUS_RANK.get(l.status, 9))

        title = (order.title or "").strip()
        if title:
            exact = [l for l in lots if (l.title_ru or "").strip() == title]
            if exact:
                return best(exact)
            norm = _normalize(title)
            fuzzy = [l for l in lots if _normalize(l.title_ru) == norm or (l.title_en and _normalize(l.title_en) == norm)]
            if fuzzy:
                return best(fuzzy)
        if order.price and order.price > 0:
            by_price = [l for l in lots if l.status == LotStatus.ACTIVE and abs(float(l.price) - float(order.price)) < 0.01]
            if len(by_price) == 1:
                return by_price[0]
        return None

    # ------------------------------------------------------------- sync
    def sync(self, max_pages: int = 1) -> dict:
        """Синхронизировать заказы с FunPay. Никогда не выбрасывает исключений."""
        result: dict = {"new": 0, "updated": 0, "matched": 0, "errors": []}
        if not self.ctx.settings.funpay.golden_key:
            result["errors"].append("не задан golden_key FunPay в настройках")
            return result
        if not self._busy.acquire(blocking=False):
            result["errors"].append("синхронизация заказов уже выполняется")
            return result
        try:
            try:
                sales = self.ctx.funpay.get_sales(include_paid=True, include_closed=True, include_refunded=True,
                                                  max_pages=max_pages) or []
            except Exception as e:  # noqa: BLE001
                msg = f"не удалось получить список заказов FunPay: {e}"
                result["errors"].append(msg)
                self.ctx.log("orders", msg, level="error")
                self.last_result = result
                return result

            storage = self.ctx.storage
            lots = storage.list_lots(limit=100000)
            known = {o.funpay_order_id: o for o in storage.list_orders(limit=100000)}
            now = utcnow()

            for raw in sales:
                try:
                    self._process(raw, lots, known, now, result)
                except Exception as e:  # noqa: BLE001
                    oid = raw.get("order_id") if isinstance(raw, dict) else raw
                    msg = f"ошибка обработки заказа {oid}: {e}"
                    result["errors"].append(msg)
                    self.ctx.log("orders", msg, level="error")

            self.last_sync = utcnow().isoformat()
            self.last_result = result
            self.ctx.log(
                "orders",
                f"синхронизация заказов: получено {len(sales)}, новых {result['new']}, "
                f"обновлено {result['updated']}, сопоставлено {result['matched']}"
                + (f", ошибок {len(result['errors'])}" if result["errors"] else ""),
                level="warning" if result["errors"] else "info",
                data={k: v for k, v in result.items() if k != "errors"},
            )
            return result
        finally:
            self._busy.release()

    def _process(self, raw: dict, lots: list[OurLot], known: dict[str, Order], now: datetime, result: dict) -> None:
        storage = self.ctx.storage
        order = self.to_order(raw)
        lot = self.match_lot(order, lots)
        if lot is not None:
            order.lot_id = lot.id
            order.source_url = lot.source_url or None
            order.source_price = lot.source_price or None
            result["matched"] += 1

        previous = known.get(order.funpay_order_id)
        saved, is_new = storage.upsert_order(order)
        known[saved.funpay_order_id] = saved

        if is_new:
            result["new"] += 1
            buyer = order.buyer_name or order.buyer_id or "?"
            self.ctx.log(
                "orders",
                f"новый заказ #{order.funpay_order_id} ({order.status.value}): {order.title} — "
                f"{order.price:.0f} {order.currency}, покупатель {buyer}"
                + (f", наш лот #{lot.id}" if lot else ", лот не сопоставлен"),
                data={"order_id": order.funpay_order_id, "lot_id": lot.id if lot else None},
            )
        elif previous is not None and previous.status != saved.status:
            result["updated"] += 1
            self.ctx.log("orders", f"заказ #{order.funpay_order_id}: статус {previous.status.value} → {saved.status.value}",
                         data={"order_id": order.funpay_order_id})

        # лот продан: оплаченный/закрытый заказ по активному лоту (FunPay сам снимает лот после продажи)
        if lot is not None and saved.status in (OrderStatus.PAID, OrderStatus.CLOSED) and lot.status == LotStatus.ACTIVE:
            lot.status = LotStatus.SOLD
            lot.error = None
            storage.save_lot(lot)
            self.ctx.log("orders", f"лот #{lot.id} «{lot.title_ru}» продан (заказ #{order.funpay_order_id}) — помечен как sold",
                         data={"lot_id": lot.id, "order_id": order.funpay_order_id})

        # уведомление: новый оплаченный заказ (или повтор, если прошлая попытка не удалась и заказ свежий)
        if saved.status == OrderStatus.PAID and not saved.notified:
            fresh = is_new or (saved.first_seen is not None and now - saved.first_seen <= RENOTIFY_WINDOW)
            if fresh:
                self._notify(saved, lot)

    def _notify(self, order: Order, lot: Optional[OurLot]) -> None:
        try:
            text = self.notifier.format_order(order, lot)
            sent = self.notifier.send(text, kind="order")
        except Exception as e:  # noqa: BLE001
            sent = False
            self.ctx.log("orders", f"не удалось отправить уведомление о заказе #{order.funpay_order_id}: {e}",
                         level="warning")
        # если уведомления выключены — считаем заказ обработанным, чтобы не пытаться бесконечно
        try:
            allowed = self.notifier.allowed("order")
        except Exception:  # noqa: BLE001
            allowed = False
        notified = bool(sent) or not allowed
        if notified != order.notified:
            self.ctx.storage.set_order_fields(order.id, notified=notified)
            order.notified = notified

    # ------------------------------------------------------------ query
    def _order_out(self, order: Order) -> dict:
        d = order.model_dump(mode="json")
        lot = self.ctx.storage.get_lot(order.lot_id) if order.lot_id else None
        d["lot"] = lot.model_dump(mode="json") if lot else None
        return d

    def list(self, status: Optional[Any] = None, limit: int = 200) -> list[dict]:
        """Заказы из базы (новые сверху) + наш лот, если сопоставлен."""
        statuses: Optional[list[str]] = None
        if status:
            if isinstance(status, str):
                statuses = [s.strip() for s in status.split(",") if s.strip()]
            else:
                statuses = [s.value if isinstance(s, OrderStatus) else str(s) for s in status]
        orders = self.ctx.storage.list_orders(status=statuses or None, limit=limit)
        return [self._order_out(o) for o in orders]

    def get(self, order_id: int) -> Optional[dict]:
        order = self.ctx.storage.get_order(order_id)
        return self._order_out(order) if order else None

    def set_note(self, order_id: int, note: str) -> Optional[dict]:
        """Сохранить заметку к заказу (например, «исходник куплен, данные переданы»)."""
        order = self.ctx.storage.get_order(order_id)
        if order is None:
            return None
        self.ctx.storage.set_order_fields(order_id, note=str(note or ""))
        return self.get(order_id)
