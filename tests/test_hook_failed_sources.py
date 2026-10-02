"""The hook block says when the last pass could not read some sources."""

from __future__ import annotations

import time

from muninn import obs
from muninn.cli_maint import heartbeat
from muninn.ingest import PassStats
from tests.hook_support import HookCase, RecallCase

NOTE = "muninn: the last finished pass could not read"


class FailedSourcesNoteTests(HookCase):
    """A fresh heartbeat, then a last pass with failures in it."""

    def test_a_pass_that_failed_sources_adds_a_note(self) -> None:
        self.assertNotIn(NOTE, self.body(self.start()))
        obs.write_status(self.home, {"failed": 3})
        text = self.body(self.start())
        self.assertIn(f"{NOTE} 3 source(s)", text)
        self.assertIn("run: muninn doctor", text)  # what to do about it
        self.assertNotIn("the index is stale", text)  # the poller is fine

    def test_no_note_when_nothing_failed(self) -> None:
        obs.write_status(self.home, {"failed": 0})
        self.assertNotIn(NOTE, self.body(self.start()))

    def test_a_count_that_is_not_a_positive_integer_adds_no_note(self) -> None:
        for junk in (True, -1, "3", None, 2.5, [1]):
            with self.subTest(failed=junk):
                obs.write_status(self.home, {"failed": junk})
                self.assertNotIn(NOTE, self.body(self.start()))

    def test_a_stale_poller_and_failed_sources_give_both_notes(self) -> None:
        old = {
            "last_pass_at": time.time() - 600,
            "interval_s": 60,
            "failed": 2,
        }
        obs.write_status(self.home, old)
        text = self.body(self.start())
        self.assertIn("the index is stale", text)
        self.assertIn(f"{NOTE} 2 source(s)", text)

    def test_the_note_goes_with_a_later_good_pass(self) -> None:
        heartbeat(self.home, PassStats(failed=2), self.env)
        self.assertIn(f"{NOTE} 2 source(s)", self.body(self.start()))
        heartbeat(self.home, PassStats(failed=0), self.env)
        text = self.body(self.start())
        self.assertNotIn(NOTE, text)
        self.assertNotIn("the index is stale", text)


class FailedSourcesPromptTests(RecallCase):
    """The per-prompt hook adds the note only when it has something to say."""

    def test_the_prompt_hook_notes_failed_sources_only_with_content(
        self,
    ) -> None:
        self.talk("a", "alphaterm betaterm gammaterm deltaterm")
        self.noise(self.repo)
        obs.write_status(self.home, {"failed": 3})
        self.assertIn(f"{NOTE} 3 source(s)", self.body(self.ask()))
        self.assertEqual(self.ask("nothing matches zzterm yyterm xxterm"), {})
