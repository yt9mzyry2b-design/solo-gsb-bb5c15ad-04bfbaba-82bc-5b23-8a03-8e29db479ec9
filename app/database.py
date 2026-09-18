"""SQLite 数据访问层。"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .billing import IN, OUT, Swipe

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    zone    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fare_matrix (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_from   INTEGER NOT NULL,
    zone_to     INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    UNIQUE(zone_from, zone_to)
);
CREATE TABLE IF NOT EXISTS swipes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    card      TEXT NOT NULL,
    station   TEXT NOT NULL,
    tap_time  TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK(kind IN ('IN', 'OUT')),
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK(id=1),
    transfer_discount_cents INTEGER NOT NULL DEFAULT 100,
    daily_cap_cents         INTEGER NOT NULL DEFAULT 1000
);
"""


def parse_dt(s: str) -> datetime:
    """接受 'YYYY-MM-DD HH:MM[:SS]'（- 或 / 分隔均可）。"""
    s = s.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间「{s}」，格式应为 YYYY-MM-DD HH:MM")


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO settings(id) VALUES(1)"
            )

    # ---------------- 站点分区 ----------------
    def list_stations(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, name, zone FROM stations ORDER BY zone, name"
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_station(self, name: str, zone: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO stations(name, zone) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET zone=excluded.zone",
                (name, zone),
            )

    def delete_station(self, sid: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM stations WHERE id=?", (sid,))

    # ---------------- 票价矩阵 ----------------
    def list_fares(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, zone_from, zone_to, price_cents "
                "FROM fare_matrix ORDER BY zone_from, zone_to"
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_fare(self, z1: int, z2: int, price_cents: int) -> None:
        a, b = sorted((z1, z2))
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO fare_matrix(zone_from, zone_to, price_cents) "
                "VALUES(?, ?, ?) ON CONFLICT(zone_from, zone_to) "
                "DO UPDATE SET price_cents=excluded.price_cents",
                (a, b, price_cents),
            )

    def delete_fare(self, fid: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM fare_matrix WHERE id=?", (fid,))

    # ---------------- 参数 ----------------
    def get_settings(self) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT transfer_discount_cents, daily_cap_cents FROM settings"
            ).fetchone()
            if row is None:
                return {"transfer_discount_cents": 100, "daily_cap_cents": 1000}
            return dict(row)

    def update_settings(self, transfer_discount_cents: int,
                        daily_cap_cents: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE settings SET transfer_discount_cents=?, "
                "daily_cap_cents=?", (transfer_discount_cents, daily_cap_cents)
            )

    # ---------------- 刷卡记录 ----------------
    def list_swipes(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, card, station, tap_time, kind, created_at "
                "FROM swipes ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def add_swipe(self, card: str, station: str, tap_time: str,
                  kind: str) -> int:
        kind = kind.upper()
        if kind not in (IN, OUT):
            raise ValueError("刷卡类型必须是 IN（进站）或 OUT（出站）")
        dt = parse_dt(tap_time)  # 校验格式，存标准化文本
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO swipes(card, station, tap_time, kind) "
                "VALUES(?, ?, ?, ?)",
                (card.strip(), station.strip(), dt.strftime("%Y-%m-%d %H:%M:%S"), kind),
            )
            return int(cur.lastrowid)

    def delete_swipe(self, sid: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM swipes WHERE id=?", (sid,))

    def clear_all(self, include_config: bool = False) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM swipes")
            if include_config:
                conn.execute("DELETE FROM stations")
                conn.execute("DELETE FROM fare_matrix")
                conn.execute(
                    "UPDATE settings SET transfer_discount_cents=100, "
                    "daily_cap_cents=1000"
                )

    # ---------------- 核验取数 ----------------
    def billing_inputs(self):
        stations = {r["name"]: r["zone"] for r in self.list_stations()}
        fares: dict[tuple[int, int], int] = {}
        for r in self.list_fares():
            a, b = sorted((r["zone_from"], r["zone_to"]))
            fares[(a, b)] = r["price_cents"]
        settings = self.get_settings()
        swipes = [
            Swipe(
                seq=r["id"], card=r["card"], station=r["station"],
                t=parse_dt(r["tap_time"]), kind=r["kind"],
            )
            for r in self.list_swipes()
        ]
        return swipes, stations, fares, settings
