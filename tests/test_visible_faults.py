"""Doctor, stats and the hook note surface the poller's failures."""

from __future__ import annotations

import time
import unittest
from typing import Any
from unittest import mock

from muninn import cli, ingest_plan, obs
from tests.cli_support import CliCase, fake_run
from tests.hook_support import HookCase
from tests.ingest_support import IngestCase, primary

ERR_NOTE = "muninn: the last pass stopped with"
FILE_NOTE = "could not open 2 transcript file(s)"


class DoctorFaultTests(CliCase):
    """The doctor checks for a failed pass and unreadable files."""

    def checks(self) -> dict[str, dict[str, Any]]:
        """Run doctor with a healthy launchd job; map check name to result."""
        with mock.patch.object(cli.obs, "run", fake_run()):
            _, out, _ = self.muninn("doctor")
        return {c["check"]: c for c in out["checks"]}

    def test_clean_status_passes_both(self) -> None:
        got = self.checks()
        self.assertTrue(got["poller_error"]["ok"])
        self.assertTrue(got["unreadable_files"]["ok"])

    def test_error_class_and_count_are_reported_as_warnings(self) -> None:
        obs.write_status(
            self.home, {"last_error": "OSError", "unreadable_files": 2}
        )
        got = self.checks()
        self.assertEqual(got["poller_error"]["detail"], "OSError")
        self.assertIs(got["poller_error"]["ok"], False)
        self.assertEqual(got["poller_error"]["level"], "warn")
        self.assertEqual(got["unreadable_files"]["detail"], "2")
        self.assertEqual(got["unreadable_files"]["level"], "warn")

    def test_text_in_last_error_is_never_echoed(self) -> None:
        text = "/Users/me/secret path: boom"
        obs.write_status(self.home, {"last_error": text})
        got = self.checks()["poller_error"]
        self.assertTrue(got["ok"])
        self.assertNotIn("secret", got["detail"])
        _, out, _ = self.muninn("stats")
        self.assertIsNone(out["last_pass"]["last_error"])

    def test_stats_shows_the_class_name_and_the_count(self) -> None:
        obs.write_status(
            self.home, {"last_error": "OSError", "unreadable_files": 1}
        )
        _, out, _ = self.muninn("stats")
        self.assertEqual(out["last_pass"]["last_error"], "OSError")
        self.assertEqual(out["last_pass"]["unreadable_files"], 1)


class HookFaultTests(HookCase):
    """The hook note for a failed pass and unreadable files."""

    def test_unreadable_files_add_a_note(self) -> None:
        obs.write_status(self.home, {"unreadable_files": 2})
        self.assertIn(FILE_NOTE, self.body(self.start()))

    def test_no_note_for_zero_or_junk_counts(self) -> None:
        for junk in (0, True, -1, "2", None):
            with self.subTest(n=junk):
                obs.write_status(self.home, {"unreadable_files": junk})
                self.assertNotIn("could not open", self.body(self.start()))

    def test_a_recent_error_adds_a_note_with_the_class_only(self) -> None:
        obs.write_status(
            self.home,
            {"last_error": "OSError", "last_error_at": time.time()},
        )
        text = self.body(self.start())
        self.assertIn(f"{ERR_NOTE} OSError", text)

    def test_an_error_older_than_the_last_pass_adds_no_note(self) -> None:
        now = time.time()
        obs.write_status(
            self.home,
            {
                "last_error": "OSError",
                "last_error_at": now - 100,
                "last_pass_at": now,
            },
        )
        self.assertNotIn(ERR_NOTE, self.body(self.start()))

    def test_a_malformed_error_adds_no_note(self) -> None:
        obs.write_status(self.home, {"last_error": "see /tmp/x: boom"})
        self.assertNotIn(ERR_NOTE, self.body(self.start()))


class UnreadableFileTests(IngestCase):
    """Only a file that cannot be opened counts as unreadable."""

    def plan_raises(self, exc: OSError) -> Any:
        """Run a pass over one transcript whose planning raises ``exc``."""
        self.write("p/a.jsonl", primary(), root="claude-projects")
        with mock.patch.object(ingest_plan, "_plan", side_effect=exc):
            return self.run_ingest()

    def test_a_permission_error_is_counted(self) -> None:
        stats = self.plan_raises(PermissionError("denied"))
        self.assertEqual(stats.unreadable_files, 1)
        self.assertEqual(stats.skipped_files, 1)

    def test_a_vanished_file_is_only_skipped(self) -> None:
        stats = self.plan_raises(FileNotFoundError("gone"))
        self.assertEqual(stats.unreadable_files, 0)
        self.assertEqual(stats.skipped_files, 1)


if __name__ == "__main__":
    unittest.main()
