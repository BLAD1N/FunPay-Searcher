"""Публикация наших лотов на FunPay по найденным объявлениям."""
from __future__ import annotations

from typing import Optional

from ..models import Found, FoundStatus, LotStatus, OurLot, Profile, utcnow
from ..pricing import calculate_price
from ..templating import render_lot
from .context import AppContext

EDITABLE = ("price", "title_ru", "title_en", "description_ru", "description_en", "fields")


class PublisherService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    # --------------------------------------------------------- helpers
    def _profile(self, profile_id: str) -> Profile:
        return self.ctx.profiles.get(profile_id)

    def resolve_subcategory(self, found: Found, profile: Profile) -> Optional[int]:
        """Куда публиковать: явный узел из шаблона -> узел исходника (FunPay) -> узел поиска профиля."""
        if profile.lot_template.funpay_subcategory_id:
            return int(profile.lot_template.funpay_subcategory_id)
        if found.listing.source == "funpay":
            sid = found.listing.attributes.get("subcategory_id")
            if sid:
                return int(sid)
        try:
            cfg = profile.funpay()
        except Exception:  # noqa: BLE001
            return None
        if cfg.subcategory_id:
            return int(cfg.subcategory_id)
        if cfg.subcategory_ids:
            return int(cfg.subcategory_ids[0])
        if cfg.game_query and self.ctx.settings.funpay.golden_key:
            ids = self.ctx.funpay.resolve_subcategory_ids(cfg)
            if ids:
                return int(ids[0])
        return None

    # --------------------------------------------------------- preview
    def preview(self, found: Found, overrides: Optional[dict] = None) -> OurLot:
        profile = self._profile(found.profile_id)
        price = found.suggested_price or calculate_price(found.listing.price, profile.pricing)
        rendered = render_lot(found.listing, found.match, profile, price)
        lot = OurLot(
            found_id=found.id or 0, profile_id=profile.id,
            subcategory_id=self.resolve_subcategory(found, profile),
            title_ru=rendered.get("title_ru", ""), title_en=rendered.get("title_en", ""),
            description_ru=rendered.get("description_ru", ""), description_en=rendered.get("description_en", ""),
            price=float(rendered.get("price", price)), source_price=found.listing.price,
            source_url=found.listing.url, seller_url=found.listing.seller_url,
            fields=dict(rendered.get("fields") or {}), status=LotStatus.DRAFT,
            source_available=found.available, source_checked_at=found.last_checked,
        )
        self._apply_overrides(lot, overrides)
        return lot

    def _apply_overrides(self, lot: OurLot, overrides: Optional[dict]) -> None:
        if not overrides:
            return
        for key in EDITABLE:
            if key in overrides and overrides[key] is not None:
                value = overrides[key]
                if key == "price":
                    value = float(value)
                    if value <= 0:
                        raise ValueError("цена должна быть больше нуля")
                if key == "fields" and not isinstance(value, dict):
                    raise ValueError("fields должен быть объектом")
                setattr(lot, key, value)
        if "subcategory_id" in overrides and overrides["subcategory_id"]:
            lot.subcategory_id = int(overrides["subcategory_id"])

    # ---------------------------------------------------------- create
    def create(self, found: Found, overrides: Optional[dict] = None, publish: bool = False) -> OurLot:
        existing = self.ctx.storage.get_lot_by_found(found.id)
        if existing and existing.status in (LotStatus.ACTIVE, LotStatus.DRAFT, LotStatus.DEACTIVATED):
            lot = existing
            self._apply_overrides(lot, overrides)
        else:
            lot = self.preview(found, overrides)
        lot = self.ctx.storage.save_lot(lot)
        self.ctx.log("lots", f"черновик лота #{lot.id} создан: {lot.title_ru} — {lot.price:.0f}",
                     data={"found_id": found.id})
        if publish:
            lot = self.publish(lot)
        return lot

    # --------------------------------------------------------- publish
    def _ensure_lot_id(self, lot: OurLot) -> int:
        """Вернуть id лота на FunPay; если он не был определён при создании — найти по названию."""
        if lot.funpay_lot_id:
            return int(lot.funpay_lot_id)
        if lot.subcategory_id and hasattr(self.ctx.funpay, "list_my_lots"):
            for mine in self.ctx.funpay.list_my_lots(lot.subcategory_id):
                if (mine.title or "").strip() == (lot.title_ru or "").strip():
                    lot.funpay_lot_id = int(mine.source_id)
                    lot.funpay_url = mine.url
                    self.ctx.storage.save_lot(lot)
                    return lot.funpay_lot_id
        raise RuntimeError("не удалось определить id лота на FunPay — измените лот вручную на сайте")

    def publish(self, lot: OurLot) -> OurLot:
        if not self.ctx.settings.funpay.golden_key:
            raise RuntimeError("не задан golden_key FunPay в настройках")
        if lot.status == LotStatus.ACTIVE and not lot.funpay_lot_id:
            raise RuntimeError("лот уже опубликован, но его id неизвестен — повторная публикация создаст дубль")
        if not lot.subcategory_id:
            raise RuntimeError("не определена категория FunPay для публикации (укажите funpay_subcategory_id в шаблоне лота)")
        profile = self._profile(lot.profile_id)
        tpl = profile.lot_template
        try:
            if lot.funpay_lot_id:
                self.ctx.funpay.update_lot(
                    lot.funpay_lot_id, lot.subcategory_id,
                    title_ru=lot.title_ru, title_en=lot.title_en, description_ru=lot.description_ru,
                    description_en=lot.description_en, price=lot.price, active=True, extra_fields=lot.fields)
            else:
                result = self.ctx.funpay.create_lot(
                    lot.subcategory_id, title_ru=lot.title_ru, title_en=lot.title_en,
                    description_ru=lot.description_ru, description_en=lot.description_en, price=lot.price,
                    amount=tpl.amount, active=tpl.active, deactivate_after_sale=tpl.deactivate_after_sale,
                    extra_fields=lot.fields)
                lot.funpay_lot_id = result.get("lot_id")
                lot.funpay_url = result.get("url") or (
                    f"https://funpay.com/lots/offer?id={lot.funpay_lot_id}" if lot.funpay_lot_id
                    else f"https://funpay.com/lots/{lot.subcategory_id}/trade")
            lot.status = LotStatus.ACTIVE
            lot.error = None
            if not lot.funpay_lot_id:
                # лот создан, но его id не найден в списке наших лотов — предупреждаем, повторная публикация запрещена
                lot.error = ("лот создан на FunPay, но его id не удалось определить автоматически; "
                             "проверьте список лотов на FunPay. Повторно не публикуйте — будет дубль.")
                self.ctx.log("lots", f"лот #{lot.id}: {lot.error}", level="warning")
            self.ctx.storage.set_found_status(lot.found_id, FoundStatus.PUBLISHED)
            self.ctx.log("lots", f"лот #{lot.id} опубликован на FunPay (id {lot.funpay_lot_id}) за {lot.price:.0f}")
        except Exception as e:  # noqa: BLE001
            lot.status = LotStatus.ERROR
            lot.error = str(e)
            self.ctx.log("lots", f"ошибка публикации лота #{lot.id}: {e}", level="error")
        return self.ctx.storage.save_lot(lot)

    def update(self, lot: OurLot, changes: dict) -> OurLot:
        self._apply_overrides(lot, changes)
        # снятый лот не трогаем на FunPay: правки уедут при активации (set_active отправляет все поля)
        if lot.status == LotStatus.ACTIVE and lot.funpay_lot_id:
            try:
                self.ctx.funpay.update_lot(
                    lot.funpay_lot_id, lot.subcategory_id, title_ru=lot.title_ru, title_en=lot.title_en,
                    description_ru=lot.description_ru, description_en=lot.description_en, price=lot.price,
                    active=True, extra_fields=lot.fields)
                lot.error = None
                self.ctx.log("lots", f"лот #{lot.id} обновлён на FunPay")
            except Exception as e:  # noqa: BLE001
                lot.error = str(e)
                self.ctx.log("lots", f"ошибка обновления лота #{lot.id}: {e}", level="error")
        return self.ctx.storage.save_lot(lot)

    def set_active(self, lot: OurLot, active: bool, reason: str = "") -> OurLot:
        if lot.status in (LotStatus.ACTIVE, LotStatus.DEACTIVATED, LotStatus.SOLD) or lot.funpay_lot_id:
            try:
                lot_id = self._ensure_lot_id(lot)
                if active:
                    # при активации отправляем все поля: правки, сделанные в снятом лоте, должны попасть на FunPay
                    self.ctx.funpay.update_lot(
                        lot_id, lot.subcategory_id, title_ru=lot.title_ru, title_en=lot.title_en,
                        description_ru=lot.description_ru, description_en=lot.description_en, price=lot.price,
                        active=True, extra_fields=lot.fields)
                else:
                    self.ctx.funpay.set_lot_active(lot_id, lot.subcategory_id, False)
                lot.error = None
            except Exception as e:  # noqa: BLE001
                lot.error = str(e)
                self.ctx.log("lots", f"не удалось {'активировать' if active else 'снять'} лот #{lot.id}: {e}",
                             level="error")
                return self.ctx.storage.save_lot(lot)
        lot.status = LotStatus.ACTIVE if active else LotStatus.DEACTIVATED
        self.ctx.log("lots", f"лот #{lot.id} {'активирован' if active else 'снят с продажи'}"
                             + (f": {reason}" if reason else ""))
        return self.ctx.storage.save_lot(lot)

    def delete(self, lot: OurLot, delete_on_funpay: bool = True) -> None:
        if delete_on_funpay and lot.funpay_lot_id and lot.subcategory_id and lot.status == LotStatus.ACTIVE:
            try:
                self.ctx.funpay.set_lot_active(lot.funpay_lot_id, lot.subcategory_id, False)
            except Exception as e:  # noqa: BLE001
                self.ctx.log("lots", f"не удалось снять лот #{lot.id} перед удалением: {e}", level="warning")
        self.ctx.storage.delete_lot(lot.id)
        self.ctx.log("lots", f"лот #{lot.id} удалён из базы")

    # ----------------------------------------------- source availability
    def check_source(self, lot: OurLot) -> OurLot:
        found = self.ctx.storage.get_found(lot.found_id)
        available: Optional[bool] = None
        if found:
            try:
                available = self.ctx.source(found.listing.source).is_available(found.listing.source_id)
            except Exception as e:  # noqa: BLE001
                self.ctx.log("monitor", f"проверка исходника лота #{lot.id}: {e}", level="warning")
            self.ctx.storage.set_found_availability(found.id, available)
        lot.source_available = available
        lot.source_checked_at = utcnow()
        lot = self.ctx.storage.save_lot(lot)
        if available is False:
            if found:
                self.ctx.storage.set_found_status(found.id, FoundStatus.SOLD)
            if lot.status == LotStatus.ACTIVE and self.ctx.settings.monitor.auto_deactivate:
                lot = self.set_active(lot, False, reason="исходное объявление продано или снято")
            self.ctx.log("monitor", f"исходник лота #{lot.id} недоступен: {lot.source_url}", level="warning")
            try:
                self.ctx.notify(self.ctx.notifier.format_source_sold(lot), kind="sold")
            except Exception as e:  # noqa: BLE001
                self.ctx.log("monitor", f"уведомление о продаже исходника: {e}", level="warning")
        return lot
