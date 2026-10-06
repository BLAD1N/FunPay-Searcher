"""HTTP API + раздача веб-интерфейса (FastAPI)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .models import Criteria, Found, FoundStatus, Listing, LotStatus, MatchResult, OurLot, PricingRule, Profile
from .pricing import calculate_price
from .services.context import AppContext
from .services.monitor import MonitorService
from .services.publisher import PublisherService
from .services.search import SearchService
from .settings import Settings

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
if not STATIC_DIR.exists():  # сборка PyInstaller
    from .settings import BUNDLE_DIR
    STATIC_DIR = BUNDLE_DIR / "app" / "web" / "static"
log = logging.getLogger("app")


# Модели тел запросов объявлены на уровне модуля: при `from __future__ import annotations`
# FastAPI не может разрешить аннотации на локальные классы внутри create_app().
class SearchBody(BaseModel):
    profile_ids: Optional[list[str]] = None
    sources: Optional[list[str]] = None


class PricingPreview(BaseModel):
    price: float
    pricing: PricingRule


class MatchingTest(BaseModel):
    profile: Optional[Profile] = None
    profile_id: Optional[str] = None
    criteria: Optional[Criteria] = None
    text: str = ""
    price: float = 0
    region: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)


def create_app(ctx: Optional[AppContext] = None, start_monitor: bool = True) -> FastAPI:
    ctx = ctx or AppContext()
    search = SearchService(ctx)
    publisher = PublisherService(ctx)
    orders = raiser = None
    try:
        from .services.orders import OrdersService
        from .services.raiser import RaiserService
        orders = OrdersService(ctx, ctx.notifier)
        raiser = RaiserService(ctx)
    except Exception as e:  # noqa: BLE001 — модули могут отсутствовать в урезанной сборке
        log.warning("сервисы заказов/поднятия недоступны: %s", e)
    repricer = autoreply = None
    try:
        from .services.repricer import RepricerService
        repricer = RepricerService(ctx, publisher)
    except Exception as e:  # noqa: BLE001
        log.warning("сервис репрайсинга недоступен: %s", e)
    monitor = MonitorService(ctx, publisher, search, orders=orders, raiser=raiser, repricer=repricer)
    try:
        from .services.autoreply import AutoReplyService
        autoreply = AutoReplyService(ctx)
    except Exception as e:  # noqa: BLE001
        log.warning("автоответчик недоступен: %s", e)

    def _after_search_finished(stats) -> None:
        """После поиска цены исходников обновлены — пересчитываем наши лоты."""
        if repricer:  # в режиме «только уведомлять» run() сам не меняет цены
            try:
                repricer.run()
            except Exception as e:  # noqa: BLE001
                ctx.log("reprice", f"ошибка репрайсинга: {e}", level="error")

    search.on_finished = _after_search_finished

    def _after_search(profile: Profile, new_found: list[Found]) -> None:
        """Новые подходящие находки: автопубликация (если включена) и уведомление в Telegram."""
        if ctx.settings.funpay.auto_publish and ctx.settings.funpay.golden_key:
            for f in new_found:
                try:
                    publisher.create(f, publish=True)
                except Exception as e:  # noqa: BLE001
                    ctx.log("lots", f"автопубликация {f.listing.key}: {e}", level="error")
        try:
            ctx.notify(ctx.notifier.format_candidates(profile.name, new_found), kind="candidates")
        except Exception as e:  # noqa: BLE001
            ctx.log("search", f"уведомление о находках: {e}", level="warning")

    search.on_new_candidates = _after_search

    app = FastAPI(title="FunPay Searcher", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.state.ctx, app.state.search, app.state.publisher, app.state.monitor = ctx, search, publisher, monitor
    app.state.orders, app.state.raiser = orders, raiser
    app.state.repricer, app.state.autoreply = repricer, autoreply

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        ctx.log("app", f"запуск FunPay Searcher v{__version__}")
        if start_monitor:
            monitor.start()
            if autoreply:
                autoreply.start()
        yield
        monitor.stop()
        if autoreply:
            try:
                autoreply.stop()
            except Exception:  # noqa: BLE001
                pass

    app.router.lifespan_context = _lifespan

    @app.middleware("http")
    async def _same_origin_guard(request: Request, call_next):
        """Защита от CSRF с чужих сайтов: изменяющие запросы к /api принимаем только со своего адреса."""
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin:
                from urllib.parse import urlsplit
                o = urlsplit(origin)
                allowed_hosts = {request.headers.get("host", ""), f"{request.url.hostname}:{request.url.port}",
                                 request.url.hostname or ""}
                if o.netloc and o.netloc not in allowed_hosts and o.hostname not in ("127.0.0.1", "localhost"):
                    return JSONResponse(status_code=403, content={"detail": "запрос с чужого источника отклонён"})
        return await call_next(request)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("необработанная ошибка в %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": str(exc) or exc.__class__.__name__})

    # ------------------------------------------------------------ helpers
    def _found_out(f: Found) -> dict:
        d = f.model_dump(mode="json")
        lot = ctx.storage.get_lot_by_found(f.id) if f.id else None
        d["lot"] = lot.model_dump(mode="json") if lot else None
        d["profile_name"] = _profile_names().get(f.profile_id, f.profile_id)
        return d

    def _lot_out(l: OurLot) -> dict:
        d = l.model_dump(mode="json")
        found = ctx.storage.get_found(l.found_id)
        d["found"] = found.model_dump(mode="json") if found else None
        d["profile_name"] = _profile_names().get(l.profile_id, l.profile_id)
        return d

    _names_cache: dict[str, Any] = {"ts": 0.0, "names": {}}

    def _profile_names() -> dict[str, str]:
        import time
        if time.time() - _names_cache["ts"] > 5:
            _names_cache["names"] = {p.id: p.name for p in ctx.profiles.list()}
            _names_cache["ts"] = time.time()
        return _names_cache["names"]

    def _get_found(found_id: int) -> Found:
        f = ctx.storage.get_found(found_id)
        if not f:
            raise HTTPException(404, "находка не найдена")
        return f

    def _get_lot(lot_id: int) -> OurLot:
        l = ctx.storage.get_lot(lot_id)
        if not l:
            raise HTTPException(404, "лот не найден")
        return l

    def _require_funpay():
        if not ctx.settings.funpay.golden_key:
            raise HTTPException(400, "не задан golden_key FunPay в настройках")

    def _require_lolz():
        if not ctx.settings.lolz.token:
            raise HTTPException(400, "не задан токен Lolzteam в настройках")

    # ------------------------------------------------------------- status
    @app.get("/api/status")
    def status():
        return {
            "version": __version__,
            "brand": ctx.settings.ui.brand,
            "counts": {
                "found": ctx.storage.count_found(),
                "lots": {s.value: len(ctx.storage.list_lots(status=[s.value], limit=100000)) for s in LotStatus},
            },
            "search": search.status(),
            "monitor": monitor.status(),
            "auth": ctx.auth_state,
            "auto_publish": ctx.settings.funpay.auto_publish,
            "orders": {"paid": len(ctx.storage.list_orders(status=["paid"], limit=100000)),
                       "total": len(ctx.storage.list_orders(limit=100000))},
            "raise": raiser.status() if raiser else None,
            "reprice": repricer.status() if repricer else None,
            "autoreply": autoreply.status() if autoreply else None,
            "telegram": {"enabled": bool(getattr(ctx.notifier, "enabled", False))},
        }

    # ----------------------------------------------------------- settings
    @app.get("/api/settings")
    def get_settings():
        return ctx.settings.masked()

    @app.put("/api/settings")
    def put_settings(body: dict):
        current = ctx.settings.model_dump(mode="json")
        merged = _deep_merge(current, body or {})
        for sec, key in (("funpay", "golden_key"), ("lolz", "token"), ("telegram", "bot_token")):
            v = (merged.get(sec) or {}).get(key)
            if not v or "…" in str(v) or set(str(v)) == {"•"}:
                merged.setdefault(sec, {})[key] = current[sec][key]
            merged[sec].pop(key + "_set", None)
        try:
            new_settings = Settings.model_validate(merged)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"ошибка в настройках: {e}")
        ctx.apply_settings(new_settings)
        ctx.log("settings", "настройки сохранены")
        return ctx.settings.masked()

    @app.post("/api/auth/check")
    def auth_check(body: Optional[dict] = None):
        which = (body or {}).get("sources")
        return ctx.check_auth(which)

    # ----------------------------------------------------------- profiles
    @app.get("/api/profiles")
    def list_profiles():
        return [p.model_dump(mode="json") for p in ctx.profiles.list()]

    @app.get("/api/profiles/{profile_id}")
    def get_profile(profile_id: str):
        try:
            return ctx.profiles.get(profile_id).model_dump(mode="json")
        except KeyError:
            raise HTTPException(404, "профиль не найден")

    @app.post("/api/profiles", status_code=201)
    def create_profile(profile: Profile):
        try:
            ctx.profiles.get(profile.id)
            raise HTTPException(409, "профиль с таким id уже существует")
        except KeyError:
            pass
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            ctx.profiles.save(profile)
        except ValueError as e:
            raise HTTPException(400, str(e))
        ctx.log("profiles", f"создан профиль {profile.id}")
        return profile.model_dump(mode="json")

    @app.put("/api/profiles/{profile_id}")
    def update_profile(profile_id: str, profile: Profile):
        if profile.id != profile_id:
            raise HTTPException(400, "id профиля нельзя менять")
        try:
            ctx.profiles.save(profile)
        except ValueError as e:
            raise HTTPException(400, str(e))
        ctx.log("profiles", f"профиль {profile.id} сохранён")
        return profile.model_dump(mode="json")

    @app.delete("/api/profiles/{profile_id}")
    def delete_profile(profile_id: str):
        try:
            ctx.profiles.delete(profile_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        ctx.log("profiles", f"профиль {profile_id} удалён")
        return {"ok": True}

    @app.post("/api/profiles/{profile_id}/duplicate", status_code=201)
    def duplicate_profile(profile_id: str):
        try:
            p = ctx.profiles.get(profile_id)
        except KeyError:
            raise HTTPException(404, "профиль не найден")
        new_id = f"{p.id}_copy"
        i = 2
        while True:
            try:
                ctx.profiles.get(new_id)
                new_id = f"{p.id}_copy{i}"
                i += 1
            except KeyError:
                break
        copy = p.model_copy(update={"id": new_id, "name": f"{p.name} (копия)"})
        ctx.profiles.save(copy)
        return copy.model_dump(mode="json")

    # ------------------------------------------------------------- search
    @app.post("/api/search")
    def start_search(body: Optional[SearchBody] = None):
        body = body or SearchBody()
        if body.sources:
            bad = [s for s in body.sources if s not in ("funpay", "lolz")]
            if bad:
                raise HTTPException(400, f"неизвестный источник: {bad}")
        if not search.start(body.profile_ids, body.sources):
            raise HTTPException(409, "поиск уже выполняется")
        return {"started": True}

    @app.post("/api/search/cancel")
    def cancel_search():
        search.cancel()
        return {"ok": True}

    @app.get("/api/search/status")
    def search_status():
        return search.status()

    # -------------------------------------------------------------- found
    @app.get("/api/found")
    def list_found(profile_id: Optional[str] = None, status: Optional[str] = None, source: Optional[str] = None,
                   limit: int = Query(200, le=5000), offset: int = 0, order: str = "score"):
        statuses = [s for s in (status or "").split(",") if s] or None
        order_sql = {"score": "score DESC, last_seen DESC", "price": "price ASC", "price_asc": "price ASC",
                     "price_desc": "price DESC", "recent": "last_seen DESC", "first_seen": "first_seen DESC",
                     "suggested_price": "suggested_price DESC"}.get(order, "score DESC, last_seen DESC")
        items = ctx.storage.list_found(profile_id=profile_id, status=statuses, source=source,
                                       limit=limit, offset=offset, order=order_sql)
        return [_found_out(f) for f in items]

    @app.get("/api/found/{found_id}")
    def get_found(found_id: int):
        return _found_out(_get_found(found_id))

    @app.post("/api/found/{found_id}/status")
    def set_found_status(found_id: int, body: dict):
        f = _get_found(found_id)
        try:
            st = FoundStatus(body.get("status"))
        except ValueError:
            raise HTTPException(400, "неизвестный статус")
        ctx.storage.set_found_status(f.id, st)
        return _found_out(_get_found(found_id))

    @app.delete("/api/found/{found_id}")
    def delete_found(found_id: int):
        f = _get_found(found_id)
        lot = ctx.storage.get_lot_by_found(f.id)
        if lot:
            raise HTTPException(409, "сначала удалите связанный лот")
        ctx.storage.delete_found(f.id)
        return {"ok": True}

    @app.post("/api/found/{found_id}/check")
    def check_found(found_id: int):
        f = _get_found(found_id)
        try:
            available = ctx.source(f.listing.source).is_available(f.listing.source_id)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"не удалось проверить: {e}")
        ctx.storage.set_found_availability(f.id, available)
        if available is False:
            ctx.storage.set_found_status(f.id, FoundStatus.SOLD)
        f = _get_found(found_id)
        return {"available": available, "checked_at": f.last_checked.isoformat() if f.last_checked else None}

    @app.post("/api/found/{found_id}/preview-lot")
    def preview_lot(found_id: int, body: Optional[dict] = None):
        f = _get_found(found_id)
        try:
            return publisher.preview(f, body or None).model_dump(mode="json")
        except KeyError:
            raise HTTPException(404, f"профиль {f.profile_id} не найден")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/found/{found_id}/create-lot", status_code=201)
    def create_lot(found_id: int, body: Optional[dict] = None):
        f = _get_found(found_id)
        body = body or {}
        publish = bool(body.pop("publish", False))
        if publish:
            _require_funpay()
        try:
            lot = publisher.create(f, body, publish=publish)
        except KeyError:
            raise HTTPException(404, f"профиль {f.profile_id} не найден")
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e))
        return _lot_out(lot)

    # --------------------------------------------------------------- lots
    @app.get("/api/lots")
    def list_lots(status: Optional[str] = None, profile_id: Optional[str] = None, limit: int = Query(500, le=5000)):
        statuses = [s for s in (status or "").split(",") if s] or None
        return [_lot_out(l) for l in ctx.storage.list_lots(status=statuses, profile_id=profile_id, limit=limit)]

    @app.get("/api/lots/{lot_id}")
    def get_lot(lot_id: int):
        return _lot_out(_get_lot(lot_id))

    @app.put("/api/lots/{lot_id}")
    def update_lot(lot_id: int, body: dict):
        lot = _get_lot(lot_id)
        try:
            return _lot_out(publisher.update(lot, body or {}, sync_source_price=True))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/lots/{lot_id}/publish")
    def publish_lot(lot_id: int):
        _require_funpay()
        lot = _get_lot(lot_id)
        try:
            lot = publisher.publish(lot)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        out = _lot_out(lot)
        if lot.status == LotStatus.ERROR:
            raise HTTPException(502, f"FunPay: {lot.error}")
        return out

    @app.post("/api/lots/{lot_id}/activate")
    def activate_lot(lot_id: int):
        return _lot_out(publisher.set_active(_get_lot(lot_id), True))

    @app.post("/api/lots/{lot_id}/deactivate")
    def deactivate_lot(lot_id: int):
        return _lot_out(publisher.set_active(_get_lot(lot_id), False, reason="вручную"))

    @app.post("/api/lots/{lot_id}/check")
    def check_lot(lot_id: int):
        return _lot_out(publisher.check_source(_get_lot(lot_id)))

    @app.delete("/api/lots/{lot_id}")
    def delete_lot(lot_id: int, funpay: bool = True):
        publisher.delete(_get_lot(lot_id), delete_on_funpay=funpay)
        return {"ok": True}

    # ------------------------------------------------------------ monitor
    @app.post("/api/monitor/run")
    def monitor_run():
        return monitor.run_once()

    # ------------------------------------------------------- funpay / lolz
    @app.get("/api/funpay/categories")
    def funpay_categories():
        _require_funpay()
        try:
            return ctx.funpay.categories()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"FunPay: {e}")

    @app.get("/api/funpay/filters")
    def funpay_filters(subcategory_id: int):
        _require_funpay()
        try:
            return ctx.funpay.list_filters(subcategory_id)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"FunPay: {e}")

    @app.get("/api/funpay/lot-form")
    def funpay_lot_form(subcategory_id: int):
        _require_funpay()
        try:
            form = ctx.funpay.get_lot_form(subcategory_id)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"FunPay: {e}")
        return form.get("schema", form) if isinstance(form, dict) else form

    @app.get("/api/lolz/categories")
    def lolz_categories():
        try:
            return ctx.lolz.categories()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Lolzteam: {e}")

    @app.get("/api/lolz/params")
    def lolz_params(category: str):
        _require_lolz()
        try:
            return ctx.lolz.category_params(category)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Lolzteam: {e}")

    # ------------------------------------------------------------- orders
    @app.get("/api/orders")
    def list_orders(status: Optional[str] = None, limit: int = Query(200, le=5000)):
        statuses = [x for x in (status or "").split(",") if x] or None
        if orders:
            return orders.list(status=statuses, limit=limit)
        return [o.model_dump(mode="json") for o in ctx.storage.list_orders(status=statuses, limit=limit)]

    @app.post("/api/orders/sync")
    def sync_orders():
        _require_funpay()
        if not orders:
            raise HTTPException(501, "сервис заказов недоступен")
        return orders.sync()

    @app.post("/api/orders/{order_id}/note")
    def order_note(order_id: int, body: dict):
        if not ctx.storage.get_order(order_id):
            raise HTTPException(404, "заказ не найден")
        ctx.storage.set_order_fields(order_id, note=str(body.get("note", ""))[:2000])
        return ctx.storage.get_order(order_id).model_dump(mode="json")

    # -------------------------------------------------------- raise / telegram
    @app.post("/api/raise")
    def raise_lots():
        _require_funpay()
        if not raiser:
            raise HTTPException(501, "сервис поднятия недоступен")
        return raiser.run()

    @app.post("/api/telegram/test")
    def telegram_test():
        n = ctx.notifier
        if not getattr(n, "enabled", False):
            raise HTTPException(400, "Telegram не настроен: включите уведомления, укажите токен бота и chat_id")
        info = n.test_connection()
        if info.get("ok"):
            n.send("✅ FunPay Searcher: уведомления подключены", kind="info")
        return info

    # ------------------------------------------------------------- export
    def _csv_response(rows: list[dict], columns: list[str], filename: str):
        import csv
        import io
        buf = io.StringIO()
        buf.write("\ufeff")  # BOM, чтобы Excel открыл UTF-8 корректно
        w = csv.DictWriter(buf, fieldnames=columns, delimiter=";", extrasaction="ignore")
        w.writeheader()
        def _safe(v):
            # защита от формул в Excel: текст из чужих объявлений не должен начинаться с = + - @
            if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
                return "'" + v
            return "" if v is None else v

        for r in rows:
            w.writerow({c: _safe(r.get(c)) for c in columns})
        from fastapi.responses import Response
        return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/export/found.csv")
    def export_found(profile_id: Optional[str] = None, status: Optional[str] = None):
        statuses = [x for x in (status or "").split(",") if x] or None
        rows = []
        for f in ctx.storage.list_found(profile_id=profile_id, status=statuses, limit=100000):
            l = f.listing
            rows.append({"id": f.id, "profile": _profile_names().get(f.profile_id, f.profile_id), "status": f.status.value,
                         "source": l.source, "title": l.title, "price": l.price, "suggested_price": f.suggested_price,
                         "region": l.region, "score": f.match.score, "highlights": ", ".join(f.match.highlights),
                         "seller": l.seller_name, "seller_url": l.seller_url, "url": l.url,
                         "available": f.available, "first_seen": f.first_seen.isoformat() if f.first_seen else ""})
        return _csv_response(rows, ["id", "profile", "status", "source", "title", "price", "suggested_price", "region",
                                    "score", "highlights", "seller", "seller_url", "url", "available", "first_seen"],
                             "found.csv")

    @app.get("/api/export/lots.csv")
    def export_lots(status: Optional[str] = None):
        statuses = [x for x in (status or "").split(",") if x] or None
        rows = []
        for l in ctx.storage.list_lots(status=statuses, limit=100000):
            rows.append({"id": l.id, "status": l.status.value, "title": l.title_ru, "price": l.price,
                         "source_price": l.source_price, "margin": round(l.price - l.source_price, 2),
                         "funpay_url": l.funpay_url, "source_url": l.source_url, "seller_url": l.seller_url,
                         "source_available": l.source_available,
                         "created_at": l.created_at.isoformat() if l.created_at else ""})
        return _csv_response(rows, ["id", "status", "title", "price", "source_price", "margin", "funpay_url",
                                    "source_url", "seller_url", "source_available", "created_at"], "lots.csv")

    @app.get("/api/export/orders.csv")
    def export_orders():
        rows = [{**o.model_dump(mode="json")} for o in ctx.storage.list_orders(limit=100000)]
        return _csv_response(rows, ["id", "funpay_order_id", "status", "title", "price", "buyer_name", "buyer_url",
                                    "order_url", "source_url", "source_price", "order_date", "note"], "orders.csv")

    # ------------------------------------------------------------- market
    @app.get("/api/found/{found_id}/market")
    def found_market(found_id: int):
        """Статистика цен по похожим находкам (тот же профиль): помогает оценить, не завышена ли цена."""
        import statistics
        f = _get_found(found_id)
        same = [x for x in ctx.storage.list_found(profile_id=f.profile_id, limit=100000)
                if x.match.matched and x.listing.price > 0]
        by_source: dict[str, list[float]] = {}
        for x in same:
            by_source.setdefault(x.listing.source, []).append(x.listing.price)

        def _stats(prices: list[float]) -> Optional[dict]:
            if not prices:
                return None
            ps = sorted(prices)
            return {"count": len(ps), "min": ps[0], "median": statistics.median(ps),
                    "avg": round(sum(ps) / len(ps), 2), "max": ps[-1]}

        all_prices = [x.listing.price for x in same]
        below = sum(1 for p_ in all_prices if p_ < f.listing.price)
        return {
            "price": f.listing.price,
            "suggested_price": f.suggested_price,
            "all": _stats(all_prices),
            "by_source": {k: _stats(v) for k, v in by_source.items()},
            "percentile": round(100 * below / len(all_prices)) if all_prices else None,
            "cheaper_than_suggested": sum(1 for p_ in all_prices if f.suggested_price and p_ < f.suggested_price),
        }

    # ------------------------------------------------------- seller / history
    @app.get("/api/funpay/seller")
    def funpay_seller(seller_id: Optional[str] = None, url: Optional[str] = None):
        """Карточка продавца FunPay: имя, отзывы, его лоты (для оценки надёжности исходника)."""
        _require_funpay()
        sid = seller_id
        if not sid and url:
            import re
            m = re.search(r"/users/(\d+)", url)
            sid = m.group(1) if m else None
        if not sid:
            raise HTTPException(400, "укажите seller_id или url продавца FunPay")
        try:
            info = ctx.funpay.get_seller(sid)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"FunPay: {e}")
        if not info:
            raise HTTPException(404, "продавец не найден")
        lots = info.get("lots") or []
        info["lots"] = [l.model_dump(mode="json") if hasattr(l, "model_dump") else l for l in lots][:200]
        info["lots_count"] = len(lots)
        return info

    @app.get("/api/found/{found_id}/history")
    def found_history(found_id: int):
        f = _get_found(found_id)
        if repricer:
            return repricer.price_trend(f.id)
        hist = ctx.storage.price_history(f.id)
        first = hist[0]["price"] if hist else f.listing.price
        last = hist[-1]["price"] if hist else f.listing.price
        return {"history": hist, "first": first, "last": last,
                "change_percent": round((last - first) / first * 100, 1) if first else 0.0}

    # ------------------------------------------------------ reprice / autoreply
    @app.post("/api/reprice")
    def reprice_now():
        if not repricer:
            raise HTTPException(501, "сервис репрайсинга недоступен")
        return repricer.run()

    @app.post("/api/autoreply/run")
    def autoreply_run():
        _require_funpay()
        if not autoreply:
            raise HTTPException(501, "автоответчик недоступен")
        return autoreply.run_once()

    @app.post("/api/autoreply/preview")
    def autoreply_preview(body: dict):
        if not autoreply:
            raise HTTPException(501, "автоответчик недоступен")
        return {"reply": autoreply.preview_reply(str(body.get("text", "")))}

    # --------------------------------------------------------------- chat
    def _chat():
        _require_funpay()
        from .sources.funpay_chat import FunPayChat
        return FunPayChat(ctx.funpay)

    @app.get("/api/chat")
    def chat_list():
        try:
            return _chat().list_chats()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"FunPay: {e}")

    @app.get("/api/chat/{chat_id}/history")
    def chat_history(chat_id: int):
        try:
            return _chat().get_history(chat_id)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"FunPay: {e}")

    @app.post("/api/chat/{chat_id}/send")
    def chat_send(chat_id: int, body: dict):
        text = str((body or {}).get("text", "")).strip()
        if not text:
            raise HTTPException(400, "пустое сообщение")
        try:
            _chat().send_message(chat_id, text)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"FunPay: {e}")
        ctx.log("chat", f"отправлено сообщение в чат {chat_id}: {text[:60]}")
        return {"ok": True}

    # ----------------------------------------------------------- utilities
    @app.get("/api/events")
    def events(limit: int = Query(200, le=2000)):
        return ctx.storage.events(limit)

    @app.post("/api/pricing/preview")
    def pricing_preview(body: PricingPreview):
        try:
            return {"price": calculate_price(body.price, body.pricing)}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"ошибка формулы: {e}")

    @app.post("/api/matching/test")
    def matching_test(body: MatchingTest):
        from .matching import evaluate
        criteria = body.criteria
        if criteria is None and body.profile is not None:
            criteria = body.profile.criteria
        if criteria is None and body.profile_id:
            try:
                criteria = ctx.profiles.get(body.profile_id).criteria
            except KeyError:
                raise HTTPException(404, "профиль не найден")
        if criteria is None:
            raise HTTPException(400, "нужен profile, profile_id или criteria")
        listing = Listing(source="funpay", source_id="test", url="", title=body.text, price=body.price,
                          region=body.region, attributes=body.attributes)
        result: MatchResult = evaluate(listing, criteria)
        out = result.model_dump(mode="json")
        pricing = body.profile.pricing if body.profile else None
        if pricing and result.matched:
            try:
                out["suggested_price"] = calculate_price(body.price, pricing)
            except Exception:  # noqa: BLE001
                pass
        return out

    # ------------------------------------------------------------- static
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(STATIC_DIR / "index.html")

    return app


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
