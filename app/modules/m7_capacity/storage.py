"""M7 中转保障容量：SQLite 持久化。

所有写操作在同一进程锁内串行，并在单条事务里完成，保证缩容与确认按同一容量版本裁决。
状态机与计数全部落在数据库，重试时不会重复归还容量。
"""
import json
import sqlite3
import threading
from pathlib import Path
from app.config import DATABASE_PATH

_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS m7_windows (
    airport TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    limit_slots INTEGER NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (airport, window_start, window_end)
);
CREATE TABLE IF NOT EXISTS m7_requests (
    request_id TEXT PRIMARY KEY,
    idem_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_seq INTEGER NOT NULL,
    chain_json TEXT NOT NULL,
    buckets_json TEXT NOT NULL,
    created_version INTEGER NOT NULL,
    decided_version INTEGER NOT NULL,
    reason_code TEXT
);
CREATE TABLE IF NOT EXISTS m7_slots (
    request_id TEXT NOT NULL,
    airport TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (request_id, airport, window_start, window_end)
);
CREATE TABLE IF NOT EXISTS m7_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at_utc TEXT NOT NULL,
    kind TEXT NOT NULL,
    request_id TEXT,
    airport TEXT,
    window_start TEXT,
    window_end TEXT,
    from_version INTEGER,
    to_version INTEGER,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS m7_seq (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else DATABASE_PATH
        self.lock = _LOCK
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def init_db(self) -> None:
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.executescript(SCHEMA)
                db.execute(
                    "INSERT OR IGNORE INTO m7_seq(name, value) VALUES('request_seq', 0)"
                )
                db.commit()
            except Exception:
                db.rollback()
                raise

    # ---- 序号 -------------------------------------------------------------
    def next_request_seq(self, db: sqlite3.Connection) -> int:
        row = db.execute("SELECT value FROM m7_seq WHERE name='request_seq'").fetchone()
        value = row["value"] + 1
        db.execute("UPDATE m7_seq SET value=? WHERE name='request_seq'", (value,))
        return value

    # ---- 容量窗 -----------------------------------------------------------
    def get_window(self, db, airport: str, start: str, end: str):
        return db.execute(
            "SELECT * FROM m7_windows WHERE airport=? AND window_start=? AND window_end=?",
            (airport, start, end),
        ).fetchone()

    def list_windows(self, db) -> list[sqlite3.Row]:
        return db.execute(
            "SELECT * FROM m7_windows ORDER BY airport, window_start"
        ).fetchall()

    def find_window(self, db, airport: str, at_utc: str):
        """半开区间 [start, end) 命中；多版重叠立即拒绝（沿用 M2 的版本纪律）。"""
        rows = db.execute(
            "SELECT * FROM m7_windows WHERE airport=? AND window_start<=? AND window_end>? ",
            (airport, at_utc, at_utc),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("CAPACITY_WINDOW_OVERLAP")
        return rows[0] if rows else None

    def upsert_window(self, db, airport: str, start: str, end: str, limit_slots: int,
                      old_version: int | None) -> sqlite3.Row:
        existing = self.get_window(db, airport, start, end)
        if existing is None:
            if old_version is not None:
                raise VersionConflict(0)
            db.execute(
                "INSERT INTO m7_windows(airport, window_start, window_end, limit_slots, version) "
                "VALUES(?,?,?,?,1)",
                (airport, start, end, limit_slots),
            )
            row = self.get_window(db, airport, start, end)
            self.log(db, "WINDOW_CREATE", airport=airport, start=start, end=end,
                     to_version=1, detail={"limit": limit_slots})
            return row
        if old_version is None or existing["version"] != old_version:
            raise VersionConflict(existing["version"])
        new_version = existing["version"] + 1
        db.execute(
            "UPDATE m7_windows SET limit_slots=?, version=? WHERE airport=? AND window_start=? AND window_end=?",
            (limit_slots, new_version, airport, start, end),
        )
        row = self.get_window(db, airport, start, end)
        self.log(db, "WINDOW_SHRINK", airport=airport, start=start, end=end,
                 from_version=existing["version"], to_version=new_version,
                 detail={"old_limit": existing["limit_slots"], "new_limit": limit_slots})
        return row

    # ---- 申请 -------------------------------------------------------------
    def get_request_by_idem(self, db, idem_key: str):
        return db.execute(
            "SELECT * FROM m7_requests WHERE idem_key=?", (idem_key,)
        ).fetchone()

    def get_request(self, db, request_id: str):
        return db.execute(
            "SELECT * FROM m7_requests WHERE request_id=?", (request_id,)
        ).fetchone()

    def insert_request(self, db, request_id: str, idem_key: str, status: str, seq: int,
                       chain: list, buckets: list, decided_version: int,
                       reason_code: str | None) -> None:
        db.execute(
            "INSERT INTO m7_requests VALUES(?,?,?,?,?,?,?,?,?)",
            (request_id, idem_key, status, seq, json.dumps(chain, ensure_ascii=False),
             json.dumps(buckets, ensure_ascii=False), decided_version, decided_version,
             reason_code),
        )

    def list_requests(self, db) -> list[sqlite3.Row]:
        return db.execute(
            "SELECT * FROM m7_requests ORDER BY created_seq"
        ).fetchall()

    def update_status(self, db, request_id: str, status: str, decided_version: int,
                      reason_code: str | None = None) -> None:
        db.execute(
            "UPDATE m7_requests SET status=?, decided_version=?, reason_code=? WHERE request_id=?",
            (status, decided_version, reason_code, request_id),
        )

    def list_waitlist(self, db) -> list[sqlite3.Row]:
        return db.execute(
            "SELECT * FROM m7_requests WHERE status='WAITLISTED' ORDER BY created_seq"
        ).fetchall()

    # ---- 占位 -------------------------------------------------------------
    def active_slots(self, db, airport: str, start: str, end: str) -> list[sqlite3.Row]:
        return db.execute(
            "SELECT * FROM m7_slots WHERE airport=? AND window_start=? AND window_end=? "
            "AND state IN ('HELD','CONFIRMED') ORDER BY seq",
            (airport, start, end),
        ).fetchall()

    def slots_of(self, db, request_id: str) -> list[sqlite3.Row]:
        return db.execute(
            "SELECT * FROM m7_slots WHERE request_id=? ORDER BY seq", (request_id,)
        ).fetchall()

    def insert_slot(self, db, request_id: str, airport: str, start: str, end: str,
                    state: str, version: int, seq: int) -> None:
        db.execute(
            "INSERT INTO m7_slots VALUES(?,?,?,?,?,?,?)",
            (request_id, airport, start, end, state, version, seq),
        )

    def set_slot_state(self, db, request_id: str, airport: str, start: str, end: str,
                       state: str, version: int) -> None:
        db.execute(
            "UPDATE m7_slots SET state=?, version=? WHERE request_id=? AND airport=? "
            "AND window_start=? AND window_end=?",
            (state, version, request_id, airport, start, end),
        )

    def release_slot(self, db, request_id: str, airport: str, start: str, end: str,
                     state: str) -> None:
        db.execute(
            "UPDATE m7_slots SET state=? WHERE request_id=? AND airport=? AND window_start=? "
            "AND window_end=? AND state IN ('HELD','CONFIRMED')",
            (state, request_id, airport, start, end),
        )

    # ---- 事件 -------------------------------------------------------------
    def log(self, db, kind: str, request_id: str | None = None, airport: str | None = None,
            start: str | None = None, end: str | None = None, from_version: int | None = None,
            to_version: int | None = None, detail: dict | None = None) -> int:
        cur = db.execute(
            "INSERT INTO m7_events(at_utc, kind, request_id, airport, window_start, window_end, "
            "from_version, to_version, detail) VALUES(?,?,?,?,?,?,?,?,?)",
            (utc_now_iso(), kind, request_id, airport, start, end, from_version, to_version,
             json.dumps(detail or {}, ensure_ascii=False)),
        )
        return cur.lastrowid

    def list_events(self, db, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM m7_events ORDER BY seq" + (" LIMIT ?" if limit else "")
        return db.execute(sql, (limit,) if limit else ()).fetchall()


class VersionConflict(Exception):
    def __init__(self, current_version: int):
        self.current_version = current_version
        super().__init__("CAPACITY_VERSION_CONFLICT")


def utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
