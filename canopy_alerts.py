"""Optional outbound Telegram alerts for Canopy status transitions.

Uses Telegram sendMessage only; never polls updates or starts a second bot.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


STATUSES = {"OK", "WARNING", "CRITICAL"}
STATE_FILE = "alert-state.json"


class AlertError(RuntimeError):
    """Telegram notification failed; retain previous state for retry."""


def send_telegram_message(token: str, chat_id: str, message: str) -> None:
    """Send an outbound message; do not consume Telegram bot updates."""
    data = urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise AlertError(f"Telegram send failed ({type(exc).__name__})") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise AlertError("Telegram API did not confirm delivery")


def _last_status(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("status") in STATUSES:
        return data["status"]
    return None


def _save_status(path: Path, status: str) -> None:
    """Replace the state atomically so a restart cannot read partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".alert-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()},
                stream,
            )
            stream.write("\n")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _message(report: dict[str, Any], previous: str | None) -> str:
    status = report["status"]
    if status == "OK":
        return "🟢 Canopy validator recovered: OK (previous: " + str(previous) + ")"
    icon = "🚨" if status == "CRITICAL" else "⚠️"
    lines = [f"{icon} Canopy validator: {status} (previous: {previous or 'unknown'})"]
    for check in report.get("checks", []):
        if not isinstance(check, dict) or check.get("status") == "OK":
            continue
        if check.get("status") not in STATUSES:
            continue
        name = str(check.get("name", "check"))[:60]
        detail = str(check.get("message", ""))[:250]
        lines.append(f"• {name}: {detail}")
        if len(lines) >= 5:
            break
    return "\n".join(lines)[:3500]


def notify_status_change(
    report: dict[str, Any],
    report_directory: Path,
    token: str,
    chat_id: str,
    sender: Callable[[str, str, str], None] | None = None,
) -> bool:
    """Send once per status transition; persist state only after success.

    Disabled unless both credentials are configured. The first healthy report
    establishes a baseline without sending. An initially unhealthy report alerts.
    On delivery failure the old state is retained and the next run retries.
    """
    if not token or not chat_id:
        return False
    status = report.get("status")
    if status not in STATUSES:
        return False

    state_path = report_directory / STATE_FILE
    previous = _last_status(state_path)
    if previous == status:
        return False
    if previous is None and status == "OK":
        _save_status(state_path, status)
        return False

    (sender or send_telegram_message)(token, chat_id, _message(report, previous))
    _save_status(state_path, status)
    return True
