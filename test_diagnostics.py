"""Offline checks for actionable, printable run diagnostics."""

import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import core


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.log = Path(directory.name) / "debug.log"
        for name, value in (("DEBUG_LOG", str(self.log)), ("ERROR_HOOK", None),
                            ("DEBUG_TRACE", False)):
            patcher = patch.object(core, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_errors_print_persist_and_deliver_context_with_tracing_disabled(self):
        received = []
        output = io.StringIO()
        with patch.object(core, "ERROR_HOOK", received.append), contextlib.redirect_stdout(output):
            event = core.report_error(
                "COMMENT_NO_BOX", "phone-a", account_num=2, link_no=3,
                url="https://www.facebook.com/live/123", detail="composer missing")
        self.assertEqual(received, [event])
        self.assertEqual(event["account"], 2)
        self.assertEqual(event["link"], 3)
        self.assertIn("[ERROR COMMENT_NO_BOX]", output.getvalue())
        self.assertIn("account=2 link=3", output.getvalue())
        self.assertIn("comments are enabled", event["hint"])
        self.assertEqual(output.getvalue(), self.log.read_text())

    def test_credentials_are_redacted_and_untrusted_lines_are_flattened(self):
        with contextlib.redirect_stdout(io.StringIO()):
            event = core.report_error(
                "SESSION_FAILED", "phone\nforged", detail="api_key=secret password=hunter2\nnext",
                url="https://www.facebook.com/posts/123?access_token=private&foo=1",
                exc=RuntimeError("Authorization: Bearer hidden"))
        logged = self.log.read_text()
        for secret in ("secret", "hunter2", "private", "hidden"):
            self.assertNotIn(secret, logged)
        self.assertNotIn("\n", event["detail"])
        self.assertEqual(event["serial"], "phone forged")
        self.assertIn("RuntimeError", event["detail"])
        self.assertEqual(len(logged.splitlines()), 1)

    def test_missing_console_readonly_log_and_failing_hook_do_not_crash_worker(self):
        with patch.object(core, "DEBUG_LOG", str(self.log / "missing")), \
             patch.object(core, "ERROR_HOOK", side_effect=RuntimeError("closed window")), \
             patch("builtins.print", side_effect=OSError("no console")):
            event = core.report_error("DEVICE_WORKER_FAILED", exc=ValueError("bad state"))
        self.assertEqual(event["code"], "DEVICE_WORKER_FAILED")
        self.assertIn("ValueError", event["detail"])

    def test_parallel_device_errors_write_complete_lines(self):
        with contextlib.redirect_stdout(io.StringIO()):
            workers = [threading.Thread(target=core.report_error,
                                        args=("COMMENT_NO_BOX", f"phone-{n}"))
                       for n in range(12)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        lines = self.log.read_text().splitlines()
        self.assertEqual(len(lines), 12)
        for n in range(12):
            self.assertEqual(sum(f"[phone-{n}]" in line for line in lines), 1)

    def test_log_rotation_preserves_previous_log(self):
        self.log.write_text("x" * 5_000_001)
        with contextlib.redirect_stdout(io.StringIO()):
            core.report_error("DEVICE_OFFLINE", "phone-a")
        self.assertEqual(Path(str(self.log) + ".1").stat().st_size, 5_000_001)
        self.assertIn("DEVICE_OFFLINE", self.log.read_text())

    def test_missing_adb_returns_actionable_failure_instead_of_crashing(self):
        with patch.object(core.subprocess, "run", side_effect=FileNotFoundError("adb missing")), \
             patch.object(core, "report_error") as error:
            result = core.run(["devices"])
        self.assertEqual(result, (1, "", "adb-unavailable"))
        self.assertEqual(error.call_args.args[0], "ADB_UNAVAILABLE")

    def test_adb_timeout_does_not_log_command_payload(self):
        failure = subprocess.TimeoutExpired(["shell"], 2)
        with patch.object(core.subprocess, "run", side_effect=failure), \
             patch.object(core, "report_error") as error:
            result = core.run(["shell", "input", "text", "private comment"], serial="phone-a")
        self.assertEqual(result, (1, "", "timeout"))
        self.assertEqual(error.call_args.args[0], "ADB_TIMEOUT")
        self.assertNotIn("private comment", str(error.call_args))


if __name__ == "__main__":
    unittest.main()
