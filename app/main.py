"""HTTP API + раздача веб-интерфейса (FastAPI)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .models import Criteria, Found, FoundStatus, Listing, LotStatus, MatchResult, OurLot, PricingRule, Profile
from .pricing import calculate_price
from .services.context import AppContext
from .services.monitor import MonitorService
from .services.publisher import PublisherService
from .services.search import SearchService
from .settings import Settings

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
log = logging.getLogger("app")


def create_app(ctx: Optional[AppContext] = None, start_monitor: bool = True) -> FastAPI:
    ctx = ctx or AppContext()
    search = SearchService(ctx)
    publisher = PublisherService(ctx)
    monitor = MonitorService(ctx, publisher, search)

    app = FastAPI(title="FunPay Searcher", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.state.ctx, app.state.search, app.state.publisher, app.state.monitor = ctx, search, publisher, monitor

    @app.on_event("startup")
    def _startup():
        ctx.log("app", f"запуск FunPay Searcher v{__version__}")
        if start_monitor:
            monitor.start()

    @app.on_event("shutdown")
    def _shutdown():
        monitor.stop()

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
        }

    # ----------------------------------------------------------- settings
    @app.get("/api/settings")
    def get_settings():
        return ctx.settings.masked()

    @app.put("/api/settings")
    def put_settings(body: dict):
        current = ctx.settings.model_dump(mode="json")
        merged = _deep_merge(current, body or {})
        for sec, key in (("funpay", "golden_key"), ("lolz", "token")):
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
    class SearchBody(BaseModel):
        profile_ids: Optional[list[str]] = None
        sources: Optional[list[str]] = None

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
        order_sql = {"score": "score DESC, last_seen DESC", "price_asc": "price ASC", "price_desc": "price DESC",
                     "recent": "last_seen DESC", "first_seen": "first_seen DESC"}.get(order, "score DESC, last_seen DESC")
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
            return _lot_out(publisher.update(lot, body or {}))
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

    # ----------------------------------------------------------- utilities
    @app.get("/api/events")
    def events(limit: int = Query(200, le=2000)):
        return ctx.storage.events(limit)

    class PricingPreview(BaseModel):
        price: float
        pricing: PricingRule

    @app.post("/api/pricing/preview")
    def pricing_preview(body: PricingPreview):
        try:
            return {"price": calculate_price(body.price, body.pricing)}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"ошибка формулы: {e}")

    class MatchingTest(BaseModel):
        profile: Optional[Profile] = None
        profile_id: Optional[str] = None
        criteria: Optional[Criteria] = None
        text: str = ""
        price: float = 0
        region: Optional[str] = None
        attributes: dict[str, Any] = {}

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
