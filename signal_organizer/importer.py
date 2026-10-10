"""Message intake: manual paste, JSON export, CSV export.

Nothing here talks to Discord or any network service. Input is text you paste
or a file you choose.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import notifier, storage
from .parser import UNKNOWN, parse_message

log = logging.getLogger(__name__)

MAX_MESSAGES_PER_IMPORT = 5000
MAX_MESSAGE_CHARS = 4000  # Discord's own limit for Nitro users
MAX_FIELD_CHARS = 200
DUPLICATE_WINDOW = timedelta(hours=24)
MAX_INDIVIDUAL_NOTIFICATIONS = 5

PASTE_SEPARATOR = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)
PASTE_TIMESTAMP = re.compile(r"^\s*\[([^\]\n]{6,40})\]\s*")

TEXT_KEYS = ("content", "text", "message", "body")
TS_KEYS = ("timestamp", "time", "created_at", "date", "datetime")
ID_KEYS = ("id", "message_id", "messageid")
AUTHOR_KEYS = ("author", "username", "user", "sender")
SOURCE_KEYS = ("channel", "source", "channel_name")
REPLY_KEYS = ("reference", "message_reference", "reply_to", "referenced_message", "in_reply_to")


class ImportErrorWithDetail(ValueError):
    """Raised when an import file can't be read at all."""


@dataclass
class RawMessage:
    text: str
    timestamp: str | None = None
    source: str = "manual paste"
    source_message_id: str | None = None
    author: str | None = None
    is_reply: bool = False


@dataclass
class ImportSummary:
    inserted_ids: list[int] = field(default_factory=list)
    skipped_same_id: int = 0
    flagged_duplicates: int = 0
    flagged_stale: int = 0
    errors: list[str] = field(default_factory=list)
    notifications_sent: int = 0


# Timestamps ----------------------------------------------------------------

_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%m/%d/%y %I:%M %p", "%m/%d/%y %H:%M",
)


