"""Фоновый монитор: проверяет доступность исходников наших лотов и кандидатов, запускает автопоиск."""
from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Optional

from ..models import FoundStatus, LotStatus, utcnow
from .context import AppContext
from .publisher import PublisherService
from .search import SearchService


class MonitorService:
    def __init__(self, ctx: AppContext, publisher: PublisherService, search: SearchService,
                 orders=None, raiser=None, repricer=None):
        self.ctx = ctx
        self.publisher = publisher
        self.search = search
        self.orders = orders      # OrdersService (опционально)
        self.raiser = raiser      # RaiserService (опционально)
        self.repricer = repricer  # RepricerService (опционально)
        self._last_orders: Optional[float] = None
        self._last_raise: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._busy = threading.Lock()
        self.last_run: Optional[str] = None
        self.next_run: Optional[str] = None
        self.last_search_auto: Optional[float] = None
        self.last_result: dict = {}

    @property
    def running(self) -> bool:
        return self._busy.locked()

    def status(self) -> dict:
        return {"running": self.running, "last_run": self.last_run, "next_run": self.next_run,
                "interval_minutes": self.ctx.settings.monitor.interval_minutes,
                "enabled": self.ctx.settings.monitor.enabled, "last_result": self.last_result}

    # ---------------------------------------------------------- thread
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # первая проверка через минуту после старта, далее по интервалу
        self.next_run = (utcnow() + timedelta(minutes=1)).isoformat()
        while not self._stop.wait(30):
            s = self.ctx.settings.monitor
            now = utcnow()
            if s.enabled and self.next_run and now.isoformat() >= self.next_run:
                try:
                    self.run_once()
                except Exception as e:  # noqa: BLE001
                    self.ctx.log("monitor", f"ошибка монитора: {e}", level="error")
                self.next_run = (utcnow() + timedelta(minutes=max(1, s.interval_minutes))).isoformat()
            if s.auto_search_minutes and s.auto_search_minutes > 0:
                if (self.last_search_auto is None or
                        time.time() - self.last_search_auto >= s.auto_search_minutes * 60):
                    self.last_search_auto = time.time()
                    if not self.search.running:
                        self.ctx.log("search", "автопоиск по расписанию")
                        self.search.start()
            # заказы (продажи наших лотов)
            if self.orders and s.orders_check_minutes and self.ctx.settings.funpay.golden_key:
                if self._last_orders is None or time.time() - self._last_orders >= s.orders_check_minutes * 60:
                    self._last_orders = time.time()
                    try:
                        self.orders.sync()
                    except Exception as e:  # noqa: BLE001
                        self.ctx.log("orders", f"ошибка проверки заказов: {e}", level="error")
            # автоподнятие лотов
            if self.raiser and s.auto_raise_hours and s.auto_raise_hours > 0 and self.ctx.settings.funpay.golden_key:
                if self._last_raise is None or time.time() - self._last_raise >= s.auto_raise_hours * 3600:
                    self._last_raise = time.time()
                    try:
                        self.raiser.run()
                    except Exception as e:  # noqa: BLE001
                        self.ctx.log("raise", f"ошибка автоподнятия: {e}", level="error")

    # --------------------------------------------------------- run once
    def run_once(self, max_candidates: int = 100) -> dict:
        if not self._busy.acquire(blocking=False):
            return {"checked": 0, "unavailable": 0, "skipped": "уже выполняется"}
        try:
            checked = unavailable = 0
            # 1) наши активные лоты — самое важное
            for lot in self.ctx.storage.list_lots(status=[LotStatus.ACTIVE.value, LotStatus.DRAFT.value]):
                lot = self.publisher.check_source(lot)
                checked += 1
                if lot.source_available is False:
                    unavailable += 1
            # 2) кандидаты без лота — чтобы не предлагать проданное
            for found in self.ctx.storage.list_found(status=[FoundStatus.CANDIDATE.value], limit=max_candidates,
                                                     order="last_seen DESC"):
                if self.ctx.storage.get_lot_by_found(found.id):
                    continue
                try:
                    available = self.ctx.source(found.listing.source).is_available(found.listing.source_id)
                except Exception:  # noqa: BLE001
                    available = None
                self.ctx.storage.set_found_availability(found.id, available)
                checked += 1
                if available is False:
                    unavailable += 1
                    self.ctx.storage.set_found_status(found.id, FoundStatus.SOLD)
            self.last_run = utcnow().isoformat()
            self.last_result = {"checked": checked, "unavailable": unavailable}
            self.ctx.log("monitor", f"проверка доступности: проверено {checked}, недоступно {unavailable}")
            # 3) цены исходников могли измениться — пересчитываем наши лоты
            if self.repricer:
                try:
                    self.repricer.run()
                except Exception as e:  # noqa: BLE001
                    self.ctx.log("reprice", f"ошибка репрайсинга: {e}", level="error")
            return self.last_result
        finally:
            self._busy.release()
