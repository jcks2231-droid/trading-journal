"""Optional local desktop notifications.

Uses only tools already on the machine (notify-send on Linux, osascript on
macOS, PowerShell on Windows). Nothing is sent over the network. Notification
text contains the ticker and a short parsed summary only: no account data, no
author names, no original message text.
"""

from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess

log = logging.getLogger(__name__)

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9 .,:/()\-]")


def _clean(text: str, limit: int = 120) -> str:
    return _SAFE_CHARS.sub("", text)[:limit]


def summarize(record: dict) -> tuple[str, str]:
    """Build (title, body) for a parsed record."""
    ticker = record.get("ticker", "UNKNOWN")
    parts = [record.get("strike", "UNKNOWN"), record.get("option_type", "UNKNOWN"),
             record.get("expiration_text", "UNKNOWN")]
    detail = " ".join(p for p in parts if p and p != "UNKNOWN") or "details unknown"
    kind = record.get("kind", "").replace("_", " ").lower()
    title = _clean(f"Signal Organizer: {ticker}", 60)
    body = _clean(f"{detail} ({kind}). Unverified third-party alert.")
    return title, body


def _command(title: str, body: str) -> list[str] | None:
    system = platform.system()
    if system == "Linux" and shutil.which("notify-send"):
        return ["notify-send", "--app-name=Signal Organizer", title, body]
    if system == "Darwin" and shutil.which("osascript"):
        return ["osascript", "-e", f'display notification "{body}" with title "{title}"']
    if system == "Windows" and shutil.which("powershell"):
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] > $null;"
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{title}')) > $null;"
            f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode('{body}')) > $null;"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
            "'Signal Organizer').Show([Windows.UI.Notifications.ToastNotification]::new($t))"
        )
        return ["powershell", "-NoProfile", "-Command", script]
    return None


def is_available() -> bool:
    return _command("t", "b") is not None


def notify(title: str, body: str, runner=subprocess.run) -> bool:
    """Show a notification. Returns True on success. Never raises."""
    title, body = _clean(title, 60), _clean(body)
    cmd = _command(title, body)
    if cmd is None:
        log.info("Desktop notifications not available on this system")
        return False
    try:
        runner(cmd, check=True, timeout=5, capture_output=True)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Desktop notification failed: %s", type(exc).__name__)
        return False
