# Discord Signal Organizer

A local Python app that turns options-trading alert messages from Discord into structured records you can filter, review, and log paper trades against.

It is a read-only research and simulation tool:

- It never places, modifies, or cancels orders. It has no brokerage connection.
- It has no Discord connection. You paste messages or import files you are authorized to have.
- It never asks for your Discord password, session cookie, or user token.
- Every record is labeled as unverified third-party information, not a recommendation.

## Quick start

Requires Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m signal_organizer         # demo mode
```

Open http://127.0.0.1:5000.

The app starts in **demo mode**. It uses a separate database (`data/demo.sqlite3`) filled with fictional example alerts. Nothing you do there touches your real data. The demo messages are dated October 2026, so they all show as stale once imported.

When you're ready to use your own data:

```bash
python -m signal_organizer --personal          # uses data/signals.sqlite3
python -m signal_organizer --db path/to.db     # custom location
python -m signal_organizer --port 8080         # different port
```

The server listens on `127.0.0.1` only, so other machines on your network can't reach it.

## Getting messages from Discord

Discord gives server members no documented way to read a channel's messages programmatically. Automating your personal account (a "self-bot") or using your user token breaks Discord's Terms of Service and can get your account banned. This app does neither.

What works:

| Your situation | Option |
| --- | --- |
| Member of the server | Copy alerts from Discord and paste them on the Import page. |
| Member, and the admin is willing | Ask the admin for an export from their own bot, then import the JSON or CSV. |
| Alert channel is an Announcement channel | Follow it into a server you own. A bot in your server could read the copies. Not built yet. |
| You own or admin the server | A Discord bot with the Message Content intent. Not built yet. |

Discord's "Request my data" package contains only messages you sent, so it rarely includes the alerts.

A bot connector would be an external account connection, so it is not included. It can be added later with your approval.

## Importing

### Paste

Separate messages with a line containing only `---`. Start a message with a bracketed time to record when it was posted:

```
[2026-10-09 09:31] BTO $ACME 50C 10/16 @ 1.20 PT 1.80/2.40 SL 0.90
---
[2026-10-09 09:48] FAKE 120 puts exp Oct 23 entry .85 target 1.60 stop .50
```

Without a bracketed time, the message timestamp is stored as UNKNOWN. Lines starting with `> ` are treated as quoted text, matching Discord's quote format.

### JSON

A list of message objects, or `{"messages": [...]}`. Recognized keys (case-insensitive):

- Message text (required): `content`, `text`, `message`, or `body`
- Time: `timestamp`, `time`, `created_at`, `date`
- ID: `id` or `message_id`. Re-importing the same ID from the same source is skipped.
- Author: `author` (string or `{"name": ...}`), `username`
- Source: `channel` (string or `{"name": ...}`), `source`
- Reply marker: `reference`, `message_reference`, `reply_to`

### CSV

A header row with a `content` column, plus any of the keys above as columns.

Limits: 5 MB per file, 5,000 messages per import, 4,000 characters per message.

Naive timestamps (no time zone) are treated as your computer's local time.

Fictional examples of each format are in [`examples/`](examples/).

## How parsing works

For each message the parser extracts:

- ticker, call or put, strike, expiration
- stated entry, stated target(s), stated stop
- the original message timestamp

Rules:

- **Nothing is invented.** A field that isn't in the message is `UNKNOWN`. A field with conflicting values (two tickers, two expirations) is `UNKNOWN`, with a note listing what was found.
- **The original text is stored exactly** and shown next to the parsed fields.
- **Expiration year:** if the message says `10/16` with no year, the year is inferred from the message timestamp (the next 10/16 on or after that date) and a note says so. With no timestamp, the date stays `UNKNOWN`. `0DTE` is noted but not turned into a date.
- **Kinds:**
  - `NEW_SIGNAL`: option details in new text.
  - `QUOTED_SIGNAL`: the only option details are inside a `> ` quote.
  - `UPDATE`: trim, sell-to-close, stopped out, or a reply that restates a contract.
  - `NOT_A_SIGNAL`: no option details.
- **Status:** `COMPLETE` when ticker, type, strike, and expiration date are all known. `PARTIAL` when some are. `FAILED` when nothing usable was found.
- **Stale:** flagged when the message was older than the threshold at import (default 30 minutes, change it in Settings), or when the option had already expired.
- **Duplicate:** flagged when the same text (ignoring case and spacing) appeared within 24 hours of an earlier record. Duplicates are stored and linked to the original.

Tickers are recognized as `$ACME` or as all-caps words of 1 to 5 letters. Common trading jargon (`BTO`, `PT`, `SL`, `EOD`, `TBD`, and so on) is ignored. A few real tickers that are also common words, like `ALL` or `NOW`, are only recognized with a `$` prefix.

## Dashboard

- **Alerts:** records sorted by message time, oldest first by default. Filter by ticker, date range, parse status, and kind.
- **Record page:** full detail, add to the paper journal, delete (with a confirmation page).
- **Paper journal:** log hypothetical decisions (`paper-buy`, `watch`, `skip`), entry, exit, and contracts. Paper P/L uses 100 shares per contract and ignores fees and fills.
- **Settings:** desktop notifications, stale threshold, delete all records (requires typing `DELETE`).

## Desktop notifications

Off by default. Turn them on in Settings. When on, each import shows up to five notifications plus a roll-up for the rest. A notification contains the ticker and a short parsed summary. It never contains author names, message text, or account details.

The app uses tools already on your system:

- Linux: `notify-send` (package `libnotify-bin` on Debian/Ubuntu)
- macOS: `osascript`
- Windows: PowerShell toast notifications

Use "Send a test notification" in Settings to check it works on your machine.

## Security notes

- Data lives in a local SQLite file under `data/`, which is in `.gitignore`.
- No secrets are needed or stored. The web session key is random per launch.
- Logs record counts only, not message text.
- Every form has CSRF protection. Pages send a strict Content Security Policy.
- All SQL uses parameters. Message text is HTML-escaped on display.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Layout:

```
signal_organizer/
  parser.py      text -> structured fields
  importer.py    paste / JSON / CSV readers, stale and duplicate checks
  storage.py     SQLite schema and queries
  notifier.py    optional local desktop notifications
  app.py         Flask dashboard
  templates/, static/
examples/        fictional sample messages
tests/
```
