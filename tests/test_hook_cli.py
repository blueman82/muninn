"""Hook command line: bin/muninn hook as a subprocess and in-process main().

Synthetic rows in temp dirs; the providers' payloads are built by hand.
Nothing touches a live data dir or a provider root.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any
from unittest import mock

from muninn import hook, hook_cursor
from tests import test_classify as tc
from tests import test_cli as tcli
from tests.hook_support import (
    HOOKS_JSON,
    USAGE,
    HookCliCase,
    launcher_args,
)
from tools import benchmark_cursor_precompact as benchmark


class HookCommandTests(HookCliCase):
    """The installed hook commands never fail the provider."""

    def test_installed_hook_commands_disabled_print_empty_object(
        self,
    ) -> None:
        commands = [
            h["command"].replace("@HOME@", str(self.tmp / "userhome"))
            for groups in json.loads(HOOKS_JSON.read_text())["hooks"].values()
            for g in groups
            for h in g["hooks"]
        ]
        self.assertEqual(len(commands), 2)
        env = self.env | {"MUNINN_HOOK_DISABLE": "1"}
        env["MUNINN_HOME"] = str(self.tmp / "no-such-dir")
        for event, provider in (
            ("session-start", "claude"),
            ("session-start", "codex"),
            ("session-start", "cursor"),
            ("prompt", "claude"),
            ("prompt", "codex"),
            ("prompt", "cursor"),
            ("pre-compact", "cursor"),
        ):
            with self.subTest(event, provider=provider):
                done = subprocess.run(
                    launcher_args("hook", event, "--provider", provider),
                    input=b"{}",
                    capture_output=True,
                    env=env,
                )
                self.assertEqual(done.returncode, 0)
                self.assertEqual(done.stdout.strip(), b"{}")
                self.assertEqual(done.stderr, b"")
        self.assertFalse((self.tmp / "no-such-dir").exists())
        for command in commands:  # the registered command lines themselves
            argv = shlex.split(command)
            self.assertEqual(argv[1], "hook")
            argv = launcher_args(*argv[1:])
            done = subprocess.run(
                argv, input=b"{}", capture_output=True, env=env
            )
            self.assertEqual(
                (done.returncode, done.stdout.strip()), (0, b"{}")
            )

    def test_precompact_refreshes_cursor_history_without_exposing_text(
        self,
    ) -> None:
        database = self.tmp / "state.vscdb"
        database.touch()
        counts = {
            "conversations_seen": 2,
            "events_added": 3,
            "events_unchanged": 4,
            "skipped_rows": 0,
            "events_skipped_erased": 0,
        }
        run = mock.patch.object(
            hook_cursor.cursor_import, "run", return_value=counts
        )
        path = mock.patch.object(
            hook_cursor.cursor_import,
            "default_database",
            return_value=database,
        )
        trace: dict[str, object] = {}
        project = str(self.tmp / "cursor-project")
        with path, run as importer:
            out = hook_cursor.pre_compact(
                {"cwd": str(self.repo), "message_count": 7},
                "cursor",
                self.env
                | {"HOME": str(self.tmp), "CURSOR_PROJECT_DIR": project},
                trace=trace,
            )
        self.assertEqual(out, {})
        self.assertEqual(trace, {"counts": counts})
        self.assertEqual(importer.call_args.args[2], project)
        self.assertEqual(importer.call_args.kwargs["max_bubbles"], 7)

    def test_precompact_skips_when_native_database_is_unconfigured(
        self,
    ) -> None:
        trace: dict[str, object] = {}
        with (
            mock.patch.object(
                hook_cursor.cursor_import,
                "default_database",
                return_value=None,
            ),
            mock.patch.object(hook_cursor.cursor_import, "run") as importer,
        ):
            out = hook_cursor.pre_compact(
                {"message_count": 2}, "cursor", self.env, trace=trace
            )
        self.assertEqual(out, {})
        self.assertEqual(trace, {"skipped": "missing_cursor_db"})
        importer.assert_not_called()

    def test_precompact_imports_explicit_native_database(self) -> None:
        database = self.tmp / "state.vscdb"
        benchmark._create_database(database, 2, 3, 16)
        trace: dict[str, object] = {}
        env = self.env | {"MUNINN_CURSOR_DB": str(database)}
        with mock.patch.object(
            hook_cursor.cursor_import.sys, "platform", "linux"
        ):
            out = hook_cursor.pre_compact(
                {"cwd": str(self.repo), "message_count": 3},
                "cursor",
                env,
                trace=trace,
            )
        self.assertEqual(out, {})
        counts = trace["counts"]
        assert isinstance(counts, dict)
        self.assertEqual(counts["events_added"], 3)
        self.assertEqual(counts["conversations_seen"], 1)

    def test_precompact_skips_without_a_message_count(self) -> None:
        trace: dict[str, object] = {}
        out = hook_cursor.pre_compact({}, "cursor", self.env, trace=trace)
        self.assertEqual(trace, {"skipped": "message_count_unavailable"})
        self.assertEqual(
            out["user_message"],
            "Muninn skipped the Cursor history refresh because this Cursor "
            "hook payload has no valid message_count.",
        )

    def test_precompact_fails_open_when_cursor_import_fails(self) -> None:
        database = self.tmp / "state.vscdb"
        database.touch()
        path = mock.patch.object(
            hook_cursor.cursor_import,
            "default_database",
            return_value=database,
        )
        run = mock.patch.object(
            hook_cursor.cursor_import, "run", side_effect=RuntimeError
        )
        trace: dict[str, object] = {}
        with path, run:
            out = hook_cursor.pre_compact(
                {"cwd": str(self.repo), "message_count": 7},
                "cursor",
                self.env | {"HOME": str(self.tmp)},
                trace=trace,
            )
        self.assertEqual(trace, {"error": "cursor_import_failed"})
        self.assertIn("user_message", out)
        self.assertNotIn("transcript", json.dumps(out))

    def test_a_malformed_hook_command_never_fails_the_provider(self) -> None:
        for argv in (
            (),
            ("bogus",),
            ("prompt",),
            ("prompt", "--provider"),
            ("prompt", "--provider", "gemini"),
        ):
            with self.subTest(argv):
                done = subprocess.run(
                    launcher_args("hook", *argv),
                    input=b"{}",
                    capture_output=True,
                    env=self.env,
                )
                self.assertEqual(done.returncode, 0)
                self.assertEqual(json.loads(done.stdout), {})

    def test_unknown_extra_flags_are_ignored_not_fatal(self) -> None:
        done = subprocess.run(
            launcher_args("hook", "session-start", "--provider=claude", "--x"),
            input=b"{}",
            capture_output=True,
            env=self.env,
        )
        self.assertEqual(done.returncode, 0)
        self.assertIn("hookSpecificOutput", json.loads(done.stdout))

    def test_bad_stdin_and_missing_store_still_answer_json_exit_0(
        self,
    ) -> None:
        for raw in (b"", b"not json", b"[1]", b"\xff" * 10):
            with self.subTest(raw):
                done = self.run_hook("session-start", "claude", {}, raw=raw)
                out = self.parsed(done)
                self.assertIn("hookSpecificOutput", out)
        gone = {"MUNINN_HOME": str(self.tmp / "gone")}
        done = self.run_hook(
            "prompt",
            "codex",
            self.payload(prompt="alphaterm betaterm gammaterm"),
            env=gone,
        )
        body = self.parsed(done)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("muninn: store unavailable (store_unavailable)", body)
        self.assertFalse(
            (self.tmp / "gone").exists()
        )  # a hook creates nothing

    def test_an_oversize_payload_is_ignored_not_read_on(self) -> None:
        sub = self.tmp / "sub.jsonl"
        sub.write_text(json.dumps(tc.subagent_meta("thr-sub")) + "\n")
        small = {"cwd": str(self.repo), "transcript_path": str(sub)}
        done = self.run_hook("session-start", "codex", small)
        self.assertEqual(self.parsed(done), {})  # a subagent: silent
        big = small | {"pad": "x" * (hook.MAX_INPUT + 10)}
        done = self.run_hook("session-start", "codex", big)
        self.assertIn("hookSpecificOutput", self.parsed(done))  # unread


class HookCliEdgeTests(HookCliCase):
    """Bad arguments, help and crashes through ``cli.main``."""

    def test_a_bad_provider_is_fail_open_for_session_start_too(self) -> None:
        for argv in (
            ("session-start", "--provider", "gemini"),
            ("session-start", "--provider"),
            ("session-start",),
        ):
            with self.subTest(argv):
                done = subprocess.run(
                    launcher_args("hook", *argv),
                    input=b"{}",
                    capture_output=True,
                    env=self.env,
                )
                self.assertEqual(done.returncode, 0)
                self.assertEqual(json.loads(done.stdout), {})

    def test_hook_help_is_help_not_a_silent_hook(self) -> None:
        code, out, err = self.muninn("hook", "--help")
        self.assertEqual((code, out), (2, ""))  # the skeleton help contract
        self.assertIn("session-start", err)

    def main_hook(
        self,
        stdin: bytes | None = b"{}",
        patches: Sequence[contextlib.AbstractContextManager[Any]] = (),
    ) -> tuple[int, str]:
        """Run an in-process session-start call through ``cli.main``.

        Args:
            stdin: Bytes on stdin, or None for a missing ``sys.stdin``.
            patches: Extra patches active for the call.

        Returns:
            The exit code and the stdout text.
        """
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(
                mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin)))
                if stdin is not None
                else mock.patch("sys.stdin", None)
            )
            stack.enter_context(
                mock.patch.dict(os.environ, self.env, clear=True)
            )
            stack.enter_context(mock.patch("sys.stdout", out))
            code = tcli.cli.main(
                ["hook", "session-start", "--provider", "claude"]
            )
        return code, out.getvalue()

    def test_a_crashing_hook_function_still_prints_json_and_exits_0(
        self,
    ) -> None:
        def boom(
            payload: object,
            provider: str,
            env: Mapping[str, str],
            trace: dict[str, object] | None = None,
        ) -> dict[str, object]:
            """Stand in for a hook function that crashes.

            Args:
                payload: Ignored.
                provider: Ignored.
                env: Ignored.
                trace: Ignored.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError("boom")

        patch = mock.patch.dict(tcli.cli.HOOKS, {"session-start": boom})
        code, text = self.main_hook(patches=[patch])
        self.assertEqual((code, json.loads(text)), (0, {}))

    def test_a_failing_stage_log_or_missing_stdin_never_fails_the_hook(
        self,
    ) -> None:
        self.session(tcli.TID, "a prompt", "a reply")
        self.run_ingest()  # the store exists
        log = mock.patch.object(
            tcli.cli.obs, "log_call", side_effect=RuntimeError("log")
        )

        def context(text: str) -> str:
            """Return the ``additionalContext`` of printed hook JSON.

            Args:
                text: Stdout of the hook call.

            Returns:
                The context text.
            """
            return json.loads(text)["hookSpecificOutput"]["additionalContext"]

        code, text = self.main_hook(patches=[log])
        self.assertEqual(code, 0)
        self.assertIn(USAGE, context(text))  # printed before the log ran
        code, text = self.main_hook(stdin=None)  # sys.stdin is None
        self.assertEqual(code, 0)
        self.assertIn(USAGE, context(text))
