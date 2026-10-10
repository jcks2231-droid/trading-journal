"""Extract structured fields from free-text options alerts.

Rules this module follows:
- Never invent a value. A field that is missing or ambiguous is UNKNOWN.
- Keep the exact original text untouched; parsing works on a copy.
- Quoted lines (Discord "> " markdown) are separated from new text, so a reply
  that quotes an old alert is not mistaken for a new alert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

UNKNOWN = "UNKNOWN"

# Message kinds
NEW_SIGNAL = "NEW_SIGNAL"
QUOTED_SIGNAL = "QUOTED_SIGNAL"
UPDATE = "UPDATE"
NOT_A_SIGNAL = "NOT_A_SIGNAL"

# Parse statuses
COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
FAILED = "FAILED"

NUM = r"\$?(\d+(?:\.\d+)?|\.\d+)"

# Words that look like tickers but are trading jargon or common caps words.
STOPWORDS = {
    "A", "I", "AM", "PM", "ET", "EST", "EDT", "PST", "CT", "UTC",
    "BTO", "STO", "BTC", "STC", "BUY", "SELL", "SOLD", "LONG", "SHORT",
    "CALL", "CALLS", "PUT", "PUTS", "C", "P",
    "PT", "TP", "SL", "TGT", "TARGET", "STOP", "ENTRY", "EXIT", "AVG",
    "EXP", "EXPIRY", "DTE", "ODTE", "OTM", "ITM", "ATM", "IV",
    "ALERT", "NEW", "UPDATE", "TRIM", "OUT", "IN", "AT", "AND", "OR", "THE",
    "LOTTO", "SWING", "SCALP", "DAY", "WEEK", "RISK", "HIGH", "LOW", "NFA",
    "FYI", "GL", "LFG", "OK", "USD", "ON", "TO", "FOR", "OF", "IS", "IT",
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "SEPT",
    "OCT", "NOV", "DEC", "STRIKE", "PRICE", "SIZE", "SMALL", "HALF", "FULL",
    "TBD", "TBA", "ETA", "IMO", "EOD", "EOW", "HOD", "LOD", "ATH", "YOLO", "FOMC",
    "CPI", "PPI", "GDP", "FED", "NYSE", "USA", "US", "MY", "WE", "ALL", "NOW", "GO",
    "BUT", "NOT", "UP", "DOWN", "IF", "SO", "BE", "NO", "YES", "OPEN", "HOLD",
}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

UPDATE_PATTERNS = [
    r"\bSTC\b", r"\bBTC\b", r"\btrim(?:med|ming)?\b", r"\bsold\b", r"\bclos(?:e|ed|ing)\b",
    r"\bstopped\s+out\b", r"\btaking\s+profits?\b", r"\bexit(?:ed|ing)?\b",
    r"\bout\s+of\b", r"\bscal(?:e|ed|ing)\s+out\b",
]

ENTRY_RE = re.compile(
    r"(?:@|\bentry(?:\s+price)?\b|\bin\s+at\b|\bfilled\s+at\b|\bavg\b|\bprice\b|\bat\b)\s*[:=]?\s*" + NUM,
    re.IGNORECASE,
)
TARGET_RE = re.compile(
    r"\b(?:price\s+targets?|take\s+profits?|targets?|tgts?|pts?|tps?)\b\s*[:=]?\s*"
    r"(\$?(?:\d+(?:\.\d+)?|\.\d+)(?:\s*(?:/|,|&|and|-)\s*\$?(?:\d+(?:\.\d+)?|\.\d+))*)",
    re.IGNORECASE,
)
STOP_RE = re.compile(r"\b(?:stop[\s-]*loss|stop|sl)\b\s*[:=]?\s*" + NUM, re.IGNORECASE)

SLASH_DATE_RE = re.compile(r"(?<![\d.])(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?(?![\d.])")
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MONTH_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)

STRIKE_SUFFIX_RE = re.compile(r"(?<![\w.])\$?(\d+(?:\.\d+)?)\s?([cp])\b", re.IGNORECASE)
STRIKE_WORD_RE = re.compile(r"(?<![\w.])\$?(\d+(?:\.\d+)?)\s*(calls?|puts?)\b", re.IGNORECASE)
STRIKE_LABEL_RE = re.compile(r"\bstrike\s*[:=]?\s*\$?(\d+(?:\.\d+)?)", re.IGNORECASE)
TYPE_WORD_RE = re.compile(r"\b(calls?|puts?)\b", re.IGNORECASE)

DOLLAR_TICKER_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
TICKER_RE = re.compile(r"(?<![\w$])([A-Z]{1,5})(?![\w])")


@dataclass
class ParsedSignal:
    kind: str = NOT_A_SIGNAL
    parse_status: str = FAILED
    ticker: str = UNKNOWN
    option_type: str = UNKNOWN
    strike: str = UNKNOWN
    expiration_text: str = UNKNOWN
    expiration_date: str = UNKNOWN
    entry: str = UNKNOWN
    target: str = UNKNOWN
    stop: str = UNKNOWN
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "parse_status": self.parse_status,
            "ticker": self.ticker,
            "option_type": self.option_type,
            "strike": self.strike,
            "expiration_text": self.expiration_text,
            "expiration_date": self.expiration_date,
            "entry": self.entry,
            "target": self.target,
            "stop": self.stop,
            "notes": list(self.notes),
        }


def split_quoted(text: str) -> tuple[str, str]:
    """Split Discord markdown quotes ("> " or ">>> ") from new text."""
    new_lines, quoted_lines = [], []
    in_block_quote = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if in_block_quote:
            quoted_lines.append(line)
        elif stripped.startswith(">>> "):
            in_block_quote = True
            quoted_lines.append(stripped[4:])
        elif stripped.startswith("> ") or stripped == ">":
            quoted_lines.append(stripped[2:])
        else:
            new_lines.append(line)
    return "\n".join(new_lines), "\n".join(quoted_lines)


def _mask(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _fmt_num(raw: str) -> str:
    raw = raw.lstrip("$")
    if raw.startswith("."):
        raw = "0" + raw
    return raw


def _unique(values: list[str]) -> list[str]:
    seen: list[str] = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen


def _pick(values: list[str], label: str, notes: list[str]) -> str:
    values = _unique(values)
    if not values:
        return UNKNOWN
    if len(values) > 1:
        notes.append(f"{label} ambiguous: found {', '.join(values)}")
        return UNKNOWN
    return values[0]


def _extract_prices(text: str, notes: list[str]) -> tuple[str, str, str, str]:
    """Extract target, stop, and entry. Returns (target, stop, entry, masked_text)."""
    targets, stops, entries = [], [], []

    for m in list(TARGET_RE.finditer(text)):
        nums = re.findall(r"\d+(?:\.\d+)?|\.\d+", m.group(1))
        targets.append(" / ".join(_fmt_num(n) for n in nums))
        text = _mask(text, m.start(), m.end())

    for m in list(STOP_RE.finditer(text)):
        stops.append(_fmt_num(m.group(1)))
        text = _mask(text, m.start(), m.end())

    for m in list(ENTRY_RE.finditer(text)):
        # "at 10/20" or "@ 450c" are not entry prices; skip when the number continues
        # into a date or a strike suffix.
        tail = text[m.end(): m.end() + 2]
        if tail[:1] in ("/", ":") or re.match(r"\s?[cCpP]\b", tail):
            continue
        entries.append(_fmt_num(m.group(1)))
        text = _mask(text, m.start(), m.end())

    target = _pick(targets, "Target", notes)
    stop = _pick(stops, "Stop", notes)
    entry = _pick(entries, "Entry", notes)
    return target, stop, entry, text


def _valid_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _extract_expiration(
    text: str, message_ts: datetime | None, notes: list[str]
) -> tuple[str, str, str]:
    """Returns (expiration_text, expiration_date_iso, masked_text)."""
    found: list[tuple[str, date | None, bool]] = []  # (raw, date, year_inferred)

    def infer_year(month: int, day: int) -> tuple[date | None, bool]:
        if message_ts is None:
            return None, False
        base = message_ts.date()
        candidate = _valid_date(base.year, month, day)
        if candidate is None:
            return None, False
        if candidate < base:
            candidate = _valid_date(base.year + 1, month, day)
        return candidate, True

    for m in list(ISO_DATE_RE.finditer(text)):
        d = _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            found.append((m.group(0), d, False))
            text = _mask(text, m.start(), m.end())

    for m in list(SLASH_DATE_RE.finditer(text)):
        month, day = int(m.group(1)), int(m.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        if m.group(3):
            year = int(m.group(3))
            year = year + 2000 if year < 100 else year
            d, inferred = _valid_date(year, month, day), False
        else:
            d, inferred = infer_year(month, day)
        found.append((m.group(0), d, inferred))
        text = _mask(text, m.start(), m.end())

    for m in list(MONTH_DATE_RE.finditer(text)):
        month = MONTHS[m.group(1).lower()[:3]]
        day = int(m.group(2))
        if m.group(3):
            d, inferred = _valid_date(int(m.group(3)), month, day), False
        else:
            d, inferred = infer_year(month, day)
        if d is None and not (1 <= day <= 31):
            continue
        found.append((m.group(0), d, inferred))
        text = _mask(text, m.start(), m.end())

    if re.search(r"\b0\s?DTE\b", text, re.IGNORECASE):
        notes.append("Message says 0DTE; expiration date not stated explicitly")

    if not found:
        return UNKNOWN, UNKNOWN, text

    distinct_dates = _unique([d.isoformat() if d else raw for raw, d, _ in found])
    if len(distinct_dates) > 1:
        notes.append("Expiration ambiguous: found " + ", ".join(raw for raw, _, _ in found))
        return UNKNOWN, UNKNOWN, text

    raw, d, inferred = found[0]
    if d is None:
        if message_ts is None:
            notes.append("Expiration year not stated and message timestamp unknown")
        else:
            notes.append(f"Expiration '{raw}' is not a valid calendar date")
        return raw, UNKNOWN, text
    if inferred:
        notes.append("Expiration year not stated; inferred from message timestamp")
    return raw, d.isoformat(), text


def _extract_strike_and_type(text: str, notes: list[str]) -> tuple[str, str, str]:
    strikes: list[str] = []
    types: list[str] = []

    for regex in (STRIKE_SUFFIX_RE, STRIKE_WORD_RE):
        for m in list(regex.finditer(text)):
            strikes.append(_fmt_num(m.group(1)))
            types.append("CALL" if m.group(2).lower().startswith("c") else "PUT")
            text = _mask(text, m.start(), m.end())

    for m in list(STRIKE_LABEL_RE.finditer(text)):
        strikes.append(_fmt_num(m.group(1)))
        text = _mask(text, m.start(), m.end())

    for m in list(TYPE_WORD_RE.finditer(text)):
        types.append("CALL" if m.group(1).lower().startswith("c") else "PUT")
        text = _mask(text, m.start(), m.end())

    strike = _pick(strikes, "Strike", notes)
    option_type = _pick(types, "Call/put", notes)
    return strike, option_type, text


def _extract_ticker(text: str, notes: list[str]) -> str:
    dollar = [m.group(1).upper() for m in DOLLAR_TICKER_RE.finditer(text)]
    dollar = [t for t in dollar if t not in STOPWORDS]
    if dollar:
        return _pick(dollar, "Ticker", notes)
    candidates = [m.group(1) for m in TICKER_RE.finditer(text) if m.group(1) not in STOPWORDS]
    return _pick(candidates, "Ticker", notes)


def _parse_body(text: str, message_ts: datetime | None) -> ParsedSignal:
    result = ParsedSignal()
    notes = result.notes
    target, stop, entry, masked = _extract_prices(text, notes)
    exp_text, exp_date, masked = _extract_expiration(masked, message_ts, notes)
    strike, option_type, masked = _extract_strike_and_type(masked, notes)
    ticker = _extract_ticker(masked, notes)

    result.ticker = ticker
    result.option_type = option_type
    result.strike = strike
    result.expiration_text = exp_text
    result.expiration_date = exp_date
    result.entry = entry
    result.target = target
    result.stop = stop
    return result


def _has_option_content(p: ParsedSignal) -> bool:
    return p.strike != UNKNOWN or p.option_type != UNKNOWN


def parse_message(text: str, message_ts: datetime | None = None, is_reply: bool = False) -> ParsedSignal:
    """Parse one message. `is_reply` is set when an export marks the message as a reply."""
    if not isinstance(text, str):
        raise TypeError("message text must be a string")

    new_text, quoted_text = split_quoted(text)
    is_update = any(re.search(p, new_text, re.IGNORECASE) for p in UPDATE_PATTERNS)

    result = _parse_body(new_text, message_ts)
    quoted_result = _parse_body(quoted_text, message_ts) if quoted_text.strip() else None

    if _has_option_content(result) or (result.ticker != UNKNOWN and is_update):
        if is_update:
            result.kind = UPDATE
        elif quoted_result is not None or is_reply:
            # New text has its own option details, but it's a reply. Treat as an
            # update to the referenced alert rather than a fresh alert.
            result.kind = UPDATE
            result.notes.append("Reply to an earlier message; treated as an update, not a new signal")
        else:
            result.kind = NEW_SIGNAL
    elif quoted_result is not None and _has_option_content(quoted_result):
        # The only option details are inside the quote: this is a quoted signal.
        quoted_result.kind = QUOTED_SIGNAL
        quoted_result.notes.append("Fields come from quoted text, not a new alert")
        if is_update:
            quoted_result.notes.append("New text looks like an update (trim/close/exit) to the quoted alert")
        result = quoted_result
    elif is_update and quoted_result is not None:
        result.kind = UPDATE
    else:
        result.kind = NOT_A_SIGNAL

    if result.kind == UPDATE and result.entry != UNKNOWN:
        result.notes.append("Price in an update message is probably an exit price, not a new entry")

    core = [result.ticker, result.option_type, result.strike, result.expiration_date]
    if result.kind == NOT_A_SIGNAL:
        result.parse_status = FAILED
    elif all(v != UNKNOWN for v in core):
        result.parse_status = COMPLETE
    elif result.ticker == UNKNOWN and result.strike == UNKNOWN:
        result.parse_status = FAILED
    else:
        result.parse_status = PARTIAL
    return result
