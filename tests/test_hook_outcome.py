"""Hook outcome keys in the stage log: misses are counted, text is not."""

from __future__ import annotations

from typing import Any
from unittest import mock

from muninn import hook, hook_recall, obs
from tests.hook_support import RecallCase

MISSES = {"miss_zero", "miss_outside", "miss_floor"}


class HookOutcomeTests(RecallCase):
    """Page 1's matches and in_scope plus one miss key, never a sum."""

    def traced(
        self, prompt: str = "alphaterm betaterm gammaterm"
    ) -> tuple[dict[str, Any], dict[str, object]]:
        """Run the hook and return its trace and answer.

        Args:
            prompt: The prompt to submit.

        Returns:
            The trace dict and the provider JSON.
        """
        trace: dict[str, Any] = {}
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/repo",
            "session_id": "sess-now",
            "prompt": prompt,
        }
        out = hook.prompt_submit(payload, "claude", self.env, trace=trace)
        return trace, out

    def test_zero_matches_is_miss_zero(self) -> None:
        self.noise(self.repo)
        trace, out = self.traced()
        self.assertEqual(out, {})
        stages = trace["stages"]
        self.assertEqual((stages["matches"], stages["in_scope"]), (0, 0))
        self.assertEqual(stages["miss_zero"], 1)
        self.assertFalse({"miss_outside", "miss_floor"} & set(stages))
        self.assertNotIn("query_sha12", trace)
        self.assertNotIn("alphaterm", repr(trace))

    def test_only_outside_scope_is_miss_outside(self) -> None:
        other = self.add_scope("/elsewhere")
        self.talk("far", "alphaterm betaterm gammaterm far", scope_id=other)
        trace, _ = self.traced()
        stages = trace["stages"]
        self.assertEqual(stages["miss_outside"], 1)
        self.assertGreater(stages["matches"], 0)
        self.assertEqual(stages["in_scope"], 0)
        self.assertFalse({"miss_zero", "miss_floor"} & set(stages))

    def test_floor_dropped_everything_is_miss_floor(self) -> None:
        self.talk("two", "only alphaterm betaterm appear in this chat")
        self.noise(self.repo)
        trace, out = self.traced()
        self.assertEqual(out, {})
        stages = trace["stages"]
        self.assertEqual(stages["miss_floor"], 1)
        self.assertGreater(stages["floor_dropped"], 0)
        self.assertFalse({"miss_zero", "miss_outside"} & set(stages))

    def test_a_returned_hit_is_not_a_miss(self) -> None:
        self.talk("a", "alphaterm betaterm gammaterm deltaterm here")
        self.noise(self.repo)
        trace, out = self.traced()
        self.assertIn("hookSpecificOutput", out)
        self.assertFalse(MISSES & set(trace["stages"]))
        self.assertGreater(trace["stages"]["matches"], 0)

    def test_knowledge_only_block_is_not_a_miss(self) -> None:
        self.add(text="alphaterm betaterm gammaterm decision")
        self.noise(self.repo)
        trace, out = self.traced()
        self.assertIn("hookSpecificOutput", out)
        self.assertFalse(MISSES & set(trace["stages"]))

    def test_skipped_prompt_has_no_stages(self) -> None:
        trace, out = self.traced("alphaterm")
        self.assertEqual(out, {})
        self.assertNotIn("stages", trace)

    def test_matches_is_page_one_not_a_sum(self) -> None:
        for i in range(8):
            self.talk(f"t{i}", f"alphaterm betaterm gammaterm n{i}")
        self.noise(self.repo)
        trace, _ = self.traced()
        self.assertEqual(trace["stages"]["matches"], 8)

    def test_error_is_never_a_miss_and_matches_stays_absent(self) -> None:
        self.noise(self.repo)
        with mock.patch.object(
            hook_recall.query, "search", return_value={"error": "bad_arg"}
        ):
            trace, out = self.traced()
        self.assertEqual(out, {})
        self.assertEqual(trace["stages"], {"floor_dropped": 0, "returned": 0})

    def test_error_on_page_two_is_not_a_miss(self) -> None:
        for i in range(8):
            self.talk(f"t{i}", f"alphaterm filler{i} word{i}")
        self.noise(self.repo)
        real = hook_recall._search_page
        pages: list[object] = []

        def flaky(*args: Any) -> Any:
            pages.append(args[-1])
            return real(*args) if len(pages) == 1 else {"error": "bad_arg"}

        with mock.patch.object(hook_recall, "_search_page", flaky):
            trace, out = self.traced()
        self.assertEqual(len(pages), 2)
        self.assertEqual(out, {})
        self.assertEqual(trace["stages"]["matches"], 8)
        self.assertFalse(MISSES & set(trace["stages"]))

    def test_outcome_failure_fails_open(self) -> None:
        self.talk("a", "alphaterm betaterm gammaterm deltaterm here")
        self.noise(self.repo)
        _, expected = self.traced()
        with mock.patch.object(
            hook_recall, "_outcome", side_effect=RuntimeError("boom")
        ):
            trace, out = self.traced()
        self.assertEqual(out, expected)
        self.assertIn("hookSpecificOutput", out)
        self.assertEqual(trace["stages"], {"floor_dropped": 0, "returned": 1})

    def test_stage_line_with_new_keys_stays_under_1kib(self) -> None:
        stages = {
            "matches": 10**9,
            "in_scope": 10**9,
            "floor_dropped": 10**9,
            "returned": 3,
            "miss_zero": 1,
            "miss_outside": 1,
            "miss_floor": 1,
        }
        obs.log_call(
            self.home,
            {
                "cmd": "hook prompt",
                "stages": stages,
                "returned_ids": [1] * 999,
            },
        )
        log = self.home / "calls.jsonl"
        self.assertLessEqual(len(log.read_bytes()), 1024)
