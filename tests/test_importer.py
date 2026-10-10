import json
from datetime import datetime, timezone

import pytest

from signal_organizer import importer, storage
from signal_organizer.importer import ImportErrorWithDetail, RawMessage


NOW = datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc)


def test_parse_timestamp_variants():
    assert importer.parse_timestamp("2026-10-09T13:31:00Z").utcoffset() is not None
    assert importer.parse_timestamp("10/09/2026 9:31 AM") is not None
    assert importer.parse_timestamp(1791552660) is not None
    assert importer.parse_timestamp("yesterday-ish") is None
    assert importer.parse_timestamp("") is None
    assert importer.parse_timestamp(None) is None


def test_read_paste_splits_and_reads_timestamps():
    text = "[2026-10-09 09:31] BTO $ACME 50C 10/16 @ 1.20\n---\nno timestamp here ACME 50C"
    msgs, errors = importer.read_paste(text, source="my server")
    assert errors == []
    assert len(msgs) == 2
    assert msgs[0].timestamp == "2026-10-09 09:31"
    assert msgs[0].text == "BTO $ACME 50C 10/16 @ 1.20"
    assert msgs[1].timestamp is None
    assert msgs[0].source == "my server"


def test_read_paste_bracket_that_is_not_a_timestamp_is_kept():
    msgs, _ = importer.read_paste("[LOTTO ALERT] ACME 50C 10/16")
    assert msgs[0].text == "[LOTTO ALERT] ACME 50C 10/16"
    assert msgs[0].timestamp is None


def test_read_paste_empty():
    msgs, errors = importer.read_paste("   ")
    assert msgs == [] and errors


def test_read_paste_too_long_message():
    msgs, errors = importer.read_paste("A" * 5000)
    assert msgs == [] and "longer than" in errors[0]


def test_read_json_object_and_list():
    data = json.dumps({"channel": {"name": "#alerts"}, "messages": [
        {"id": "1", "timestamp": "2026-10-09T13:31:00Z", "author": {"name": "X"}, "content": "ACME 50C"},
        {"id": "2", "content": ""},
        "not an object",
    ]})
    msgs, errors = importer.read_json(data)
    assert len(msgs) == 1
    assert msgs[0].source == "#alerts" and msgs[0].author == "X"
    assert len(errors) == 2

    msgs, _ = importer.read_json(json.dumps([{"text": "ACME 50C", "reply_to": "abc"}]))
    assert msgs[0].is_reply is True


def test_read_json_invalid():
    with pytest.raises(ImportErrorWithDetail):
        importer.read_json("{nope")
    with pytest.raises(ImportErrorWithDetail):
        importer.read_json(json.dumps({"foo": 1}))


def test_read_csv(tmp_path):
    data = "id,timestamp,author,content,reply_to\n1,2026-10-09 09:31,X,\"ACME 50C\nline 2\",\n2,,Y,FAKE 10P,1\n"
    msgs, errors = importer.read_csv(data.encode())
    assert errors == []
    assert msgs[0].text == "ACME 50C\nline 2"
    assert msgs[1].is_reply is True


def test_read_csv_requires_content_column():
    with pytest.raises(ImportErrorWithDetail):
        importer.read_csv(b"a,b\n1,2\n")


def test_ingest_stores_and_flags(conn):
    msgs = [
        RawMessage("BTO $ACME 50C 10/16 @ 1.20", "2026-10-09T13:55:00Z", "s", "1"),
        RawMessage("BTO $ACME 50C 10/16 @ 1.20", "2026-10-09T13:56:00Z", "s", "2"),  # duplicate text
        RawMessage("ACME 50C 9/1/2026", "2026-08-01T13:00:00Z", "s", "3"),  # old + expired
    ]
    summary = importer.ingest(conn, msgs, now=NOW, notify_fn=lambda *a: True)
    assert len(summary.inserted_ids) == 3
    r1, r2, r3 = (storage.get_record(conn, i) for i in summary.inserted_ids)
    assert not r1["is_stale"] and r1["duplicate_of"] is None
    assert r2["duplicate_of"] == r1["id"]
    assert r3["is_stale"]
    assert "expired" in r3["stale_reason"] and "old at import" in r3["stale_reason"]
    assert summary.flagged_duplicates == 1 and summary.flagged_stale == 1
    assert r1["original_text"] == "BTO $ACME 50C 10/16 @ 1.20"
    assert summary.notifications_sent == 0  # notifications are off by default


def test_reimport_same_message_id_is_skipped(conn):
    msg = RawMessage("ACME 50C", "2026-10-09T13:55:00Z", "s", "same-id")
    importer.ingest(conn, [msg], now=NOW)
    summary = importer.ingest(conn, [msg], now=NOW)
    assert summary.skipped_same_id == 1
    assert storage.count_records(conn) == 1


def test_same_text_days_apart_is_not_duplicate(conn):
    importer.ingest(conn, [RawMessage("ACME 50C", "2026-10-01T13:00:00Z")], now=NOW)
    summary = importer.ingest(conn, [RawMessage("ACME 50C", "2026-10-09T13:00:00Z")], now=NOW)
    assert summary.flagged_duplicates == 0


def test_unreadable_timestamp_noted(conn):
    summary = importer.ingest(conn, [RawMessage("ACME 50C", "not a time")], now=NOW)
    r = storage.get_record(conn, summary.inserted_ids[0])
    assert r["message_ts"] is None
    assert any("could not be read" in n for n in r["notes"])


def test_notifications_when_enabled(conn):
    storage.set_setting(conn, "notifications_enabled", "1")
    sent = []
    msgs = [RawMessage(f"ACME {50 + i}C 10/16", f"2026-10-09T13:5{i}:00Z") for i in range(7)]
    msgs.append(RawMessage("hello everyone", "2026-10-09T13:59:00Z"))
    summary = importer.ingest(conn, msgs, now=NOW, notify_fn=lambda t, b: sent.append((t, b)) or True)
    # 5 individual + 1 roll-up, and nothing for the non-signal message
    assert len(sent) == 6
    assert summary.notifications_sent == 6
    assert "ACME" in sent[0][0]
    assert "hello" not in " ".join(t + b for t, b in sent)
