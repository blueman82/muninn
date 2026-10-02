"""stats and doctor say how far a classifier change's re-read has got."""

from __future__ import annotations

import time
from typing import Any
from unittest import mock

from muninn import cli, obs
from muninn.query.index_age import ALIVE_AT
from tests.cli_support import CliCase, fake_run
from tests.test_ingest import TID

# The classifier version a source carries before a release changes it.
OLDER = 0


class RereadProgressTests(CliCase):
    """One session on disk, read, then stamped as an older release left it."""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.session(TID, "hello there", "hi")
        self.assertEqual(self.muninn("ingest")[0], 0)

    def stamp_sources(self, version: int) -> None:
        """Pretend the sources were last read under ``version``."""
        self.conn.execute(
            "UPDATE source SET classifier_version = ?", (version,)
        )
        self.conn.commit()

    def stats(self) -> dict[str, Any]:
        """Return the parsed ``muninn stats`` answer."""
        code, out, _ = self.muninn("stats")
        self.assertEqual(code, 0, out)
        return out

    def reread_check(self) -> tuple[int, dict[str, Any]]:
        """Run doctor with launchctl faked; return its exit code and check."""
        with mock.patch.object(cli.obs, "run", fake_run()):
            code, out, _ = self.muninn("doctor")
        return code, {c["check"]: c for c in out["checks"]}["reread"]

    def test_nothing_is_pending_when_every_source_is_current(self) -> None:
        self.assertEqual(self.stats()["reread"], {"pending": 0, "of": 1})
        code, check = self.reread_check()
        self.assertEqual(
            (code, check["level"], check["ok"], check["detail"]),
            (0, "info", True, "0 of 1"),
        )

    def test_older_sources_are_pending_and_never_fail_doctor(self) -> None:
        self.stamp_sources(OLDER)
        self.assertEqual(self.stats()["reread"], {"pending": 1, "of": 1})
        code, check = self.reread_check()
        self.assertEqual(
            (code, check["level"], check["ok"], check["detail"]),
            (0, "info", True, "1 of 1"),
        )

    def count_events(self) -> int:
        """Return how many events the store holds."""
        return self.conn.execute("SELECT count(*) FROM event").fetchone()[0]

    def test_a_pass_brings_pending_back_to_zero_without_losing_events(
        self,
    ) -> None:
        events = self.count_events()
        self.stamp_sources(OLDER)
        self.assertEqual(self.stats()["reread"]["pending"], 1)
        self.assertEqual(self.muninn("ingest")[0], 0)
        self.assertEqual(self.stats()["reread"], {"pending": 0, "of": 1})
        self.assertEqual(self.count_events(), events)

    def test_a_source_that_cannot_be_read_stays_pending(self) -> None:
        self.stamp_sources(OLDER)
        rest = self.path.read_text().split("\n", 1)[1]
        self.path.write_text("this is not a thread header\n" + rest)
        self.assertEqual(self.muninn("ingest")[0], 0)
        self.assertEqual(self.stats()["reread"], {"pending": 1, "of": 1})

    def test_missing_sources_stay_out_of_both_counts(self) -> None:
        self.session("thr-two", "second", "one")
        self.assertEqual(self.muninn("ingest")[0], 0)
        self.path.unlink()
        self.assertEqual(self.muninn("ingest")[0], 0)  # marks it missing
        self.stamp_sources(OLDER)
        self.assertEqual(self.stats()["reread"], {"pending": 1, "of": 1})

    def test_sources_whose_files_are_gone_are_not_counted(self) -> None:
        for path in self.roots["codex-sessions"].rglob("*.jsonl"):
            path.unlink()
        self.assertEqual(self.muninn("ingest")[0], 0)
        self.stamp_sources(OLDER)
        self.assertEqual(self.stats()["reread"], {"pending": 0, "of": 0})

    def test_alive_age_shows_only_while_a_pass_is_running(self) -> None:
        self.assertIsNone(self.stats()["last_pass"]["alive_age_s"])
        now = time.time()
        running = {"last_pass_at": now - 100, ALIVE_AT: now - 5}
        obs.write_status(self.home, running)
        age = self.stats()["last_pass"]["alive_age_s"]
        self.assertTrue(5 <= age <= 60, age)
        obs.write_status(self.home, {ALIVE_AT: now + 1e6})
        self.assertIsNone(self.stats()["last_pass"]["alive_age_s"])

    def test_a_stamp_older_than_the_last_pass_is_not_a_running_pass(
        self,
    ) -> None:
        """A stamp left by a poller killed mid-pass is outdated by a pass."""
        now = time.time()
        left_behind = {"last_pass_at": now - 5, ALIVE_AT: now - 100}
        obs.write_status(self.home, left_behind)
        self.assertIsNone(self.stats()["last_pass"]["alive_age_s"])
