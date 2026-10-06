import tempfile
from pathlib import Path

from app.models import Found, FoundStatus, Listing, LotStatus, MatchResult, OurLot
from app.storage import Storage


def _listing(i="1", price=100.0):
    return Listing(source="funpay", source_id=i, url=f"https://funpay.com/lots/offer?id={i}", title="t", price=price)


def test_found_upsert_keeps_user_status():
    s = Storage(Path(tempfile.mkdtemp()) / "t.db")
    f, new = s.upsert_found(Found(profile_id="p", listing=_listing(), match=MatchResult(matched=True, score=2),
                                  suggested_price=200, status=FoundStatus.CANDIDATE))
    assert new and f.id and f.available is True
    s.set_found_status(f.id, FoundStatus.PUBLISHED)
    f2, new2 = s.upsert_found(Found(profile_id="p", listing=_listing(price=120), match=MatchResult(matched=True),
                                    status=FoundStatus.CANDIDATE))
    assert not new2 and f2.id == f.id and f2.status == FoundStatus.PUBLISHED and f2.listing.price == 120
    assert s.count_found() == {"published": 1}
    assert s.list_found(status=["published"])[0].id == f.id
    s.set_found_availability(f.id, False)
    assert s.get_found(f.id).available is False and s.get_found(f.id).last_checked


def test_lots_roundtrip():
    s = Storage(Path(tempfile.mkdtemp()) / "t.db")
    f, _ = s.upsert_found(Found(profile_id="p", listing=_listing(), match=MatchResult(matched=True)))
    lot = s.save_lot(OurLot(found_id=f.id, profile_id="p", price=200, source_price=100, source_url="u",
                            fields={"fields[server]": "ru"}))
    assert lot.id and s.get_lot_by_found(f.id).fields == {"fields[server]": "ru"}
    lot.status = LotStatus.ACTIVE; lot.funpay_lot_id = 5
    s.save_lot(lot)
    assert s.list_lots(status=["active"])[0].funpay_lot_id == 5
    s.delete_lot(lot.id)
    assert s.list_lots() == []


def test_events_log():
    s = Storage(Path(tempfile.mkdtemp()) / "t.db")
    s.log("k", "m", data={"a": 1})
    e = s.events(1)[0]
    assert e["message"] == "m" and e["data"] == {"a": 1}


def test_price_history_tracks_changes():
    s = Storage(Path(tempfile.mkdtemp()) / "t.db")
    f, _ = s.upsert_found(Found(profile_id="p", listing=_listing(price=100), match=MatchResult(matched=True)))
    s.upsert_found(Found(profile_id="p", listing=_listing(price=100), match=MatchResult(matched=True)))
    s.upsert_found(Found(profile_id="p", listing=_listing(price=80), match=MatchResult(matched=True)))
    hist = s.price_history(f.id)
    assert [h["price"] for h in hist] == [100, 80]
    s.update_found_suggested_price(f.id, 150)
    assert s.get_found(f.id).suggested_price == 150
    s.delete_found(f.id)
    assert s.price_history(f.id) == []
