"""Tests for tool_error events and their call linking."""

from __future__ import annotations

import json
import unittest

from pctx import classify as c
from tests.classify_claude_support import (
    bash,
    claude_rec,
    run_claude,
    text_block,
    tool_result,
    tool_use,
)
from tests.classify_support import (
    EXEC_ARGS,
    PROC,
    SCRIPT,
    TS,
    codex_meta,
    ctc_output,
    custom_call,
    exec_errors,
    fc_output,
    function_call,
    run_codex,
    terr,
)


class ToolErrorTests(unittest.TestCase):
    """Which Codex outputs become tool_error events, and how they are cut."""

    def test_tool_error_nonzero_exit_emitted_head_tail_2k(self) -> None:
        body = "h" * 3000 + "t" * 3000
        full = PROC.format(2) + "Output:\n" + body
        want = full[:2048] + "\n…\n" + full[-2048:]
        self.assertEqual(exec_errors(full), [terr(3, want, flags=4)])
        short = PROC.format(1) + "Output:\nboom"
        self.assertEqual(exec_errors(short), [terr(3, short)])
        forms = (
            "Exit code: 1\nWall time: 0.1 seconds\nOutput:\nx",
            SCRIPT.format("completed") + "step exit_code=3\n",
            '{"exit_code": 1, "loop_id": "l", "output": "x"}',
            SCRIPT.format("failed") + "boom",
        )
        for text in forms:
            with self.subTest(text=text[:20]):
                got = exec_errors(text, custom=True)
                self.assertEqual(got, [terr(3, text, tag="exec")])
        multibyte = "Exit code: 1\n" + "€" * 3000
        (event,) = exec_errors(multibyte)
        data = multibyte.encode()
        head = data[:2048].decode(errors="ignore")
        tail = data[-2048:].decode(errors="ignore")
        self.assertEqual(event.text, f"{head}\n…\n{tail}")

    def test_successful_tool_output_not_emitted(self) -> None:
        for output in (
            PROC.format(0) + "Output:\nall good",
            SCRIPT.format("completed") + "ok\nexit_code=0",
            '{"message":"Wait completed.","timed_out":false}',
            [{"type": "input_text", "text": "fine"}],
            "",
        ):
            with self.subTest(output=str(output)[:20]):
                self.assertEqual(exec_errors(output), [])

    def test_tool_error_traceback_and_test_summary_patterns(self) -> None:
        positive = (
            "Traceback (most recent call last):\n  File x",
            "FAILED tests/test_x.py::test_a - AssertionError",
            "FAIL: test_x (m.T)",
            "--- FAIL: TestX (0.00s)",
            "not ok 3 - parses input",
            "fatal: not a git repository",
            "x.c:1:10: fatal error: y.h: No such file",
            "error: could not compile `x`",
            "error[E0308]: mismatched types",
            "Error: Cannot find module 'x'",
            "ERROR: test_y (m.T)",
            "npm ERR! code 1",
            "ValueError: bad value",
            "json.decoder.JSONDecodeError: Expecting value",
            "x.py:3: error: Incompatible types",
            "make: *** [all] Error 2",
            "zsh: command not found: foo",
            "thread 'main' panicked at src/main.rs:2:5",
            "===== 1 failed, 2 passed in 0.12s =====",
            "5 passed in 0.12s",
            "1 failed, 1 passed, 2 warnings in 1.00s",
            "Ran 3 tests in 0.001s\n\nOK",
            "Tests:       1 failed, 2 passed, 3 total",
            "      Tests  3 passed (3)",
            "test result: ok. 3 passed; 0 failed; 0 ignored",
            "ok  \texample.com/pkg\t0.123s",
        )
        negative = (
            "all good",
            "see the error handling section",
            "errors: 0",
            "No errors found",
            "    raise ValueError('x')",
            "fatalistic view",
            "Errors are values",
            "okay then",
            "the FAILED count is shown later",
        )
        for text in positive + negative:
            with self.subTest(text=text):
                output = SCRIPT.format("completed") + text
                found = exec_errors(output, custom=True)
                self.assertEqual(len(found), int(text in positive))


