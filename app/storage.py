"""SQLite-хранилище найденных объявлений и наших лотов."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .models import Found, FoundStatus, Listing, LotStatus, MatchResult, Order, OrderStatus, OurLot, utcnow
from .settings import DATA_DIR

DB_FILE = DATA_DIR / "searcher.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS found (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT, price REAL NOT NULL, currency TEXT,
    seller_name TEXT, seller_url TEXT, region TEXT,
    listing_json TEXT NOT NULL,
    match_json TEXT NOT NULL,
    score REAL DEFAULT 0,
    suggested_price REAL,
    status TEXT NOT NULL DEFAULT 'new',
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    available INTEGER, last_checked TEXT,
    UNIQUE(profile_id, source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_found_status ON found(status);
CREATE INDEX IF NOT EXISTS idx_found_profile ON found(profile_id);

CREATE TABLE IF NOT EXISTS lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_id INTEGER NOT NULL REFERENCES found(id),
    profile_id TEXT NOT NULL,
    funpay_lot_id INTEGER, funpay_url TEXT, subcategory_id INTEGER,
    title_ru TEXT, title_en TEXT, description_ru TEXT, description_en TEXT,
    price REAL NOT NULL, source_price REAL NOT NULL, source_url TEXT NOT NULL, seller_url TEXT,
    fields_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft', error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    source_available INTEGER, source_checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_lots_status ON lots(status);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    funpay_order_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL, title TEXT, subcategory_name TEXT, price REAL NOT NULL, currency TEXT,
    buyer_name TEXT, buyer_id TEXT, buyer_url TEXT, order_url TEXT, order_date TEXT,
    lot_id INTEGER, source_url TEXT, source_price REAL,
    first_seen TEXT NOT NULL, updated_at TEXT NOT NULL, notified INTEGER DEFAULT 0, note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_id INTEGER NOT NULL REFERENCES found(id) ON DELETE CASCADE,
    ts TEXT NOT NULL, price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_found ON price_history(found_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, data_json TEXT
);
"""


