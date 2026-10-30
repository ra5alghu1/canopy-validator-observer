"""Offline tests: no Telegram API or validator is contacted."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import canopy_alerts

from canopy_alerts import AlertError, notify_status_change


def report(status):
    return {
        "status": status,
        "checks": [
            {"name": "peers", "status": status, "message": "no connected peers"}
        ],
    }


class TelegramAlertTests(unittest.TestCase):
    def test_disabled_does_not_write_or_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = Mock()
            self.assertFalse(notify_status_change(report("CRITICAL"), Path(tmp), "", "123", sender))
            sender.assert_not_called()
            self.assertFalse((Path(tmp) / "alert-state.json").exists())

    def test_initial_ok_baseline_without_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = Mock()
            directory = Path(tmp)
            self.assertFalse(notify_status_change(report("OK"), directory, "token", "123", sender))
            sender.assert_not_called()
            self.assertEqual(json.loads((directory / "alert-state.json").read_text())["status"], "OK")

    def test_transition_alerts_once_and_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            sender = Mock()
            notify_status_change(report("OK"), directory, "token", "123", sender)
            self.assertTrue(notify_status_change(report("WARNING"), directory, "token", "123", sender))
            self.assertFalse(notify_status_change(report("WARNING"), directory, "token", "123", sender))
            self.assertTrue(notify_status_change(report("CRITICAL"), directory, "token", "123", sender))
            self.assertFalse(notify_status_change(report("CRITICAL"), directory, "token", "123", sender))
            # A new invocation reads the state file instead of alerting again.
            self.assertTrue(notify_status_change(report("OK"), directory, "token", "123", sender))
            self.assertFalse(notify_status_change(report("OK"), directory, "token", "123", sender))
            self.assertEqual(sender.call_count, 3)
            self.assertIn("recovered", sender.call_args.args[2])

    def test_initial_critical_sends_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = Mock()
            self.assertTrue(notify_status_change(report("CRITICAL"), Path(tmp), "token", "123", sender))
            self.assertIn("CRITICAL", sender.call_args.args[2])

    def test_failed_delivery_retains_old_state_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            notify_status_change(report("OK"), directory, "token", "123", Mock())
            failing = Mock(side_effect=AlertError("network unavailable"))
            with self.assertRaises(AlertError):
                notify_status_change(report("CRITICAL"), directory, "token", "123", failing)
            self.assertEqual(json.loads((directory / "alert-state.json").read_text())["status"], "OK")
            success = Mock()
            self.assertTrue(notify_status_change(report("CRITICAL"), directory, "token", "123", success))
            success.assert_called_once()

    def test_concurrent_invocations_send_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            sender = Mock()
            with ThreadPoolExecutor(max_workers=8) as executor:
                outcomes = list(executor.map(
                    lambda _: notify_status_change(report("CRITICAL"), directory, "token", "123", sender),
                    range(8),
                ))
            self.assertEqual(outcomes.count(True), 1)
            self.assertEqual(outcomes.count(False), 7)
            sender.assert_called_once()

    def test_state_write_failure_before_send_blocks_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = Mock()
            with patch("canopy_alerts._save_status", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    notify_status_change(report("CRITICAL"), Path(tmp), "token", "123", sender)
            sender.assert_not_called()

    def test_failed_final_save_keeps_pending_and_suppresses_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            sender = Mock()
            real_save = canopy_alerts._save_status

            def fail_final_save(path, status, pending=None):
                if status == "CRITICAL" and pending is None:
                    raise OSError("disk full after send")
                return real_save(path, status, pending)

            with patch("canopy_alerts._save_status", side_effect=fail_final_save):
                with self.assertRaises(OSError):
                    notify_status_change(report("CRITICAL"), directory, "token", "123", sender)
            sender.assert_called_once()
            self.assertEqual(
                json.loads((directory / "alert-state.json").read_text())["pending"],
                "CRITICAL",
            )
            self.assertFalse(notify_status_change(report("CRITICAL"), directory, "token", "123", sender))
            sender.assert_called_once()

    def test_unknown_status_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = Mock()
            self.assertFalse(notify_status_change(report("BROKEN"), Path(tmp), "token", "123", sender))
            sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
