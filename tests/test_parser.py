from datetime import datetime, timezone

import pytest

from signal_organizer.parser import (
    COMPLETE, FAILED, NEW_SIGNAL, NOT_A_SIGNAL, PARTIAL, QUOTED_SIGNAL, UNKNOWN, UPDATE,
    parse_message, split_quoted,
)

TS = datetime(2026, 10, 9, 13, 31, tzinfo=timezone.utc)


def test_full_signal():
    p = parse_message("BTO $ACME 50C 10/16 @ 1.20 PT 1.80/2.40 SL 0.90", TS)
    assert p.kind == NEW_SIGNAL
    assert p.parse_status == COMPLETE
    assert (p.ticker, p.option_type, p.strike) == ("ACME", "CALL", "50")
    assert p.expiration_text == "10/16"
    assert p.expiration_date == "2026-10-16"
    assert (p.entry, p.target, p.stop) == ("1.20", "1.80 / 2.40", "0.90")
    assert any("inferred from message timestamp" in n for n in p.notes)


def test_words_and_month_names():
    p = parse_message("FAKE 120 puts exp Oct 23 entry .85 target 1.60 stop .50", TS)
    assert (p.ticker, p.option_type, p.strike) == ("FAKE", "PUT", "120")
    assert p.expiration_date == "2026-10-23"
    assert (p.entry, p.target, p.stop) == ("0.85", "1.60", "0.50")


def test_iso_expiration_and_target_with_slash_not_a_date():
    p = parse_message("$XMPL 12.5P 2026-11-20 @ .40 PT 2/3", TS)
    assert p.expiration_date == "2026-11-20"
    assert p.target == "2 / 3"
    assert p.strike == "12.5"
    assert not any("ambiguous" in n.lower() for n in p.notes)


def test_explicit_year_not_inferred():
    p = parse_message("BTO ACME 50C 1/15/27 @ 3.00", TS)
    assert p.expiration_date == "2027-01-15"
    assert not any("inferred" in n for n in p.notes)


def test_year_rolls_forward_when_month_already_passed():
    p = parse_message("ACME 50C 1/15 @ 3.00", TS)
    assert p.expiration_date == "2027-01-15"


def test_missing_fields_are_unknown_not_invented():
    p = parse_message("TSLA 250c 0DTE @ 1.10", TS)
    assert p.expiration_text == UNKNOWN
    assert p.expiration_date == UNKNOWN
    assert p.target == UNKNOWN and p.stop == UNKNOWN
    assert p.parse_status == PARTIAL
    assert any("0DTE" in n for n in p.notes)


def test_no_timestamp_means_no_year_inference():
    p = parse_message("ACME 50C 10/16 @ 1.20", None)
    assert p.expiration_text == "10/16"
    assert p.expiration_date == UNKNOWN
    assert p.parse_status == PARTIAL


def test_ambiguous_ticker():
    p = parse_message("ZZZT or XMPL 80C 11/20", TS)
    assert p.ticker == UNKNOWN
    assert any("Ticker ambiguous" in n for n in p.notes)


def test_caps_jargon_not_ticker():
    p = parse_message("ZZZT 80C 11/20, entry TBD. IMO hold till EOD", TS)
    assert p.ticker == "ZZZT"
    assert p.entry == UNKNOWN


def test_ambiguous_type():
    p = parse_message("ACME 50C and 45P 10/16", TS)
    assert p.option_type == UNKNOWN
    assert p.strike == UNKNOWN


def test_conflicting_expirations():
    p = parse_message("ACME 50C 10/16 or 10/23", TS)
    assert p.expiration_date == UNKNOWN
    assert any("Expiration ambiguous" in n for n in p.notes)


def test_invalid_calendar_date():
    p = parse_message("ACME 50C 2/30/2027", TS)
    assert p.expiration_date == UNKNOWN


def test_not_a_signal():
    p = parse_message("good morning everyone, market opens at 9:30", TS)
    assert p.kind == NOT_A_SIGNAL
    assert p.parse_status == FAILED
    assert p.entry == UNKNOWN


def test_quoted_signal_is_not_new():
    p = parse_message("> BTO QQQ 380P 10/17 @ 2.10\nis this still good?", TS)
    assert p.kind == QUOTED_SIGNAL
    assert p.ticker == "QQQ"


def test_reply_with_details_is_update():
    p = parse_message("ACME 50C 10/16 still in", TS, is_reply=True)
    assert p.kind == UPDATE


def test_update_message():
    p = parse_message("STC $ACME 50C 10/16 @ 1.75", TS)
    assert p.kind == UPDATE
    assert any("exit price" in n for n in p.notes)


def test_block_quote():
    new, quoted = split_quoted("hello\n>>> line one\nline two")
    assert new == "hello"
    assert quoted == "line one\nline two"


def test_original_text_not_mutated():
    text = "BTO $ACME 50C 10/16 @ 1.20"
    parse_message(text, TS)
    assert text == "BTO $ACME 50C 10/16 @ 1.20"


def test_time_of_day_not_entry():
    p = parse_message("ACME 50C 10/16 alert at 9:45", TS)
    assert p.entry == UNKNOWN


def test_rejects_non_string():
    with pytest.raises(TypeError):
        parse_message(None)  # type: ignore[arg-type]
