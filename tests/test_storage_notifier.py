import subprocess

import pytest

from signal_organizer import notifier, storage


def test_settings_roundtrip(conn):
    assert storage.get_setting(conn, "notifications_enabled") == "0"
    storage.set_setting(conn, "stale_minutes", "45")
    assert storage.get_setting(conn, "stale_minutes") == "45"
    with pytest.raises(KeyError):
        storage.set_setting(conn, "api_token", "x")


def test_journal_pnl(conn):
    eid = storage.insert_journal_entry(conn, {
        "ticker": "ACME", "option_type": "CALL", "strike": "50", "expiration": "2026-10-16",
        "decision": "paper-buy", "hypothetical_entry": 1.20, "hypothetical_exit": None, "contracts": 2,
    })
    assert storage.get_journal_entry(conn, eid)["pnl"] is None
    storage.update_journal_exit(conn, eid, 1.75, "closed on paper")
    assert storage.get_journal_entry(conn, eid)["pnl"] == 110.0
    assert storage.delete_journal_entry(conn, eid)


def test_summary_has_no_sensitive_fields():
    record = {"ticker": "ACME", "strike": "50", "option_type": "CALL", "expiration_text": "10/16",
              "kind": "NEW_SIGNAL", "author": "SomeUser", "original_text": "secret stuff", "source": "#private"}
    title, body = notifier.summarize(record)
    combined = title + body
    assert "ACME" in title and "50 CALL 10/16" in body
    for leaked in ("SomeUser", "secret", "private"):
        assert leaked not in combined


def test_notify_strips_quotes_and_never_raises(monkeypatch):
    monkeypatch.setattr(notifier, "_command", lambda t, b: ["echo", t, b])
    calls = []
    assert notifier.notify('A"; rm -rf /', "b'c", runner=lambda cmd, **kw: calls.append(cmd))
    assert '"' not in calls[0][1] and ";" not in calls[0][1] and "'" not in calls[0][2]

    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)
    assert notifier.notify("t", "b", runner=boom) is False


def test_notify_unavailable(monkeypatch):
    monkeypatch.setattr(notifier, "_command", lambda t, b: None)
    assert notifier.notify("t", "b") is False
