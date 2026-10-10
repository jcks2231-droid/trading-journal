"""SQLite storage. Every query uses parameters; no string-built SQL with user input."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_message_id TEXT,
    author TEXT,
    original_text TEXT NOT NULL,
    message_ts TEXT,
    imported_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    kind TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    ticker TEXT NOT NULL,
    option_type TEXT NOT NULL,
    strike TEXT NOT NULL,
    expiration_text TEXT NOT NULL,
    expiration_date TEXT NOT NULL,
    entry TEXT NOT NULL,
    target TEXT NOT NULL,
    stop TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '[]',
    is_stale INTEGER NOT NULL DEFAULT 0,
    stale_reason TEXT,
    duplicate_of INTEGER REFERENCES records(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_records_hash ON records(content_hash);
CREATE INDEX IF NOT EXISTS idx_records_ticker ON records(ticker);
CREATE INDEX IF NOT EXISTS idx_records_msgid ON records(source, source_message_id);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER REFERENCES records(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    option_type TEXT NOT NULL,
    strike TEXT NOT NULL,
    expiration TEXT NOT NULL,
    decision TEXT NOT NULL,
    hypothetical_entry REAL,
    hypothetical_exit REAL,
    contracts INTEGER,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "notifications_enabled": "0",
    "stale_minutes": "30",
}

RECORD_FIELDS = (
    "source", "source_message_id", "author", "original_text", "message_ts",
    "imported_at", "content_hash", "kind", "parse_status", "ticker", "option_type",
    "strike", "expiration_text", "expiration_date", "entry", "target", "stop",
    "notes", "is_stale", "stale_reason", "duplicate_of",
)


def now_iso() -> str:
    """Current local time with UTC offset, so stored dates match the user's calendar."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    return conn


# Settings ------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS[key]


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    if key not in DEFAULT_SETTINGS:
        raise KeyError(f"unknown setting: {key}")
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


# Records -------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if "notes" in d and isinstance(d["notes"], str):
        try:
            d["notes"] = json.loads(d["notes"])
        except json.JSONDecodeError:
            d["notes"] = [d["notes"]]
    return d


def insert_record(conn: sqlite3.Connection, record: dict) -> int:
    values = dict(record)
    values["notes"] = json.dumps(values.get("notes", []))
    values["is_stale"] = 1 if values.get("is_stale") else 0
    cols = ", ".join(RECORD_FIELDS)
    placeholders = ", ".join("?" for _ in RECORD_FIELDS)
    cur = conn.execute(
        f"INSERT INTO records ({cols}) VALUES ({placeholders})",
        tuple(values.get(f) for f in RECORD_FIELDS),
    )
    conn.commit()
    return int(cur.lastrowid)


def find_by_message_id(conn: sqlite3.Connection, source: str, message_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM records WHERE source = ? AND source_message_id = ? LIMIT 1",
        (source, message_id),
    ).fetchone()
    return _row_to_dict(row) if row else None


def find_by_hash(conn: sqlite3.Connection, content_hash: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM records WHERE content_hash = ? ORDER BY id", (content_hash,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_record(conn: sqlite3.Connection, record_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_records(
    conn: sqlite3.Connection,
    ticker: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    newest_first: bool = False,
    limit: int = 1000,
) -> list[dict]:
    # Chronological by message time. Records without a message timestamp fall
    # back to import time so they still sort sensibly.
    sort_key = "COALESCE(message_ts, imported_at)"
    clauses, params = [], []
    if ticker:
        clauses.append("ticker = ?")
        params.append(ticker.upper())
    if date_from:
        clauses.append(f"substr({sort_key}, 1, 10) >= ?")
        params.append(date_from)
    if date_to:
        clauses.append(f"substr({sort_key}, 1, 10) <= ?")
        params.append(date_to)
    if status:
        clauses.append("parse_status = ?")
        params.append(status)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order = "DESC" if newest_first else "ASC"
    rows = conn.execute(
        f"SELECT * FROM records {where} ORDER BY {sort_key} {order}, id {order} LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def distinct_tickers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM records WHERE ticker != 'UNKNOWN' ORDER BY ticker"
    ).fetchall()
    return [r["ticker"] for r in rows]


def count_records(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])


def delete_record(conn: sqlite3.Connection, record_id: int) -> bool:
    cur = conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
    conn.commit()
    return cur.rowcount > 0


def delete_all_records(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM records")
    conn.commit()
    return cur.rowcount


# Paper-trading journal -----------------------------------------------------

def insert_journal_entry(conn: sqlite3.Connection, entry: dict) -> int:
    cur = conn.execute(
        "INSERT INTO journal (record_id, created_at, ticker, option_type, strike, expiration, "
        "decision, hypothetical_entry, hypothetical_exit, contracts, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entry.get("record_id"), entry.get("created_at") or now_iso(), entry["ticker"],
            entry["option_type"], entry["strike"], entry["expiration"], entry["decision"],
            entry.get("hypothetical_entry"), entry.get("hypothetical_exit"),
            entry.get("contracts"), entry.get("notes", ""),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_journal_exit(conn: sqlite3.Connection, entry_id: int, exit_price: float | None, notes: str) -> bool:
    cur = conn.execute(
        "UPDATE journal SET hypothetical_exit = ?, notes = ? WHERE id = ?",
        (exit_price, notes, entry_id),
    )
    conn.commit()
    return cur.rowcount > 0


def hypothetical_pnl(entry: dict) -> float | None:
    """Paper P/L per standard 100-share option contract. None if inputs are missing."""
    e, x, n = entry.get("hypothetical_entry"), entry.get("hypothetical_exit"), entry.get("contracts")
    if e is None or x is None or n is None:
        return None
    return round((x - e) * n * 100, 2)


def list_journal(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM journal ORDER BY created_at DESC, id DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pnl"] = hypothetical_pnl(d)
        out.append(d)
    return out


def get_journal_entry(conn: sqlite3.Connection, entry_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM journal WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["pnl"] = hypothetical_pnl(d)
    return d


def delete_journal_entry(conn: sqlite3.Connection, entry_id: int) -> bool:
    cur = conn.execute("DELETE FROM journal WHERE id = ?", (entry_id,))
    conn.commit()
    return cur.rowcount > 0
