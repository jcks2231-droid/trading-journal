"""Launch the local dashboard: python -m signal_organizer [--personal] [--port 5000]"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .app import PROJECT_ROOT, create_app

DATA_DIR = PROJECT_ROOT / "data"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Discord Signal Organizer (local, read-only research tool)")
    ap.add_argument("--personal", action="store_true",
                    help="use your personal database instead of the demo database")
    ap.add_argument("--db", type=Path, help="custom SQLite file path (implies --personal)")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    demo = not (args.personal or args.db)
    db_path = args.db or (DATA_DIR / ("demo.sqlite3" if demo else "signals.sqlite3"))
    app = create_app(db_path, demo=demo)

    mode = "DEMO mode (fictional data)" if demo else "personal mode"
    print(f"\nDiscord Signal Organizer, {mode}")
    print(f"Database: {db_path}")
    print(f"Open http://127.0.0.1:{args.port} in your browser. Press Ctrl+C to stop.\n")
    # 127.0.0.1 only: the dashboard is not reachable from other machines.
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