class ToolErrorLinkTests(unittest.TestCase):
    """Linking error output to its call across Codex and Claude."""

    def test_tool_error_claude_is_error(self) -> None:
        failed = "Exit code 1\nsomething broke"
        records = [
            bash("make"),
            claude_rec("user", [tool_result("toolu_9", failed, True)]),
        ]
        errors = [e for e in run_claude(records) if e.kind == "tool_error"]
        self.assertEqual(
            errors,
            [
                c.EventRec(
                    2,
                    1,
                    2,
                    TS,
                    "user",
                    "tool_error",
                    "Bash",
                    0,
                    failed,
                    "toolu_9",
                )
            ],
        )
        blocks = [text_block("line one"), text_block("line two")]
        two = claude_rec(
            "assistant",
            [
                tool_use("Read", {"file_path": "/a"}, "t1"),
                tool_use("Read", {"file_path": "/b"}, "t2"),
            ],
        )
        results = claude_rec(
            "user",
            [
                tool_result("t1", blocks, True),
                tool_result("t2", "fine", False),
                tool_result("t1x", "x", True),
            ],
        )
        errors = [
            e for e in run_claude([two, results]) if e.kind == "tool_error"
        ]
        self.assertEqual(
            [(e.part, e.call_id, e.text) for e in errors],
            [(1, "t1", "line one\nline two")],
        )
        ok = claude_rec("user", [tool_result("toolu_9", "done", False)])
        self.assertEqual(run_claude([bash("ls"), ok])[1:], [])

    def _skips_pctx_transcript_reads_and_nested_markers(self) -> None:
        """Pin that errors from reading transcripts or pctx output vanish.

        Re-ingesting such output would feed the store its own text back, so
        the error must not be stored.  The method is bound to its test name
        below because that name does not fit on one line.
        """
        fail = PROC.format(1) + "Output:\nTraceback (most recent call last):"
        calls = (
            '{"cmd":"pctx search foo","workdir":"/w"}',
            '{"cmd":"rg foo ~/.codex/sessions","workdir":"/w"}',
            '{"cmd":"ls /t/corpus/.codex/archived_sessions"}',
            '{"cmd":"grep -r x ~/.claude/projects"}',
        )
        for args in calls:
            with self.subTest(args=args):
                records = [
                    codex_meta(),
                    function_call(1, "exec_command", args, "c1"),
                    fc_output(2, "c1", fail),
                ]
                kinds = [e.kind for e in run_codex(records)[0]]
                self.assertEqual(kinds, ["tool_call"])
        nested = (
            '{"type":"session_meta","payload":{}}',
            '{"timestamp":"t", "type": "response_item", "payload": {}}',
            '{"parentUuid": null, "sessionId": "s"}',
            *c.INJECTED_MARKERS,
            "<pctx-memory x",
            "<pctx-recall",
            c.NOTICE,
        )
        for marker in nested:
            with self.subTest(marker=marker[:20]):
                self.assertEqual(exec_errors(fail + "\n" + marker), [])
        orphan = [codex_meta(), fc_output(1, "nobody", fail)]
        self.assertEqual(run_codex(orphan)[0], [])
        waited = [
            codex_meta(),
            function_call(1, "wait_agent", "{}", "w1"),
            fc_output(2, "w1", fail),
        ]
        self.assertEqual(run_codex(waited)[0], [])
        read = claude_rec(
            "assistant",
            [
                tool_use(
                    "Read",
                    {"file_path": "/h/.claude/projects/p/s.jsonl"},
                    "r1",
                )
            ],
        )
        bad = claude_rec("user", [tool_result("r1", "Error: boom", True)])
        self.assertEqual(run_claude([read, bad])[1:], [])
        no_state = [
            bash("make"),
            claude_rec("user", [tool_result("toolu_9", "Error: boom", True)]),
        ]
        self.assertEqual(run_claude(no_state, state=False)[1:], [])

    test_tool_error_skipped_for_pctx_call_transcript_read_or_nested_markers = (
        _skips_pctx_transcript_reads_and_nested_markers
    )

    def test_long_running_exec_output_inherits_its_call(self) -> None:
        running = SCRIPT.format("running with cell ID 7") + "partial"
        records = [
            codex_meta(),
            custom_call(
                1,
                "exec",
                "await tools.exec_command({cmd: 'make test'})",
                "x1",
            ),
            ctc_output(2, "x1", running),
            function_call(
                3, "wait", '{"cell_id":"7","yield_time_ms":1}', "w1"
            ),
            fc_output(4, "w1", SCRIPT.format("running with cell ID 7")),
            function_call(5, "wait", '{"cell_id":"7"}', "w2"),
            fc_output(6, "w2", SCRIPT.format("failed") + "FAILED t::x"),
        ]
        events, _ = run_codex(records)
        self.assertEqual(
            [(e.kind, e.tag, e.call_id, e.line) for e in events],
            [("tool_call", "exec", "x1", 2), ("tool_error", "exec", "x1", 7)],
        )
        tainted = [dict(r) for r in records]
        tainted[1] = custom_call(
            1,
            "exec",
            "await tools.exec_command("
            "{cmd: 'cat ~/.codex/sessions/r.jsonl'})",
            "x1",
        )
        kinds = [e.kind for e in run_codex(tainted)[0]]
        self.assertEqual(kinds, ["tool_call"])

    def test_tool_call_and_tool_error_share_call_id(self) -> None:
        events, _ = run_codex(
            [
                codex_meta(),
                function_call(1, "exec_command", EXEC_ARGS, "call-7"),
                fc_output(2, "call-7", PROC.format(2) + "Output:\n"),
            ]
        )
        call, error = events
        self.assertEqual((call.kind, error.kind), ("tool_call", "tool_error"))
        self.assertEqual(call.call_id, error.call_id)
        self.assertEqual(call.call_id, "call-7")
        claude = run_claude(
            [
                bash("make", "toolu_5"),
                claude_rec("user", [tool_result("toolu_5", "Error: x", True)]),
            ]
        )
        self.assertEqual([e.call_id for e in claude], ["toolu_5", "toolu_5"])

    def test_pctx_tool_call_flag1(self) -> None:
        flagged = (
            "pctx search foo",
            "cd /w && pctx open codex:t:1.1",
            "PCTX_HOME=/tmp/h pctx know list",
            "/Users/x/.local/bin/pctx --version",
            "./bin/pctx stats",
            "python3.13 -I -B -m pctx search q",
            "echo `pctx search q`",
            "x=$(pctx sessions)",
        )
        clean = (
            "cat pctx/classify.py",
            "pytest -q tests/test_pctx.py",
            "ruff check pctx tests",
            "git commit -m 'pctx: fix x'",
            "ls ~/.local/share/provenance-context/pctx.sqlite",
        )
        for command in flagged + clean:
            with self.subTest(command=command):
                want = 1 if command in flagged else 0
                args = '{"cmd":' + json.dumps(command) + "}"
                codex = run_codex(
                    [codex_meta(), function_call(1, "exec_command", args)]
                )
                self.assertEqual(codex[0][0].flags, want)
                claude = run_claude([bash(command)])
                self.assertEqual(claude[0].flags, want)
