import io
import re

import pytest

from signal_organizer import storage
from signal_organizer.app import create_app


def _csrf(client, path="/import"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "t.sqlite3", demo=False, testing=True)
    return app.test_client()


@pytest.fixture
def demo_client(tmp_path):
    app = create_app(tmp_path / "demo.sqlite3", demo=True, testing=True)
    return app.test_client()


def test_demo_mode_seeds_fictional_data(demo_client):
    html = demo_client.get("/").get_data(as_text=True)
    assert "Demo mode" in html
    assert "unverified third-party information" in html
    assert "ACME" in html and "DUPLICATE" in html and "QUOTED SIGNAL" in html


def test_dashboard_filters(demo_client):
    html = demo_client.get("/?ticker=FAKE").get_data(as_text=True)
    assert "FAKE 120 puts" in html and "BTO $ACME" not in html
    html = demo_client.get("/?status=FAILED").get_data(as_text=True)
    assert "Good morning" in html and "FAKE 120" not in html
    html = demo_client.get("/?from=2026-09-01&to=2026-09-30").get_data(as_text=True)
    assert "XMPL 75P" in html and "ACME 50C" not in html
    assert demo_client.get("/?from=garbage").status_code == 200


def test_chronological_order(demo_client):
    html = demo_client.get("/").get_data(as_text=True)
    assert html.index("XMPL 75P") < html.index("BTO $ACME 50C")


def test_post_without_csrf_rejected(client):
    assert client.post("/import/paste", data={"text": "ACME 50C"}).status_code == 400


def test_paste_import(client):
    token = _csrf(client)
    resp = client.post("/import/paste", data={"csrf": token, "text": "[2026-10-09 09:31] BTO $ACME 50C 10/16 @ 1.20"},
                       follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert "Stored 1 record" in html
    assert "BTO $ACME 50C 10/16 @ 1.20" in html


def test_html_in_messages_is_escaped(client):
    token = _csrf(client)
    html = client.post("/import/paste", data={"csrf": token, "text": "<script>alert(1)</script> ACME 50C"},
                       follow_redirects=True).get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_file_import_csv_and_bad_files(client):
    token = _csrf(client)
    csv_bytes = b"content,timestamp\nACME 50C 10/16,2026-10-09 09:31\n"
    html = client.post("/import/file", data={"csrf": token, "file": (io.BytesIO(csv_bytes), "x.csv")},
                       content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
    assert "Stored 1 record" in html
    html = client.post("/import/file", data={"csrf": token, "file": (io.BytesIO(b"{bad"), "x.json")},
                       content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
    assert "Not valid JSON" in html
    html = client.post("/import/file", data={"csrf": token, "file": (io.BytesIO(b"x"), "x.exe")},
                       content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
    assert "Only .json and .csv" in html


def test_delete_requires_confirmation(demo_client):
    token = _csrf(demo_client)
    page = demo_client.get("/records/1/delete").get_data(as_text=True)
    assert "cannot be undone" in page
    demo_client.post("/records/1/delete", data={"csrf": token})
    assert demo_client.get("/records/1").status_code == 200  # still there
    demo_client.post("/records/1/delete", data={"csrf": token, "confirm": "yes"})
    assert demo_client.get("/records/1").status_code == 404


def test_delete_all_requires_typed_confirmation(demo_client, tmp_path):
    token = _csrf(demo_client)
    demo_client.post("/records/delete-all", data={"csrf": token, "confirm": "yes", "typed": "delete"})
    assert "No records match" not in demo_client.get("/").get_data(as_text=True)
    demo_client.post("/records/delete-all", data={"csrf": token, "confirm": "yes", "typed": "DELETE"})
    assert "No records match" in demo_client.get("/").get_data(as_text=True)


def test_journal_flow(demo_client):
    token = _csrf(demo_client, "/journal")
    resp = demo_client.post("/journal", data={
        "csrf": token, "record_id": "1", "ticker": "acme", "option_type": "CALL", "strike": "50",
        "expiration": "2026-10-16", "decision": "paper-buy", "hypothetical_entry": "1.20", "contracts": "1",
    }, follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert "No order was placed" in html and "ACME" in html
    demo_client.post("/journal/1/exit", data={"csrf": token, "hypothetical_exit": "1.75", "notes": "done"})
    assert "+55.00" in demo_client.get("/journal").get_data(as_text=True)


def test_journal_validation(client):
    token = _csrf(client, "/journal")
    html = client.post("/journal", data={
        "csrf": token, "ticker": "AC ME!", "decision": "yolo", "hypothetical_entry": "abc", "contracts": "-3",
    }, follow_redirects=True).get_data(as_text=True)
    assert "Ticker must be" in html and "Pick a decision" in html
    assert "must be a number" in html and "Contracts must be" in html


def test_settings(client):
    token = _csrf(client, "/settings")
    client.post("/settings", data={"csrf": token, "notifications_enabled": "on", "stale_minutes": "60"})
    html = client.get("/settings").get_data(as_text=True)
    assert 'value="60"' in html and "checked" in html
    html = client.post("/settings", data={"csrf": token, "stale_minutes": "abc"},
                       follow_redirects=True).get_data(as_text=True)
    assert "Stale threshold must be" in html


def test_security_headers_and_no_order_routes(client):
    resp = client.get("/")
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "DENY"
    rules = {r.rule for r in client.application.url_map.iter_rules()}
    assert not any(word in rule for rule in rules for word in ("order", "broker", "trade"))
