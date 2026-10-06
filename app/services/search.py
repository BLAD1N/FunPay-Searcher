"""Сервис поиска: обходит источники по профилям, применяет критерии, сохраняет находки."""
from __future__ import annotations

import threading
import traceback
from typing import Callable, Optional

from ..matching import evaluate
from ..models import Found, FoundStatus, MatchResult, Profile, SearchRunStats, utcnow
from ..pricing import calculate_price
from .context import AppContext


class SearchService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self.progress: dict = {"profile_id": None, "profile_name": None, "source": None,
                               "fetched": 0, "matched": 0, "new": 0, "done": 0, "total": 0}
        self.last: list[SearchRunStats] = []
        self.last_started: Optional[str] = None
        self.last_finished: Optional[str] = None
        # вызывается после каждой задачи (профиль+источник) со списком НОВЫХ подходящих находок
        self.on_new_candidates: Optional[Callable[[Profile, list[Found]], None]] = None
        # вызывается по завершении всего прогона со статистикой
        self.on_finished: Optional[Callable[[list[SearchRunStats]], None]] = None

    # ----------------------------------------------------------- state
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        return {
            "running": self.running,
            "progress": dict(self.progress),
            "last": [s.model_dump(mode="json") for s in self.last],
            "last_started": self.last_started,
            "last_finished": self.last_finished,
        }

    def cancel(self) -> None:
        self._cancel.set()

    # ----------------------------------------------------------- start
    def start(self, profile_ids: Optional[list[str]] = None, sources: Optional[list[str]] = None) -> bool:
        with self._lock:
            if self.running:
                return False
            self._cancel.clear()
            self._thread = threading.Thread(target=self._run_safe, args=(profile_ids, sources),
                                            name="search", daemon=True)
            self._thread.start()
            return True

    def run_sync(self, profile_ids: Optional[list[str]] = None, sources: Optional[list[str]] = None) -> list[SearchRunStats]:
        """Синхронный запуск (для тестов и автопоиска из монитора)."""
        self._cancel.clear()
        return self._run(profile_ids, sources)

    def _run_safe(self, profile_ids, sources):
        try:
            self._run(profile_ids, sources)
        except Exception as e:  # noqa: BLE001
            self.ctx.log("search", f"поиск аварийно завершён: {e}", level="error",
                         data={"trace": traceback.format_exc()[-2000:]})

    # ------------------------------------------------------------- run
    def _select_profiles(self, profile_ids: Optional[list[str]]) -> list[Profile]:
        profiles = self.ctx.profiles.list()
        if profile_ids:
            wanted = set(profile_ids)
            return [p for p in profiles if p.id in wanted]
        return [p for p in profiles if p.enabled]

    def _run(self, profile_ids, sources) -> list[SearchRunStats]:
        self.last_started = utcnow().isoformat()
        self.last_finished = None
        profiles = self._select_profiles(profile_ids)
        wanted_sources = set(sources or ["funpay", "lolz"])
        # источники без ключей пропускаем сразу (одно предупреждение вместо ошибки на каждый профиль)
        has_creds = {"funpay": bool(self.ctx.settings.funpay.golden_key), "lolz": bool(self.ctx.settings.lolz.token)}
        for name in list(wanted_sources):
            if not has_creds.get(name):
                wanted_sources.discard(name)
                self.ctx.log("search", f"{name}: не задан токен/cookie в настройках — источник пропущен", level="warning")
        jobs: list[tuple[Profile, str]] = []
        for p in profiles:
            if "funpay" in wanted_sources and p.funpay().enabled:
                jobs.append((p, "funpay"))
            if "lolz" in wanted_sources and p.lolz().enabled:
                jobs.append((p, "lolz"))
        self.progress.update({"done": 0, "total": len(jobs), "fetched": 0, "matched": 0, "new": 0})
        self.ctx.log("search", f"старт поиска: профилей {len(profiles)}, задач {len(jobs)}")
        stats_all: list[SearchRunStats] = []

        for profile, source_name in jobs:
            if self._cancel.is_set():
                self.ctx.log("search", "поиск отменён пользователем", level="warning")
                break
            stats = SearchRunStats(profile_id=profile.id, source=source_name)
            self.progress.update({"profile_id": profile.id, "profile_name": profile.name, "source": source_name,
                                  "fetched": 0, "matched": 0, "new": 0})
            try:
                source = self.ctx.source(source_name)
                listings = source.search(profile)
                stats.fetched = len(listings)
                self.progress["fetched"] = len(listings)
                new_candidates: list[Found] = []
                for listing in listings:
                    if self._cancel.is_set():
                        break
                    found, is_new = self._process(profile, listing)
                    if found.match.matched:
                        stats.matched += 1
                        self.progress["matched"] = stats.matched
                    if is_new and found.match.matched:
                        stats.new += 1
                        self.progress["new"] = stats.new
                        new_candidates.append(found)
                if new_candidates and self.on_new_candidates:
                    try:
                        self.on_new_candidates(profile, new_candidates)
                    except Exception as e:  # noqa: BLE001
                        self.ctx.log("search", f"обработчик новых находок: {e}", level="error")
                self.ctx.log("search", f"{profile.name} / {source_name}: получено {stats.fetched}, "
                                       f"подходит {stats.matched}, новых {stats.new}")
            except Exception as e:  # noqa: BLE001
                msg = f"{profile.name} / {source_name}: {e}"
                stats.errors.append(str(e))
                self.ctx.log("search", msg, level="error")
            stats.finished_at = utcnow()
            stats_all.append(stats)
            self.progress["done"] += 1

        self.last = stats_all
        self.last_finished = utcnow().isoformat()
        self.ctx.log("search", "поиск завершён")
        if self.on_finished:
            try:
                self.on_finished(stats_all)
            except Exception as e:  # noqa: BLE001
                self.ctx.log("search", f"обработчик завершения поиска: {e}", level="error")
        return stats_all

    def _process(self, profile: Profile, listing) -> tuple[Found, bool]:
        try:
            match: MatchResult = evaluate(listing, profile.criteria)
        except Exception as e:  # noqa: BLE001
            match = MatchResult(matched=False, rejections=[f"ошибка критериев: {e}"])
        suggested = None
        if match.matched:
            try:
                suggested = calculate_price(listing.price, profile.pricing)
            except Exception as e:  # noqa: BLE001
                match.rejections.append(f"ошибка расчёта цены: {e}")
                match.matched = False
        found = Found(profile_id=profile.id, listing=listing, match=match, suggested_price=suggested,
                      status=FoundStatus.CANDIDATE if match.matched else FoundStatus.REJECTED)
        return self.ctx.storage.upsert_found(found)