def _dt(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v else None


def _pdt(v: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(v) if v else None


class Storage:
    def __init__(self, path: Path = DB_FILE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------ found
    def _row_to_found(self, r: sqlite3.Row) -> Found:
        return Found(
            id=r["id"], profile_id=r["profile_id"],
            listing=Listing.model_validate_json(r["listing_json"]),
            match=MatchResult.model_validate_json(r["match_json"]),
            suggested_price=r["suggested_price"], status=FoundStatus(r["status"]),
            first_seen=_pdt(r["first_seen"]), last_seen=_pdt(r["last_seen"]),
            available=None if r["available"] is None else bool(r["available"]),
            last_checked=_pdt(r["last_checked"]),
        )

    def upsert_found(self, found: Found) -> tuple[Found, bool]:
        """Вставить/обновить. Возвращает (запись, is_new)."""
        with self._lock:
            l = found.listing
            row = self._conn.execute(
                "SELECT * FROM found WHERE profile_id=? AND source=? AND source_id=?",
                (found.profile_id, l.source, l.source_id)).fetchone()
            now = utcnow()
            if row:
                status = row["status"]
                # если пользователь скрыл/опубликовал — статус не трогаем, обновляем цену и текст
                if status in (FoundStatus.NEW.value, FoundStatus.CANDIDATE.value, FoundStatus.REJECTED.value):
                    status = found.status.value
                if abs(float(row["price"]) - float(l.price)) > 0.009:
                    self._conn.execute("INSERT INTO price_history (found_id, ts, price) VALUES (?,?,?)",
                                       (row["id"], _dt(now), l.price))
                self._conn.execute(
                    """UPDATE found SET url=?, title=?, price=?, currency=?, seller_name=?, seller_url=?, region=?,
                       listing_json=?, match_json=?, score=?, suggested_price=?, status=?, last_seen=?, available=1
                       WHERE id=?""",
                    (l.url, l.title, l.price, l.currency, l.seller_name, l.seller_url, l.region,
                     l.model_dump_json(), found.match.model_dump_json(), found.match.score,
                     found.suggested_price, status, _dt(now), row["id"]))
                self._conn.commit()
                return self.get_found(row["id"]), False
            cur = self._conn.execute(
                """INSERT INTO found (profile_id, source, source_id, url, title, price, currency, seller_name, seller_url,
                   region, listing_json, match_json, score, suggested_price, status, first_seen, last_seen, available)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (found.profile_id, l.source, l.source_id, l.url, l.title, l.price, l.currency, l.seller_name,
                 l.seller_url, l.region, l.model_dump_json(), found.match.model_dump_json(), found.match.score,
                 found.suggested_price, found.status.value, _dt(now), _dt(now)))
            self._conn.execute("INSERT INTO price_history (found_id, ts, price) VALUES (?,?,?)",
                               (cur.lastrowid, _dt(now), l.price))
            self._conn.commit()
            return self.get_found(cur.lastrowid), True

    def get_found(self, found_id: int) -> Optional[Found]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM found WHERE id=?", (found_id,)).fetchone()
            return self._row_to_found(r) if r else None

    def list_found(self, profile_id: Optional[str] = None, status: Optional[Iterable[str]] = None,
                   source: Optional[str] = None, limit: int = 500, offset: int = 0,
                   order: str = "score DESC, last_seen DESC") -> list[Found]:
        q = "SELECT * FROM found WHERE 1=1"
        args: list = []
        if profile_id:
            q += " AND profile_id=?"; args.append(profile_id)
        if status:
            st = list(status)
            q += f" AND status IN ({','.join('?' * len(st))})"; args.extend(st)
        if source:
            q += " AND source=?"; args.append(source)
        allowed = {"score DESC, last_seen DESC", "price ASC", "price DESC", "last_seen DESC", "first_seen DESC",
                   "suggested_price DESC", "suggested_price ASC"}
        q += f" ORDER BY {order if order in allowed else 'score DESC, last_seen DESC'} LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        with self._lock:
            return [self._row_to_found(r) for r in self._conn.execute(q, args).fetchall()]

    def count_found(self, profile_id: Optional[str] = None) -> dict[str, int]:
        q = "SELECT status, COUNT(*) c FROM found"
        args: list = []
        if profile_id:
            q += " WHERE profile_id=?"; args.append(profile_id)
        q += " GROUP BY status"
        with self._lock:
            return {r["status"]: r["c"] for r in self._conn.execute(q, args).fetchall()}

    def set_found_status(self, found_id: int, status: FoundStatus) -> None:
        with self._lock:
            self._conn.execute("UPDATE found SET status=? WHERE id=?", (status.value, found_id))
            self._conn.commit()

    def set_found_availability(self, found_id: int, available: Optional[bool]) -> None:
        with self._lock:
            self._conn.execute("UPDATE found SET available=?, last_checked=? WHERE id=?",
                               (None if available is None else int(available), _dt(utcnow()), found_id))
            self._conn.commit()

    def delete_found(self, found_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM price_history WHERE found_id=?", (found_id,))
            self._conn.execute("DELETE FROM found WHERE id=?", (found_id,))
            self._conn.commit()

    def price_history(self, found_id: int, limit: int = 100) -> list[dict]:
        """История цены исходного объявления: [{ts, price}] от старых к новым."""
        with self._lock:
            rows = self._conn.execute("SELECT ts, price FROM price_history WHERE found_id=? ORDER BY id DESC LIMIT ?",
                                      (found_id, limit)).fetchall()
            return [{"ts": r["ts"], "price": r["price"]} for r in reversed(rows)]

    def update_found_suggested_price(self, found_id: int, suggested_price: Optional[float]) -> None:
        with self._lock:
            self._conn.execute("UPDATE found SET suggested_price=? WHERE id=?", (suggested_price, found_id))
            self._conn.commit()

    # ------------------------------------------------------------- lots
    def _row_to_lot(self, r: sqlite3.Row) -> OurLot:
        return OurLot(
            id=r["id"], found_id=r["found_id"], profile_id=r["profile_id"],
            funpay_lot_id=r["funpay_lot_id"], funpay_url=r["funpay_url"], subcategory_id=r["subcategory_id"],
            title_ru=r["title_ru"] or "", title_en=r["title_en"] or "",
            description_ru=r["description_ru"] or "", description_en=r["description_en"] or "",
            price=r["price"], source_price=r["source_price"], source_url=r["source_url"], seller_url=r["seller_url"],
            fields=json.loads(r["fields_json"] or "{}"), status=LotStatus(r["status"]), error=r["error"],
            created_at=_pdt(r["created_at"]), updated_at=_pdt(r["updated_at"]),
            source_available=None if r["source_available"] is None else bool(r["source_available"]),
            source_checked_at=_pdt(r["source_checked_at"]),
        )

    def save_lot(self, lot: OurLot) -> OurLot:
        with self._lock:
            lot.updated_at = utcnow()
            if lot.id is None:
                cur = self._conn.execute(
                    """INSERT INTO lots (found_id, profile_id, funpay_lot_id, funpay_url, subcategory_id, title_ru, title_en,
                       description_ru, description_en, price, source_price, source_url, seller_url, fields_json, status, error,
                       created_at, updated_at, source_available, source_checked_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (lot.found_id, lot.profile_id, lot.funpay_lot_id, lot.funpay_url, lot.subcategory_id, lot.title_ru,
                     lot.title_en, lot.description_ru, lot.description_en, lot.price, lot.source_price, lot.source_url,
                     lot.seller_url, json.dumps(lot.fields, ensure_ascii=False), lot.status.value, lot.error,
                     _dt(lot.created_at), _dt(lot.updated_at),
                     None if lot.source_available is None else int(lot.source_available), _dt(lot.source_checked_at)))
                lot.id = cur.lastrowid
            else:
                self._conn.execute(
                    """UPDATE lots SET funpay_lot_id=?, funpay_url=?, subcategory_id=?, title_ru=?, title_en=?,
                       description_ru=?, description_en=?, price=?, source_price=?, source_url=?, seller_url=?, fields_json=?,
                       status=?, error=?, updated_at=?, source_available=?, source_checked_at=? WHERE id=?""",
                    (lot.funpay_lot_id, lot.funpay_url, lot.subcategory_id, lot.title_ru, lot.title_en,
                     lot.description_ru, lot.description_en, lot.price, lot.source_price, lot.source_url, lot.seller_url,
                     json.dumps(lot.fields, ensure_ascii=False), lot.status.value, lot.error, _dt(lot.updated_at),
                     None if lot.source_available is None else int(lot.source_available), _dt(lot.source_checked_at),
                     lot.id))
            self._conn.commit()
            return lot

    def get_lot(self, lot_id: int) -> Optional[OurLot]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
            return self._row_to_lot(r) if r else None

    def get_lot_by_found(self, found_id: int) -> Optional[OurLot]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM lots WHERE found_id=? ORDER BY id DESC", (found_id,)).fetchone()
            return self._row_to_lot(r) if r else None

    def list_lots(self, status: Optional[Iterable[str]] = None, profile_id: Optional[str] = None,
                  limit: int = 500) -> list[OurLot]:
        q = "SELECT * FROM lots WHERE 1=1"
        args: list = []
        if status:
            st = list(status)
            q += f" AND status IN ({','.join('?' * len(st))})"; args.extend(st)
        if profile_id:
            q += " AND profile_id=?"; args.append(profile_id)
        q += " ORDER BY updated_at DESC LIMIT ?"; args.append(limit)
        with self._lock:
            return [self._row_to_lot(r) for r in self._conn.execute(q, args).fetchall()]

    def delete_lot(self, lot_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM lots WHERE id=?", (lot_id,))
            self._conn.commit()

    # ----------------------------------------------------------- orders
    def _row_to_order(self, r: sqlite3.Row) -> Order:
        return Order(
            id=r["id"], funpay_order_id=r["funpay_order_id"], status=OrderStatus(r["status"]), title=r["title"] or "",
            subcategory_name=r["subcategory_name"], price=r["price"], currency=r["currency"] or "RUB",
            buyer_name=r["buyer_name"], buyer_id=r["buyer_id"], buyer_url=r["buyer_url"], order_url=r["order_url"] or "",
            order_date=_pdt(r["order_date"]), lot_id=r["lot_id"], source_url=r["source_url"],
            source_price=r["source_price"], first_seen=_pdt(r["first_seen"]), updated_at=_pdt(r["updated_at"]),
            notified=bool(r["notified"]), note=r["note"] or "",
        )

    def upsert_order(self, order: Order) -> tuple[Order, bool]:
        """Вставить/обновить заказ по funpay_order_id. Возвращает (заказ, is_new)."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM orders WHERE funpay_order_id=?", (order.funpay_order_id,)).fetchone()
            now = utcnow()
            if row:
                self._conn.execute(
                    """UPDATE orders SET status=?, title=?, subcategory_name=?, price=?, currency=?, buyer_name=?, buyer_id=?,
                       buyer_url=?, order_url=?, order_date=?, lot_id=COALESCE(?, lot_id), source_url=COALESCE(?, source_url),
                       source_price=COALESCE(?, source_price), updated_at=? WHERE id=?""",
                    (order.status.value, order.title, order.subcategory_name, order.price, order.currency,
                     order.buyer_name, order.buyer_id, order.buyer_url, order.order_url, _dt(order.order_date),
                     order.lot_id, order.source_url, order.source_price, _dt(now), row["id"]))
                self._conn.commit()
                return self.get_order(row["id"]), False
            cur = self._conn.execute(
                """INSERT INTO orders (funpay_order_id, status, title, subcategory_name, price, currency, buyer_name, buyer_id,
                   buyer_url, order_url, order_date, lot_id, source_url, source_price, first_seen, updated_at, notified, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order.funpay_order_id, order.status.value, order.title, order.subcategory_name, order.price,
                 order.currency, order.buyer_name, order.buyer_id, order.buyer_url, order.order_url,
                 _dt(order.order_date), order.lot_id, order.source_url, order.source_price, _dt(now), _dt(now),
                 int(order.notified), order.note))
            self._conn.commit()
            return self.get_order(cur.lastrowid), True

    def get_order(self, order_id: int) -> Optional[Order]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            return self._row_to_order(r) if r else None

    def list_orders(self, status: Optional[Iterable[str]] = None, limit: int = 500) -> list[Order]:
        q = "SELECT * FROM orders WHERE 1=1"
        args: list = []
        if status:
            st = list(status)
            q += f" AND status IN ({','.join('?' * len(st))})"; args.extend(st)
        q += " ORDER BY COALESCE(order_date, first_seen) DESC LIMIT ?"; args.append(limit)
        with self._lock:
            return [self._row_to_order(r) for r in self._conn.execute(q, args).fetchall()]

    def set_order_fields(self, order_id: int, **fields) -> None:
        allowed = {"notified", "note", "lot_id", "source_url", "source_price", "status"}
        items = [(k, v) for k, v in fields.items() if k in allowed]
        if not items:
            return
        with self._lock:
            sets = ", ".join(f"{k}=?" for k, _ in items)
            vals = [(v.value if isinstance(v, OrderStatus) else (int(v) if isinstance(v, bool) else v)) for _, v in items]
            self._conn.execute(f"UPDATE orders SET {sets}, updated_at=? WHERE id=?", (*vals, _dt(utcnow()), order_id))
            self._conn.commit()

    # ----------------------------------------------------------- events
    def log(self, kind: str, message: str, level: str = "info", data: Optional[dict] = None) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events (ts, level, kind, message, data_json) VALUES (?,?,?,?,?)",
                               (_dt(utcnow()), level, kind, message, json.dumps(data, ensure_ascii=False) if data else None))
            self._conn.execute("DELETE FROM events WHERE id < (SELECT MAX(id) FROM events) - 2000")
            self._conn.commit()

    def events(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [{"id": r["id"], "ts": r["ts"], "level": r["level"], "kind": r["kind"], "message": r["message"],
                     "data": json.loads(r["data_json"]) if r["data_json"] else None} for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
