"""Local web dashboard. Binds to 127.0.0.1 only; there is no brokerage or Discord connection."""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from datetime import date
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for

from . import importer, notifier, storage
from .parser import COMPLETE, FAILED, NEW_SIGNAL, NOT_A_SIGNAL, PARTIAL, QUOTED_SIGNAL, UPDATE

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = PROJECT_ROOT / "examples"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

STATUSES = (COMPLETE, PARTIAL, FAILED)
KINDS = (NEW_SIGNAL, QUOTED_SIGNAL, UPDATE, NOT_A_SIGNAL)
DECISIONS = ("paper-buy", "watch", "skip")


def seed_demo(conn) -> int:
    """Load the fictional example export into an empty demo database."""
    if storage.count_records(conn):
        return 0
    data = (EXAMPLES_DIR / "demo_export.json").read_text(encoding="utf-8")
    messages, _ = importer.read_json(data, default_source="demo (fictional)")
    summary = importer.ingest(conn, messages, notify_fn=lambda *_: False)
    return len(summary.inserted_ids)


def _parse_float(value: str, label: str, errors: list[str], minimum: float = 0.0) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        errors.append(f"{label} must be a number")
        return None
    if number < minimum or number > 1_000_000:
        errors.append(f"{label} is out of range")
        return None
    return number


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def create_app(db_path: str | Path, demo: bool = True, testing: bool = False) -> Flask:
    app = Flask(__name__)
    # A fresh random key per launch. Nothing secret lives in source code.
    app.config.update(
        SECRET_KEY=os.urandom(32),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        DB_PATH=str(db_path),
        DEMO_MODE=demo,
        TESTING=testing,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_HTTPONLY=True,
    )

    with storage.connect(app.config["DB_PATH"]) as conn:
        if demo:
            seeded = seed_demo(conn)
            if seeded:
                log.info("Demo mode: loaded %d fictional example records", seeded)

    def db():
        if "db" not in g:
            g.db = storage.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    # CSRF protection: a per-session token on every form.
    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            sent = request.form.get("csrf", "")
            expected = session.get("csrf", "")
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400, "Form expired or invalid. Reload the page and try again.")

    @app.after_request
    def security_headers(resp):
        resp.headers["Content-Security-Policy"] = "default-src 'self'; form-action 'self'; frame-ancestors 'none'"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.context_processor
    def inject_globals():
        return {"csrf_token": csrf_token, "demo_mode": app.config["DEMO_MODE"]}

    @app.errorhandler(413)
    def too_large(_e):
        flash(f"File too large. Limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.", "error")
        return redirect(url_for("import_view"))

    # Dashboard -------------------------------------------------------------

    @app.get("/")
    def dashboard():
        args = request.args
        ticker = (args.get("ticker") or "").strip().upper()[:10] or None
        status = args.get("status") if args.get("status") in STATUSES else None
        kind = args.get("kind") if args.get("kind") in KINDS else None
        date_from = _parse_date(args.get("from"))
        date_to = _parse_date(args.get("to"))
        newest_first = args.get("order") == "desc"
        records = storage.list_records(
            db(), ticker=ticker, date_from=date_from, date_to=date_to,
            status=status, kind=kind, newest_first=newest_first,
        )
        return render_template(
            "dashboard.html",
            records=records,
            tickers=storage.distinct_tickers(db()),
            statuses=STATUSES,
            kinds=KINDS,
            filters={"ticker": ticker or "", "status": status or "", "kind": kind or "",
                     "from": date_from or "", "to": date_to or "", "order": "desc" if newest_first else "asc"},
        )

    @app.get("/records/<int:record_id>")
    def record_detail(record_id: int):
        record = storage.get_record(db(), record_id)
        if not record:
            abort(404)
        return render_template("record.html", r=record, decisions=DECISIONS)

    # Import ----------------------------------------------------------------

    def _report(summary: importer.ImportSummary, read_errors: list[str]) -> None:
        stored = len(summary.inserted_ids)
        msg = f"Stored {stored} record(s)."
        if summary.skipped_same_id:
            msg += f" Skipped {summary.skipped_same_id} already-imported message(s)."
        if summary.flagged_duplicates:
            msg += f" Flagged {summary.flagged_duplicates} duplicate(s)."
        if summary.flagged_stale:
            msg += f" Flagged {summary.flagged_stale} stale."
        if summary.notifications_sent:
            msg += f" Sent {summary.notifications_sent} desktop notification(s)."
        flash(msg, "ok" if stored else "info")
        for err in read_errors[:20]:
            flash(err, "error")
        if len(read_errors) > 20:
            flash(f"...and {len(read_errors) - 20} more problems", "error")

    @app.get("/import")
    def import_view():
        return render_template("import.html")

    @app.post("/import/paste")
    def import_paste():
        text = request.form.get("text", "")
        source = request.form.get("source", "").strip() or "manual paste"
        messages, errors = importer.read_paste(text, source=source)
        if not messages:
            for err in errors or ["No messages found"]:
                flash(err, "error")
            return redirect(url_for("import_view"))
        _report(importer.ingest(db(), messages), errors)
        return redirect(url_for("dashboard", order="desc"))

    @app.post("/import/file")
    def import_file():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            flash("Choose a .json or .csv file to import.", "error")
            return redirect(url_for("import_view"))
        name = upload.filename.lower()
        data = upload.read()
        try:
            if name.endswith(".json"):
                messages, errors = importer.read_json(data, default_source=f"file: {Path(name).name[:80]}")
            elif name.endswith(".csv"):
                messages, errors = importer.read_csv(data, default_source=f"file: {Path(name).name[:80]}")
            else:
                flash("Only .json and .csv files are supported.", "error")
                return redirect(url_for("import_view"))
        except importer.ImportErrorWithDetail as exc:
            flash(str(exc), "error")
            return redirect(url_for("import_view"))
        if not messages:
            flash("No messages found in the file.", "error")
            for err in errors[:20]:
                flash(err, "error")
            return redirect(url_for("import_view"))
        _report(importer.ingest(db(), messages), errors)
        return redirect(url_for("dashboard", order="desc"))

    # Deletion (always behind a confirmation page) ---------------------------

    @app.route("/records/<int:record_id>/delete", methods=["GET", "POST"])
    def delete_record(record_id: int):
        record = storage.get_record(db(), record_id)
        if not record:
            abort(404)
        if request.method == "GET":
            return render_template(
                "confirm_delete.html",
                what=f"record #{record_id} ({record['ticker']})",
                preview=record["original_text"],
                action=url_for("delete_record", record_id=record_id),
                cancel=url_for("record_detail", record_id=record_id),
                require_typed=False,
            )
        if request.form.get("confirm") != "yes":
            flash("Not deleted: confirmation missing.", "error")
            return redirect(url_for("record_detail", record_id=record_id))
        storage.delete_record(db(), record_id)
        flash(f"Deleted record #{record_id}.", "ok")
        return redirect(url_for("dashboard"))

    @app.route("/records/delete-all", methods=["GET", "POST"])
    def delete_all():
        if request.method == "GET":
            return render_template(
                "confirm_delete.html",
                what=f"ALL {storage.count_records(db())} records",
                preview=None,
                action=url_for("delete_all"),
                cancel=url_for("settings_view"),
                require_typed=True,
            )
        if request.form.get("confirm") != "yes" or request.form.get("typed") != "DELETE":
            flash("Not deleted: type DELETE to confirm.", "error")
            return redirect(url_for("delete_all"))
        count = storage.delete_all_records(db())
        flash(f"Deleted {count} records.", "ok")
        return redirect(url_for("dashboard"))

    # Paper-trading journal -------------------------------------------------

    @app.get("/journal")
    def journal_view():
        entries = storage.list_journal(db())
        closed = [e["pnl"] for e in entries if e["pnl"] is not None]
        return render_template(
            "journal.html", entries=entries, decisions=DECISIONS,
            total_pnl=round(sum(closed), 2) if closed else None, closed_count=len(closed),
        )

    @app.post("/journal")
    def journal_add():
        f = request.form
        errors: list[str] = []
        record_id = f.get("record_id", "").strip()
        rid = None
        if record_id:
            if not record_id.isdigit() or not storage.get_record(db(), int(record_id)):
                errors.append("Linked record not found")
            else:
                rid = int(record_id)
        ticker = f.get("ticker", "").strip().upper()
        if not ticker or not ticker.isalpha() or len(ticker) > 6:
            errors.append("Ticker must be 1 to 6 letters")
        option_type = f.get("option_type", "UNKNOWN")
        if option_type not in ("CALL", "PUT", "UNKNOWN"):
            errors.append("Type must be CALL, PUT, or UNKNOWN")
        decision = f.get("decision", "")
        if decision not in DECISIONS:
            errors.append("Pick a decision")
        entry_price = _parse_float(f.get("hypothetical_entry", ""), "Hypothetical entry", errors)
        exit_price = _parse_float(f.get("hypothetical_exit", ""), "Hypothetical exit", errors)
        contracts_raw = f.get("contracts", "").strip()
        contracts = None
        if contracts_raw:
            if not contracts_raw.isdigit() or not 0 < int(contracts_raw) <= 10_000:
                errors.append("Contracts must be a whole number from 1 to 10000")
            else:
                contracts = int(contracts_raw)
        if errors:
            for e in errors:
                flash(e, "error")
            return redirect(request.referrer or url_for("journal_view"))
        storage.insert_journal_entry(db(), {
            "record_id": rid,
            "ticker": ticker,
            "option_type": option_type,
            "strike": f.get("strike", "").strip()[:20] or "UNKNOWN",
            "expiration": f.get("expiration", "").strip()[:20] or "UNKNOWN",
            "decision": decision,
            "hypothetical_entry": entry_price,
            "hypothetical_exit": exit_price,
            "contracts": contracts,
            "notes": f.get("notes", "").strip()[:2000],
        })
        flash("Paper journal entry saved. No order was placed anywhere.", "ok")
        return redirect(url_for("journal_view"))

    @app.post("/journal/<int:entry_id>/exit")
    def journal_exit(entry_id: int):
        entry = storage.get_journal_entry(db(), entry_id)
        if not entry:
            abort(404)
        errors: list[str] = []
        exit_price = _parse_float(request.form.get("hypothetical_exit", ""), "Hypothetical exit", errors)
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            notes = request.form.get("notes", entry["notes"]).strip()[:2000]
            storage.update_journal_exit(db(), entry_id, exit_price, notes)
            flash("Paper outcome recorded.", "ok")
        return redirect(url_for("journal_view"))

    @app.route("/journal/<int:entry_id>/delete", methods=["GET", "POST"])
    def journal_delete(entry_id: int):
        entry = storage.get_journal_entry(db(), entry_id)
        if not entry:
            abort(404)
        if request.method == "GET":
            return render_template(
                "confirm_delete.html",
                what=f"paper journal entry #{entry_id} ({entry['ticker']})",
                preview=entry["notes"] or None,
                action=url_for("journal_delete", entry_id=entry_id),
                cancel=url_for("journal_view"),
                require_typed=False,
            )
        if request.form.get("confirm") != "yes":
            flash("Not deleted: confirmation missing.", "error")
            return redirect(url_for("journal_view"))
        storage.delete_journal_entry(db(), entry_id)
        flash(f"Deleted journal entry #{entry_id}.", "ok")
        return redirect(url_for("journal_view"))

    # Settings --------------------------------------------------------------

    @app.route("/settings", methods=["GET", "POST"])
    def settings_view():
        if request.method == "POST":
            enabled = "1" if request.form.get("notifications_enabled") == "on" else "0"
            stale = request.form.get("stale_minutes", "30").strip()
            if not stale.isdigit() or not 1 <= int(stale) <= 10_080:
                flash("Stale threshold must be 1 to 10080 minutes.", "error")
            else:
                storage.set_setting(db(), "notifications_enabled", enabled)
                storage.set_setting(db(), "stale_minutes", str(int(stale)))
                flash("Settings saved. The stale threshold applies to future imports.", "ok")
            return redirect(url_for("settings_view"))
        return render_template(
            "settings.html",
            notifications_enabled=storage.get_setting(db(), "notifications_enabled") == "1",
            stale_minutes=storage.get_setting(db(), "stale_minutes"),
            notifications_available=notifier.is_available(),
            record_count=storage.count_records(db()),
            db_path=app.config["DB_PATH"],
        )

    @app.post("/settings/test-notification")
    def test_notification():
        ok = notifier.notify("Signal Organizer", "Test notification. No data was sent anywhere.")
        flash("Test notification sent." if ok else "Desktop notifications are not available on this system.",
              "ok" if ok else "error")
        return redirect(url_for("settings_view"))

    @app.get("/discord-access")
    def discord_access():
        return render_template("discord_access.html")

    @app.template_filter("pretty_ts")
    def pretty_ts(value):
        if not value:
            return "UNKNOWN"
        return value.replace("T", " ")[:19]

    return app