def parse_timestamp(value) -> datetime | None:
    """Parse a timestamp. Naive values are treated as the computer's local time.

    Returns None when the value is missing or unreadable. Never guesses.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Unix seconds, or milliseconds if it's clearly too large for seconds.
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in _TS_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # interpret as local time
    return dt.astimezone()


# Readers -------------------------------------------------------------------

def _clip(value, limit: int = MAX_FIELD_CHARS) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _first(d: dict, keys: tuple[str, ...]):
    lowered = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in lowered and lowered[k] not in (None, ""):
            return lowered[k]
    return None


def _author_name(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("username") or value.get("nickname")
    return _clip(value, 80)


def _truthy_reply(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (dict, list)):
        return bool(value)
    return str(value).strip().lower() not in ("", "0", "false", "no", "none", "null")


def _from_mapping(item: dict, default_source: str, index: int, errors: list[str]) -> RawMessage | None:
    text = _first(item, TEXT_KEYS)
    if not isinstance(text, str) or not text.strip():
        errors.append(f"Item {index}: no message text, skipped")
        return None
    if len(text) > MAX_MESSAGE_CHARS:
        errors.append(f"Item {index}: message longer than {MAX_MESSAGE_CHARS} characters, skipped")
        return None
    source = _first(item, SOURCE_KEYS)
    if isinstance(source, dict):
        source = source.get("name")
    return RawMessage(
        text=text,
        timestamp=_first(item, TS_KEYS),
        source=_clip(source) or default_source,
        source_message_id=_clip(_first(item, ID_KEYS)),
        author=_author_name(_first(item, AUTHOR_KEYS)),
        is_reply=_truthy_reply(_first(item, REPLY_KEYS)),
    )


def read_paste(text: str, source: str = "manual paste") -> tuple[list[RawMessage], list[str]]:
    """Split pasted text into messages.

    Separate multiple messages with a line containing only `---`. A message may
    start with a bracketed timestamp, e.g. `[2026-10-09 09:31] BTO ...`.
    """
    errors: list[str] = []
    if not isinstance(text, str) or not text.strip():
        return [], ["Nothing to import: the pasted text is empty"]
    source = _clip(source) or "manual paste"
    chunks = [c for c in PASTE_SEPARATOR.split(text) if c.strip()]
    if len(chunks) > MAX_MESSAGES_PER_IMPORT:
        return [], [f"Too many messages: limit is {MAX_MESSAGES_PER_IMPORT} per import"]
    messages = []
    for i, chunk in enumerate(chunks, 1):
        chunk = chunk.strip("\n")
        timestamp = None
        m = PASTE_TIMESTAMP.match(chunk)
        if m and parse_timestamp(m.group(1)) is not None:
            timestamp = m.group(1)
            body = chunk[m.end():]
        else:
            body = chunk
        if not body.strip():
            errors.append(f"Message {i}: empty after timestamp, skipped")
            continue
        if len(body) > MAX_MESSAGE_CHARS:
            errors.append(f"Message {i}: longer than {MAX_MESSAGE_CHARS} characters, skipped")
            continue
        messages.append(RawMessage(text=body.strip(), timestamp=timestamp, source=source))
    return messages, errors


def read_json(data: bytes | str, default_source: str = "JSON import") -> tuple[list[RawMessage], list[str]]:
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ImportErrorWithDetail(f"Not valid JSON: {exc}") from None
    if isinstance(payload, dict):
        channel = payload.get("channel")
        if isinstance(channel, dict) and channel.get("name"):
            default_source = _clip(channel["name"]) or default_source
        payload = payload.get("messages")
    if not isinstance(payload, list):
        raise ImportErrorWithDetail(
            "JSON must be a list of messages, or an object with a \"messages\" list"
        )
    if len(payload) > MAX_MESSAGES_PER_IMPORT:
        raise ImportErrorWithDetail(f"Too many messages: limit is {MAX_MESSAGES_PER_IMPORT} per import")
    errors: list[str] = []
    messages = []
    for i, item in enumerate(payload, 1):
        if not isinstance(item, dict):
            errors.append(f"Item {i}: not an object, skipped")
            continue
        msg = _from_mapping(item, default_source, i, errors)
        if msg:
            messages.append(msg)
    return messages, errors


def read_csv(data: bytes | str, default_source: str = "CSV import") -> tuple[list[RawMessage], list[str]]:
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ImportErrorWithDetail("CSV must be UTF-8 encoded") from None
    reader = csv.DictReader(io.StringIO(data))
    headers = {h.strip().lower() for h in (reader.fieldnames or []) if h}
    if not headers.intersection(TEXT_KEYS):
        raise ImportErrorWithDetail(
            "CSV needs a header row with a message column named one of: " + ", ".join(TEXT_KEYS)
        )
    errors: list[str] = []
    messages = []
    try:
        for i, row in enumerate(reader, 1):
            if i > MAX_MESSAGES_PER_IMPORT:
                raise ImportErrorWithDetail(f"Too many messages: limit is {MAX_MESSAGES_PER_IMPORT} per import")
            row = {(k or "").strip().lower(): v for k, v in row.items()}
            msg = _from_mapping(row, default_source, i, errors)
            if msg:
                messages.append(msg)
    except csv.Error as exc:
        raise ImportErrorWithDetail(f"CSV could not be read: {exc}") from None
    return messages, errors


# Ingest --------------------------------------------------------------------

def content_hash(text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _stale_reason(
    message_ts: datetime | None, imported_at: datetime, expiration_date: str, stale_minutes: int
) -> str | None:
    reasons = []
    if expiration_date != UNKNOWN:
        exp = datetime.fromisoformat(expiration_date).date()
        if exp < imported_at.date():
            reasons.append(f"option expired on {expiration_date}")
    if message_ts is not None:
        age = imported_at - message_ts
        if age > timedelta(minutes=stale_minutes):
            hours = age.total_seconds() / 3600
            age_text = f"{hours:.1f} hours" if hours >= 1 else f"{int(age.total_seconds() // 60)} minutes"
            reasons.append(f"message was {age_text} old at import (limit {stale_minutes} min)")
    return "; ".join(reasons) or None


def _find_duplicate(conn: sqlite3.Connection, digest: str, message_ts: datetime | None) -> int | None:
    for existing in storage.find_by_hash(conn, digest):
        other_ts = parse_timestamp(existing["message_ts"])
        if message_ts is None or other_ts is None or abs(message_ts - other_ts) <= DUPLICATE_WINDOW:
            return int(existing["id"])
    return None


def ingest(
    conn: sqlite3.Connection,
    messages: list[RawMessage],
    now: datetime | None = None,
    notify_fn=notifier.notify,
) -> ImportSummary:
    """Parse and store messages. Notifications fire only if enabled in settings."""
    summary = ImportSummary()
    now = now or datetime.now(timezone.utc).astimezone()
    stale_minutes = int(storage.get_setting(conn, "stale_minutes"))
    new_records = []

    for msg in messages:
        if msg.source_message_id and storage.find_by_message_id(conn, msg.source, msg.source_message_id):
            summary.skipped_same_id += 1
            continue
        message_ts = parse_timestamp(msg.timestamp)
        parsed = parse_message(msg.text, message_ts, is_reply=msg.is_reply)
        notes = list(parsed.notes)
        if msg.timestamp and message_ts is None:
            notes.append("Timestamp in source could not be read; stored as UNKNOWN")
        elif message_ts is None:
            notes.append("No message timestamp provided")

        digest = content_hash(msg.text)
        duplicate_of = _find_duplicate(conn, digest, message_ts)
        if duplicate_of:
            summary.flagged_duplicates += 1
            notes.append(f"Same text as record #{duplicate_of}")

        stale_reason = _stale_reason(message_ts, now, parsed.expiration_date, stale_minutes)
        if stale_reason:
            summary.flagged_stale += 1

        record = {
            "source": msg.source,
            "source_message_id": msg.source_message_id,
            "author": msg.author,
            "original_text": msg.text,
            "message_ts": message_ts.isoformat(timespec="seconds") if message_ts else None,
            "imported_at": now.isoformat(timespec="seconds"),
            "content_hash": digest,
            **{k: v for k, v in parsed.as_dict().items() if k != "notes"},
            "notes": notes,
            "is_stale": bool(stale_reason),
            "stale_reason": stale_reason,
            "duplicate_of": duplicate_of,
        }
        record_id = storage.insert_record(conn, record)
        summary.inserted_ids.append(record_id)
        new_records.append(record)

    log.info(
        "Import finished: %d stored, %d skipped (same message id), %d flagged duplicate, %d errors",
        len(summary.inserted_ids), summary.skipped_same_id, summary.flagged_duplicates, len(summary.errors),
    )

    if storage.get_setting(conn, "notifications_enabled") == "1" and new_records:
        notable = [r for r in new_records if r["kind"] != "NOT_A_SIGNAL" and not r["duplicate_of"]]
        for r in notable[:MAX_INDIVIDUAL_NOTIFICATIONS]:
            if notify_fn(*notifier.summarize(r)):
                summary.notifications_sent += 1
        extra = len(notable) - MAX_INDIVIDUAL_NOTIFICATIONS
        if extra > 0 and notify_fn("Signal Organizer", f"{extra} more records imported"):
            summary.notifications_sent += 1
    return summary
