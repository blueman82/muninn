"""Hook outcome keys in the stage log: misses are counted, text is not."""

from __future__ import annotations

import io
import json
import os
import sys
from typing import Any
from unittest import mock

from muninn import cli, hook, hook_recall, obs
from tests import test_classify as tc
from tests.hook_support import RecallCase

MISSES = {"miss_zero", "miss_outside", "miss_floor"}
CANARY = "CANARY-HOOK-" + "q7" * 9


class HookOutcomeTests(RecallCase):
    """Page 1's matches and in_scope plus one miss key, never a sum."""

    provider = "claude"

    def traced(
        self,
        prompt: str = "alphaterm betaterm gammaterm",
        env: dict[str, str] | None = None,
        **kw: object,
    ) -> tuple[dict[str, Any], dict[str, object]]:
        """Run the hook and return its trace and answer.

        Args:
            prompt: The prompt to submit.
            env: Environment entries that override the test environment.
            **kw: Payload fields that override the defaults.

        Returns:
            The trace dict and the provider JSON.
        """
        trace: dict[str, Any] = {}
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/repo",
            "session_id": "sess-now",
            "prompt": prompt,
        } | kw
        out = hook.prompt_submit(
            payload, self.provider, self.env | (env or {}), trace=trace
        )
        return trace, out

    def calls(self, payload: dict[str, object]) -> list[dict[str, Any]]:
        """Run ``muninn hook prompt`` through ``cli.main`` with a payload.

        Args:
            payload: Payload piped to stdin.

        Returns:
            The ``calls.jsonl`` lines, parsed; empty when none was written.
        """
        stdin = mock.Mock(buffer=io.BytesIO(json.dumps(payload).encode()))
        with (
            mock.patch.dict(os.environ, self.env),
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.object(sys, "stdout", io.StringIO()),
        ):
            cli.main(["hook", "prompt", "--provider", self.provider])
        log = self.home / "calls.jsonl"
        return (
            [json.loads(x) for x in log.read_text().splitlines()]
            if (log.exists())
            else []
        )

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
            self.talk(f"t{i}", f"alphaterm filler{i} word{i}")
        self.noise(self.repo)
        real = hook_recall._search_page
        pages: list[object] = []

        def spy(*args: Any) -> Any:
            pages.append(args[-1])
            return real(*args)

        with mock.patch.object(hook_recall, "_search_page", spy):
            trace, _ = self.traced()
        self.assertGreaterEqual(len(pages), 2)
        self.assertEqual(trace["stages"]["matches"], 8)
        self.assertEqual(trace["stages"]["floor_dropped"], 8)

    def test_error_is_never_a_miss_and_matches_stays_absent(self) -> None:
        self.noise(self.repo)
        with mock.patch.object(
            hook_recall.query, "search", return_value={"error": "bad_arg"}
        ):
            trace, out = self.traced()
        self.assertEqual(out, {})
        stages = trace["stages"]
        self.assertEqual(stages["pool_error"], 1)
        self.assertNotIn("matches", stages)
        self.assertFalse(MISSES & set(stages))

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
        self.assertEqual(trace["stages"]["pool_error"], 1)
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
        self.assertEqual(trace["stages"]["returned"], 1)
        self.assertFalse(MISSES & set(trace["stages"]))

    def test_knowledge_with_events_only_out_of_scope_is_not_a_miss(
        self,
    ) -> None:
        self.add(text="alphaterm betaterm gammaterm decision")
        other = self.add_scope("/elsewhere")
        self.talk("far", "alphaterm betaterm gammaterm far", scope_id=other)
        trace, out = self.traced()
        self.assertIn("hookSpecificOutput", out)
        self.assertFalse(MISSES & set(trace["stages"]))

    def test_skip_paths_log_no_miss(self) -> None:
        self.noise(self.repo)
        sub = self.tmp / "sub.jsonl"
        first = (
            tc.subagent_meta("thr-s")
            if self.provider == "codex"
            else {"isSidechain": True, "sessionId": "sess-sub"}
        )
        sub.write_text(json.dumps(first) + "\n")
        (self.home / "recall.off").write_text("")
        trace, _ = self.traced()
        self.assertEqual(trace["skipped"], "recall_off")
        (self.home / "recall.off").unlink()
        trace, _ = self.traced(transcript_path=str(sub))
        self.assertEqual(trace["skipped"], "subagent")
        for found in (trace, self.traced(env={"MUNINN_HOOK_DISABLE": "1"})[0]):
            self.assertNotIn("stages", found)

    def test_disabled_hook_writes_no_line_and_skip_logs_no_miss(self) -> None:
        self.noise(self.repo)
        payload: dict[str, object] = {
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/repo",
            "prompt": "alphaterm betaterm gammaterm",
        }
        self.env["MUNINN_HOOK_DISABLE"] = "1"
        self.assertEqual(self.calls(payload), [])  # disabled: no line
        del self.env["MUNINN_HOOK_DISABLE"]
        (self.home / "recall.off").write_text("")
        (call,) = self.calls(payload)  # a skipped call still logs once
        self.assertFalse(MISSES & set(call.get("stages", {})))

    def test_end_to_end_line_has_miss_key_and_no_text(self) -> None:
        self.noise(self.repo)
        call = self.calls(
            {
                "hook_event_name": "UserPromptSubmit",
                "cwd": "/repo",
                "session_id": "sess-now",
                "prompt": f"{CANARY} betaterm gammaterm",
            }
        )[-1]
        self.assertEqual(call["cmd"], "hook prompt")
        self.assertEqual(call["stages"]["miss_zero"], 1)
        raw = (self.home / "calls.jsonl").read_text()
        self.assertNotIn(CANARY, raw)
        self.assertNotIn("query_sha12", raw)

    def test_stage_line_with_new_keys_stays_under_1kib(self) -> None:
        stages = {
            "matches": 10**9,
            "in_scope": 10**9,
            "floor_dropped": 10**9,
            "returned": 3,
            "pool_error": 1,
            "miss_zero": 1,
            "miss_outside": 1,
            "miss_floor": 1,
        }
        log = self.home / "calls.jsonl"
        for key in ("returned_ids", "knowledge_ids"):
            log.unlink(missing_ok=True)
            obs.log_call(
                self.home,
                {"cmd": "search", "stages": stages, key: [1] * 999},
            )
            raw = log.read_bytes()
            self.assertLessEqual(len(raw), 1024)
            self.assertTrue(raw.endswith(b"\n"))
            self.assertEqual(len(raw.splitlines()), 1)
            self.assertEqual(json.loads(raw)["stages"], stages)
        log.unlink()
        obs.log_call(
            self.home,
            {
                "cmd": "search",
                "stages": stages,
                "knowledge_ids": list(range(500)),
                "returned_ids": list(range(500)),
            },
        )
        raw = log.read_bytes()
        self.assertLessEqual(len(raw), 1024)
        self.assertEqual(len(raw.splitlines()), 1)
        self.assertEqual(json.loads(raw)["stages"], stages)


class CodexOutcomeTests(HookOutcomeTests):
    """The same outcomes for the Codex provider."""

    provider = "codex"
