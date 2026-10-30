"""Optional outbound Telegram alerts for Canopy status transitions.

Uses Telegram sendMessage only; never polls updates or starts a second bot.
"""

from __future__ import annotations

import fcntl
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


def _read_state(path: Path) -> dict[str, str | None]:
    """Read committed and in-flight alert states."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": None, "pending": None}
    if not isinstance(data, dict):
        return {"status": None, "pending": None}
    return {
        "status": data.get("status") if data.get("status") in STATUSES else None,
        "pending": data.get("pending") if data.get("pending") in STATUSES else None,
    }


def _save_status(path: Path, status: str | None, pending: str | None = None) -> None:
    """Atomically record the state before and after an outbound send."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".alert-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "status": status,
                    "pending": pending,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
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
    """Serialize sends and durably reserve each transition before delivery.

    An interrupted/ambiguous send stays pending and is not retried automatically:
    this favors avoiding duplicate alerts over guaranteed delivery. A confirmed
    send failure clears the reservation, allowing a retry on the next run.
    """
    if not token or not chat_id:
        return False
    status = report.get("status")
    if status not in STATUSES:
        return False

    # flock serializes separate observer processes as well as threads.
    # Keep the lock throughout the network send to prevent duplicate delivery.
    report_directory.mkdir(parents=True, exist_ok=True)
    state_path = report_directory / STATE_FILE
    lock_path = report_directory / ".alert-state.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _read_state(state_path)
        previous = state["pending"] or state["status"]
        if previous == status:
            return False
        if previous is None and status == "OK":
            _save_status(state_path, status)
            return False

        # Fail closed if disk persistence fails: never send without a
        # durable reservation. If final commit fails after a successful
        # send, pending prevents duplicate delivery after a restart.
        _save_status(state_path, state["status"], pending=status)
        try:
            (sender or send_telegram_message)(token, chat_id, _message(report, previous))
        except Exception:
            # Known delivery failure: restore the prior state so the next
            # run can retry. If rollback itself fails, pending suppresses
            # duplicates rather than risking a second send.
            _save_status(state_path, state["status"], pending=state["pending"])
            raise
        _save_status(state_path, status)
        return True
