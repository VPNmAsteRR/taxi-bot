"""SQLite storage shared by the passenger and driver bots."""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS passengers (
    chat_id     INTEGER PRIMARY KEY,
    name        TEXT,
    username    TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS drivers (
    chat_id     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    username    TEXT,
    phone       TEXT,
    car         TEXT NOT NULL,
    color       TEXT NOT NULL,
    plate       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    passenger_id         INTEGER NOT NULL,
    passenger_name       TEXT,
    passenger_username   TEXT,
    from_addr            TEXT NOT NULL,
    to_addr              TEXT NOT NULL,
    distance_km          REAL,
    tariff_note          TEXT,
    fare                 INTEGER NOT NULL,
    status               TEXT NOT NULL,
    driver_id            INTEGER,
    passenger_message_id INTEGER,
    created_at           INTEGER NOT NULL,
    accepted_at          INTEGER,
    completed_at         INTEGER,
    cancelled_at         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_passenger ON orders (passenger_id, status);

CREATE TABLE IF NOT EXISTS driver_order_messages (
    order_id    INTEGER NOT NULL,
    driver_id   INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (order_id, driver_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    bot         TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    state       TEXT,
    data        TEXT,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (bot, chat_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS admins (
    chat_id     INTEGER PRIMARY KEY,
    name        TEXT,
    username    TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fines (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    passenger_id     INTEGER NOT NULL,
    order_id         INTEGER,
    amount           INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    receipt_file_id  TEXT,
    receipt_note     TEXT,
    created_at       INTEGER NOT NULL,
    paid_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fines_passenger ON fines (passenger_id, status);
"""


class Store:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._migrate()
        # Merge the WAL into taxi.db on every start, so the drivers table lives in
        # the main file even if the panel drops the -wal file on restart/backup.
        try:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass

    def _migrate(self):
        columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(orders)")
        }
        if "distance_km" not in columns:
            self.db.execute("ALTER TABLE orders ADD COLUMN distance_km REAL")
        if "tariff_note" not in columns:
            self.db.execute("ALTER TABLE orders ADD COLUMN tariff_note TEXT")

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _dict(row):
        return dict(row) if row is not None else None

    def _one(self, sql, args=()):
        with self.lock:
            return self._dict(self.db.execute(sql, args).fetchone())

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # ------------------------------------------------------------- passengers
    def ensure_passenger(self, chat_id, name=None, username=None):
        now = int(time.time())
        with self.lock:
            self.db.execute(
                "INSERT INTO passengers (chat_id, name, username, created_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "name=COALESCE(excluded.name, passengers.name), "
                "username=COALESCE(excluded.username, passengers.username)",
                (chat_id, name, username, now),
            )

    def get_passenger(self, chat_id):
        return self._one("SELECT * FROM passengers WHERE chat_id=?", (chat_id,))

    # ----------------------------------------------------------------- drivers
    def get_driver(self, chat_id):
        return self._one("SELECT * FROM drivers WHERE chat_id=?", (chat_id,))

    def create_driver(self, chat_id, name, car, color, plate, phone=None, username=None):
        now = int(time.time())
        with self.lock:
            self.db.execute(
                "INSERT INTO drivers "
                "(chat_id, name, username, phone, car, color, plate, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?, 'active', ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "name=excluded.name, username=excluded.username, phone=excluded.phone, "
                "car=excluded.car, color=excluded.color, plate=excluded.plate, "
                "status=drivers.status, updated_at=excluded.updated_at",
                (chat_id, name, username, phone, car, color, plate, now, now),
            )
        return self.get_driver(chat_id)

    def set_driver_status(self, chat_id, status):
        with self.lock:
            self.db.execute(
                "UPDATE drivers SET status=?, updated_at=? WHERE chat_id=?",
                (status, int(time.time()), chat_id),
            )
        return self.get_driver(chat_id)

    def active_drivers(self):
        return self._all(
            "SELECT * FROM drivers d WHERE d.status='active' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM orders o "
            "  WHERE o.driver_id=d.chat_id AND o.status='accepted'"
            ") ORDER BY d.created_at"
        )

    def driver_count(self):
        row = self._one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active "
            "FROM drivers"
        )
        return {"total": row["total"] or 0, "active": row["active"] or 0}

    # ------------------------------------------------------------------ orders
    def create_order(self, passenger_id, passenger_name, passenger_username,
                     from_addr, to_addr, fare, distance_km=None,
                     tariff_note=None, passenger_message_id=None):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO orders "
                "(passenger_id, passenger_name, passenger_username, from_addr, to_addr, "
                "distance_km, tariff_note, fare, status, passenger_message_id, created_at) "
                "VALUES (?,?,?,?,?,?,?,?, 'searching', ?, ?)",
                (
                    passenger_id,
                    passenger_name,
                    passenger_username,
                    from_addr,
                    to_addr,
                    distance_km,
                    tariff_note,
                    fare,
                    passenger_message_id,
                    now,
                ),
            )
            order_id = cur.lastrowid
        return self.get_order(order_id)

    def get_order(self, order_id):
        return self._one("SELECT * FROM orders WHERE id=?", (order_id,))

    def get_order_with_driver(self, order_id):
        return self._one(
            "SELECT o.*, "
            "d.name AS driver_name, d.username AS driver_username, d.phone AS driver_phone, "
            "d.car AS driver_car, d.color AS driver_color, d.plate AS driver_plate "
            "FROM orders o LEFT JOIN drivers d ON d.chat_id=o.driver_id "
            "WHERE o.id=?",
            (order_id,),
        )

    def set_passenger_message(self, order_id, message_id):
        with self.lock:
            self.db.execute(
                "UPDATE orders SET passenger_message_id=? WHERE id=?",
                (message_id, order_id),
            )

    def accept_order(self, order_id, driver_id):
        """Atomically accept a searching order.

        The conditional UPDATE is the whole race protection: even if two
        drivers press the button at the same second, only one row changes.
        """
        now = int(time.time())
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                cur = self.db.execute(
                    "UPDATE orders SET status='accepted', driver_id=?, accepted_at=? "
                    "WHERE id=? AND status='searching'",
                    (driver_id, now, order_id),
                )
                if cur.rowcount != 1:
                    self.db.execute("ROLLBACK")
                    return None
                row = self.db.execute(
                    "SELECT * FROM orders WHERE id=?", (order_id,)
                ).fetchone()
                self.db.execute("COMMIT")
                return self._dict(row)
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def complete_order(self, order_id, driver_id):
        with self.lock:
            cur = self.db.execute(
                "UPDATE orders SET status='completed', completed_at=? "
                "WHERE id=? AND driver_id=? AND status='accepted'",
                (int(time.time()), order_id, driver_id),
            )
            return cur.rowcount == 1

    def cancel_order(self, order_id, passenger_id):
        """Passenger may cancel both while searching and after a driver accepted."""
        with self.lock:
            cur = self.db.execute(
                "UPDATE orders SET status='cancelled', cancelled_at=? "
                "WHERE id=? AND passenger_id=? AND status IN ('searching','accepted')",
                (int(time.time()), order_id, passenger_id),
            )
            return cur.rowcount == 1

    def expire_searching_order(self, order_id, cutoff):
        """Cancel a searching order that is older than cutoff (unix time)."""
        with self.lock:
            cur = self.db.execute(
                "UPDATE orders SET status='cancelled', cancelled_at=? "
                "WHERE id=? AND status='searching' AND created_at<=?",
                (int(time.time()), order_id, cutoff),
            )
            return cur.rowcount == 1

    def searching_orders(self):
        return self._all(
            "SELECT * FROM orders WHERE status='searching' ORDER BY id"
        )

    def active_order_for_passenger(self, passenger_id):
        return self._one(
            "SELECT * FROM orders "
            "WHERE passenger_id=? AND status IN ('searching','accepted') "
            "ORDER BY id DESC LIMIT 1",
            (passenger_id,),
        )

    def active_order_for_driver(self, driver_id):
        return self._one(
            "SELECT * FROM orders "
            "WHERE driver_id=? AND status='accepted' "
            "ORDER BY id DESC LIMIT 1",
            (driver_id,),
        )

    def order_stats(self):
        rows = self._all("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")
        by_status = {r["status"]: r["n"] for r in rows}
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "drivers": self.driver_count(),
        }

    # ------------------------------------------------------------------ admins
    def is_admin(self, chat_id):
        return self._one("SELECT 1 FROM admins WHERE chat_id=?", (chat_id,)) is not None

    def add_admin(self, chat_id, name=None, username=None):
        with self.lock:
            self.db.execute(
                "INSERT INTO admins (chat_id, name, username, created_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "name=COALESCE(excluded.name, admins.name), "
                "username=COALESCE(excluded.username, admins.username)",
                (chat_id, name, username, int(time.time())),
            )

    def admin_count(self):
        return self._one("SELECT COUNT(*) AS n FROM admins")["n"] or 0

    # ------------------------------------------------------------------- fines
    def create_fine(self, passenger_id, order_id, amount):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO fines (passenger_id, order_id, amount, status, created_at) "
                "VALUES (?,?,?, 'pending', ?)",
                (passenger_id, order_id, amount, now),
            )
            fine_id = cur.lastrowid
        return self.get_fine(fine_id)

    def get_fine(self, fine_id):
        return self._one("SELECT * FROM fines WHERE id=?", (fine_id,))

    def active_fine(self, passenger_id):
        """An unpaid fine means the passenger is blocked."""
        return self._one(
            "SELECT * FROM fines WHERE passenger_id=? AND status='pending' "
            "ORDER BY id LIMIT 1",
            (passenger_id,),
        )

    def is_blocked(self, passenger_id):
        return self.active_fine(passenger_id) is not None

    def attach_receipt(self, fine_id, file_id, note=None):
        with self.lock:
            self.db.execute(
                "UPDATE fines SET receipt_file_id=?, receipt_note=? WHERE id=?",
                (file_id, note, fine_id),
            )
        return self.get_fine(fine_id)

    def paid_fine(self, fine_id):
        with self.lock:
            self.db.execute(
                "UPDATE fines SET status='paid', paid_at=? WHERE id=?",
                (int(time.time()), fine_id),
            )
        return self.get_fine(fine_id)

    def fines_stats(self):
        return self._one(
            "SELECT "
            "(SELECT COUNT(*) FROM fines WHERE status='pending') AS open, "
            "(SELECT COUNT(*) FROM fines WHERE status='paid') AS paid, "
            "(SELECT COALESCE(SUM(amount),0) FROM fines WHERE status='paid') AS paid_sum"
        )

    @staticmethod
    def _day_start_ts():
        """Midnight in the configured TZ (falls back to server local time)."""
        now = datetime.now()
        tz_name = os.environ.get("TZ")
        if tz_name:
            try:
                from zoneinfo import ZoneInfo

                now = datetime.now(ZoneInfo(tz_name))
            except Exception:
                pass
        return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    def admin_stats(self):
        start = self._day_start_ts()
        row = self._one(
            "SELECT "
            "(SELECT COUNT(*) FROM drivers) AS drivers_total, "
            "(SELECT COUNT(*) FROM drivers WHERE status='active') AS drivers_active, "
            "(SELECT COUNT(*) FROM orders WHERE status='searching') AS orders_searching, "
            "(SELECT COUNT(*) FROM orders WHERE status='accepted') AS orders_accepted, "
            "(SELECT COUNT(*) FROM orders WHERE created_at >= ?) AS orders_today, "
            "(SELECT COUNT(*) FROM orders WHERE status='completed' "
            " AND completed_at >= ?) AS completed_today, "
            "(SELECT COUNT(*) FROM orders WHERE status='cancelled' "
            " AND cancelled_at >= ?) AS cancelled_today, "
            "(SELECT COALESCE(SUM(fare), 0) FROM orders WHERE status='completed' "
            " AND completed_at >= ?) AS revenue_today, "
            "(SELECT COUNT(*) FROM orders) AS orders_total, "
            "(SELECT COUNT(*) FROM passengers) AS passengers_total",
            (start, start, start, start),
        )
        row["admins_total"] = self.admin_count()
        return row

    def drivers_on_shift(self):
        return self._all(
            "SELECT d.*, o.id AS order_id, o.from_addr AS order_from, "
            "o.to_addr AS order_to, o.fare AS order_fare, "
            "o.distance_km AS order_distance "
            "FROM drivers d "
            "LEFT JOIN orders o ON o.driver_id=d.chat_id AND o.status='accepted' "
            "WHERE d.status='active' "
            "ORDER BY d.name COLLATE NOCASE"
        )

    def recent_orders(self, limit=10):
        return self._all(
            "SELECT o.*, d.name AS driver_name "
            "FROM orders o LEFT JOIN drivers d ON d.chat_id=o.driver_id "
            "ORDER BY o.id DESC LIMIT ?",
            (int(limit),),
        )

    # --------------------------------------------------------- driver messages
    def add_driver_message(self, order_id, driver_id, message_id):
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO driver_order_messages "
                "(order_id, driver_id, message_id, created_at) VALUES (?,?,?,?)",
                (order_id, driver_id, message_id, int(time.time())),
            )

    def get_driver_message(self, order_id, driver_id):
        return self._one(
            "SELECT * FROM driver_order_messages WHERE order_id=? AND driver_id=?",
            (order_id, driver_id),
        )

    def driver_messages(self, order_id):
        return self._all(
            "SELECT * FROM driver_order_messages WHERE order_id=? ORDER BY created_at",
            (order_id,),
        )

    def remove_driver_message(self, order_id, driver_id):
        with self.lock:
            self.db.execute(
                "DELETE FROM driver_order_messages WHERE order_id=? AND driver_id=?",
                (order_id, driver_id),
            )

    # ----------------------------------------------------------------- sessions
    def get_session(self, bot, chat_id):
        row = self._one(
            "SELECT * FROM sessions WHERE bot=? AND chat_id=?", (bot, chat_id)
        )
        if not row:
            return None
        try:
            data = json.loads(row["data"] or "{}")
        except ValueError:
            data = {}
        return {"state": row["state"], "data": data}

    def set_session(self, bot, chat_id, state, data=None):
        with self.lock:
            self.db.execute(
                "INSERT INTO sessions (bot, chat_id, state, data, updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(bot, chat_id) DO UPDATE SET "
                "state=excluded.state, data=excluded.data, updated_at=excluded.updated_at",
                (
                    bot,
                    chat_id,
                    state,
                    json.dumps(data or {}, ensure_ascii=False),
                    int(time.time()),
                ),
            )

    def clear_session(self, bot, chat_id):
        with self.lock:
            self.db.execute(
                "DELETE FROM sessions WHERE bot=? AND chat_id=?", (bot, chat_id)
            )

    # --------------------------------------------------------------------- meta
    def meta_get(self, key, default=None):
        row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def meta_set(self, key, value):
        with self.lock:
            self.db.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
