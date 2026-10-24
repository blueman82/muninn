"""Ensure recall switches do not gate lifecycle learning capture."""

from __future__ import annotations

import contextlib
import io
import json
import os
from unittest import mock

from muninn import cli_hook
from tests.ingest_support import primary, rollout
from tests.test_review_requests import RequestCase


class HookLearningRecallModeTests(RequestCase):
    """Check SessionEnd and PreCompact in every prompt recall mode."""

    def setUp(self) -> None:
        super().setUp()
        self.env = dict(os.environ) | {
            "HOME": str(self.tmp / "native-home"),
            "MUNINN_HOME": str(self.home),
        }

    def payload(
        self, event: str, session_id: str, transcript_path: str
    ) -> dict[str, object]:
        """Build a compact Codex lifecycle payload for one indexed source."""
        data: dict[str, object] = {
            "session_id": session_id,
            "cwd": str(self.tmp),
            "transcript_path": transcript_path,
            "hook_event_name": event,
        }
        if event == "SessionEnd":
            data["reason"] = "other"
        else:
            data.update(turn_id="turn", model="explicit", trigger="auto")
        return data

    def dispatch(self, command: str, payload: dict[str, object]) -> None:
        """Run a provider hook and require its compact successful response."""
        output = io.StringIO()
        stream = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            mock.patch.object(cli_hook.sys, "stdin", stream),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(
                cli_hook.hook_main([command, "--provider", "codex"], self.env),
                0,
            )
        self.assertEqual(json.loads(output.getvalue()), {})

    def assert_last_capture_accepted(self) -> None:
        """Require the content-free capture record to report admission."""
        lines = (self.home / "calls.jsonl").read_text().splitlines()
        result = next(
            row
            for row in map(json.loads, reversed(lines))
            if row.get("event") in {"review_enqueued", "review_enqueue_busy"}
        )
        self.assertEqual(result["event"], "review_enqueued")
        self.assertTrue(result["accepted"], result)

    def test_all_recall_modes_allow_session_end_and_precompact_capture(
        self,
    ) -> None:
        switches = (self.home / "recall.off", self.home / "recall.advisory")
        events = (("session-end", "SessionEnd"), ("pre-compact", "PreCompact"))
        modes = (None, "recall.off", "recall.advisory")
        for index, mode in enumerate(modes):
            for switch in switches:
                switch.unlink(missing_ok=True)
            if mode is not None:
                (self.home / mode).touch(mode=0o600)
            for command, event in events:
                session_id = f"recall-mode-{index}-{event}"
                path = self.write(rollout(session_id), primary(session_id))
                self.run_ingest()
                self.dispatch(
                    command,
                    self.payload(event, session_id, str(path)),
                )
                self.assert_last_capture_accepted()
