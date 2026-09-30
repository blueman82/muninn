"""Tests for the trial_tool MCP wrapper and the isolated launcher."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from trial_harness import canaries, launch, mcp_reader

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "trial_harness" / "mcp_reader.py"
PROTOCOL = Path(launch.load_config()["trial_dir"]) / "trial-protocol.md"
REPO_CLI = Path(
    "/Users/garyharr/Github/provenance-context-build/"
    "v7-evidence/round4/reader_trial.py"
)
FROZEN_CLI_SHA256 = (
    "b4b1491954d23afdfa23e2d7f2a172ec2756e8448d187121a1b19e6b73b7762c"
)

# A stand-in for reader_trial.py with the same CLI and case() interface.
STUB_CLI = textwrap.dedent("""
    import json, sys, time
    from pathlib import Path
    FIELDS = ("provider", "source_path", "source_line", "source_ordinal",
              "source_hash")
    TRACE = Path(__file__).with_name("reader_trace.jsonl")
    REFS = [
        {"provider": "claude", "source_path": "a.jsonl", "source_line": 1,
         "source_ordinal": 1, "source_hash": "h1"},
        {"provider": "claude", "source_path": "a.jsonl", "source_line": 2,
         "source_ordinal": 1, "source_hash": "h2"},
        {"provider": "codex", "source_path": "b.jsonl", "source_line": 7,
         "source_ordinal": 1, "source_hash": "h3"},
    ]
    def case(label_id):
        return {"id": label_id, "question": "q", "scope": "/s"}, REFS
    def main(args):
        command, label = args[:2]
        if command == "files":
            result = {"notice": "n", "next_cursor": None, "items": [
                {"file_id": 0, "messages": 2, "hint": "alpha"},
                {"file_id": 1, "messages": 1, "hint": "gamma é"}]}
        elif command == "events":
            result = {"notice": "n", "next_cursor": None, "items": [
                {"event_id": 0, "role": "user", "hint": "alpha"},
                {"event_id": 1, "role": "assistant", "hint": "beta"}]}
        elif command == "open":
            if args[2] == "99":
                time.sleep(30)
            if args[2] == "bad":
                raise ValueError("invalid literal")
            ref = REFS[int(args[2])]
            result = {"notice": "n", "source": ref, "role": "user",
                      "text": "hello", "next_offset": None}
        else:
            raise ValueError("unexpected command")
        encoded = json.dumps(result, ensure_ascii=False,
                             separators=(",", ":"))
        with TRACE.open("a") as trace:
            trace.write(json.dumps({"question": label, "command": command,
                                    "args": args[2:], "bytes": 0}) + "\\n")
        print(encoded)
        return 0
    if __name__ == "__main__":
        try:
            raise SystemExit(main(sys.argv[1:]))
        except (ValueError, IndexError) as error:
            print(json.dumps({"error": str(error)}))
            raise SystemExit(2)
    """)

# A stand-in NEW adapter that reports what it was given.
STUB_ADAPTER = textwrap.dedent("""
    import json, os, sys
    print(json.dumps({"argv": sys.argv[1:], "env": sorted(os.environ),
                      "items": [{"provider": "claude",
                                 "source_path": "x.jsonl", "line": 3,
                                 "record_sha256": "r3", "text": "abc"}]}))
    """)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class WrapperFixture(unittest.TestCase):
    """Builds a binding for a stub OLD CLI in a temporary directory."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cli = self.tmp / "reader_trial.py"
        self.cli.write_text(STUB_CLI)
        self.token = "ab" * 16
        self.log = self.tmp / "wrapper-log.jsonl"
        self.binding = {
            "unit_id": "OLD:T1",
            "arm": "OLD",
            "qid": "T1",
            "scope": "/s",
            "token_sha256": sha(self.token),
            "agent_id": "agent-1",
            "log_path": str(self.log),
            "t_launch": time.time(),
            "max_calls": 300,
            "max_seconds": 3600,
            "call_timeout_s": 5,
            "exec_cwd": str(self.tmp),
            "old": {
                "python": sys.executable,
                "cli": str(self.cli),
                "env": {"PATH": "/usr/bin:/bin", "PYTHONUTF8": "1"},
            },
        }

    def wrapper(self, **overrides: object) -> mcp_reader.Wrapper:
        return mcp_reader.Wrapper(self.binding | overrides)

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class OldWrapperTest(WrapperFixture):
    def test_files_passes_stdout_byte_for_byte_and_logs_ids(self) -> None:
        text, is_error = self.wrapper().call(
            {"token": self.token, "args": ["files"]}
        )
        expected = subprocess.run(
            [sys.executable, str(self.cli), "files", "T1"],
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONUTF8": "1"},
        ).stdout.decode()
        self.assertEqual(text, expected)
        self.assertFalse(is_error)
        (record,) = self.records()
        self.assertEqual(record["event"], "OK")
        self.assertEqual(record["argv"], ["files"])
        self.assertEqual(
            record["exec_argv"][1:], [str(self.cli), "files", "T1"]
        )
        self.assertEqual(record["bytes_out"], len(text.encode()))
        self.assertEqual(record["reader_token_sha256"], sha(self.token))
        self.assertEqual(record["agent_id"], "agent-1")
        # file 0 surfaces refs[group0[0]] = ref 0; file 1 surfaces ref 2.
        ids = [item["identity"]["source_hash"] for item in record["ids"]]
        self.assertEqual(ids, ["h1", "h3"])
        self.assertEqual(record["ids"][1]["chars"], len("gamma é"))
        self.assertEqual(
            {item["class"] for item in record["ids"]}, {"listing"}
        )

    def test_events_and_open_ids(self) -> None:
        wrapper = self.wrapper()
        wrapper.call({"token": self.token, "args": ["events", "0"]})
        wrapper.call({"token": self.token, "args": ["open", "2"]})
        events, opened = self.records()
        self.assertEqual(
            [item["identity"]["source_hash"] for item in events["ids"]],
            ["h1", "h2"],
        )
        self.assertEqual(opened["ids"][0]["class"], "open")
        self.assertEqual(opened["ids"][0]["identity"]["source_hash"], "h3")
        self.assertEqual(opened["ids"][0]["chars"], len("hello"))

    def test_question_command_is_refused_without_running_cli(self) -> None:
        text, is_error = self.wrapper().call(
            {"token": self.token, "args": ["question"]}
        )
        self.assertTrue(is_error)
        self.assertIn("error", json.loads(text))
        (record,) = self.records()
        self.assertEqual(record["event"], "REFUSED")
        self.assertIsNone(record["exec_argv"])
        self.assertFalse((self.tmp / "reader_trace.jsonl").exists())

    def test_unknown_token_is_rejected_and_logged(self) -> None:
        text, is_error = self.wrapper().call(
            {"token": "cd" * 16, "args": ["files"]}
        )
        self.assertTrue(is_error)
        self.assertEqual(json.loads(text), {"error": "unknown token"})
        (record,) = self.records()
        self.assertEqual(record["event"], "UNKNOWN_TOKEN")
        self.assertEqual(record["reader_token_sha256"], sha("cd" * 16))
        self.assertIsNone(record["exec_argv"])

    def test_malformed_arguments_are_rejected(self) -> None:
        _, is_error = self.wrapper().call({"token": self.token, "args": [1]})
        self.assertTrue(is_error)
        self.assertEqual(self.records()[0]["event"], "BAD_ARGS")

    def test_cli_error_json_passes_through_with_exit_code(self) -> None:
        text, is_error = self.wrapper().call(
            {"token": self.token, "args": ["open", "bad"]}
        )
        self.assertTrue(is_error)
        self.assertEqual(json.loads(text), {"error": "invalid literal"})
        record = self.records()[0]
        self.assertEqual(record["exit_code"], 2)
        self.assertEqual(record["ids"], [])

    def test_call_limit_ceiling(self) -> None:
        wrapper = self.wrapper(max_calls=2)
        for _ in range(2):
            wrapper.call({"token": self.token, "args": ["files"]})
        text, _ = wrapper.call({"token": self.token, "args": ["files"]})
        self.assertEqual(text, mcp_reader.CEILING)
        self.assertEqual(
            [record["event"] for record in self.records()],
            ["OK", "OK", "CEILING_HIT"],
        )

    def test_time_ceiling(self) -> None:
        wrapper = self.wrapper(t_launch=time.time() - 3601)
        text, _ = wrapper.call({"token": self.token, "args": ["files"]})
        self.assertEqual(text, mcp_reader.CEILING)
        self.assertEqual(self.records()[0]["event"], "CEILING_HIT")

    def test_per_call_timeout(self) -> None:
        wrapper = self.wrapper(call_timeout_s=1)
        text, is_error = wrapper.call(
            {"token": self.token, "args": ["open", "99"]}
        )
        self.assertTrue(is_error)
        self.assertEqual(self.records()[0]["event"], "TIMEOUT")
        self.assertIn("error", json.loads(text))

    def test_hash_chain_verifies_and_survives_restart(self) -> None:
        self.wrapper().call({"token": self.token, "args": ["files"]})
        self.wrapper().call({"token": self.token, "args": ["events", "1"]})
        self.assertEqual(mcp_reader.verify_chain(self.log), 2)
        records = self.records()
        self.assertEqual([record["seq"] for record in records], [1, 2])
        self.assertEqual(records[0]["prev_sha256"], mcp_reader.GENESIS)
        lines = self.log.read_text().splitlines()
        lines[0] = lines[0].replace('"OK"', '"XX"')
        self.log.write_text("\n".join(lines) + "\n")
        with self.assertRaises(ValueError):
            mcp_reader.verify_chain(self.log)

    def test_native_trace_count_matches_successful_calls(self) -> None:
        wrapper = self.wrapper()
        for args in (["files"], ["question"], ["open", "bad"], ["open", "1"]):
            wrapper.call({"token": self.token, "args": args})
        trace = self.tmp / "reader_trace.jsonl"
        ok = sum(record["exit_code"] == 0 for record in self.records())
        self.assertEqual(mcp_reader.native_trace_count(trace, "T1", 0), ok)
        self.assertEqual(ok, 2)


class NewWrapperTest(WrapperFixture):
    def setUp(self) -> None:
        super().setUp()
        adapter = self.tmp / "adapter.py"
        adapter.write_text(STUB_ADAPTER)
        home = self.tmp / "new-home"
        home.mkdir()
        self.binding |= {
            "unit_id": "NEW:T1",
            "arm": "NEW",
            "new": {
                "argv": [sys.executable, str(adapter)],
                "env": {
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(home),
                    "TRIAL_INDEX": str(self.tmp / "index"),
                },
                "command_classes": {"search": "listing"},
            },
        }
        del self.binding["old"]

    def test_adapter_gets_scope_and_only_three_env_vars(self) -> None:
        text, is_error = self.wrapper().call(
            {"token": self.token, "args": ["search", "x"]}
        )
        self.assertFalse(is_error)
        data = json.loads(text)
        self.assertEqual(data["argv"], ["--scope", "/s", "search", "x"])
        # The child interpreter itself adds these two (macOS CoreFoundation
        # and PEP 538 locale coercion); the wrapper passes exactly three.
        added = {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}
        self.assertEqual(
            set(data["env"]) - added, {"PATH", "HOME", "TRIAL_INDEX"}
        )
        (record,) = self.records()
        self.assertEqual(record["ids"][0]["class"], "listing")
        self.assertEqual(record["ids"][0]["identity"]["line"], 3)
        self.assertEqual(record["ids"][0]["chars"], 3)

    def test_exact_env_is_passed_and_bad_output_does_not_crash(self) -> None:
        seen = {}
        real_run = subprocess.run

        def fake_run(argv, **kwargs):
            seen.update(kwargs["env"])
            return real_run(["/bin/echo", "not json"], capture_output=True)

        with patch.object(mcp_reader.subprocess, "run", fake_run):
            text, is_error = self.wrapper().call(
                {"token": self.token, "args": ["search", "x"]}
            )
        self.assertEqual(seen, self.binding["new"]["env"])
        self.assertEqual(text, "not json\n")
        self.assertFalse(is_error)
        (record,) = self.records()
        self.assertIn("ids_error", record)

    def test_reader_cannot_override_scope(self) -> None:
        for args in (["search", "--scope", "/x"], ["search", "--scope=/x"]):
            _, is_error = self.wrapper().call(
                {"token": self.token, "args": args}
            )
            self.assertTrue(is_error)
        self.assertEqual(
            {record["event"] for record in self.records()}, {"REFUSED"}
        )

    def test_extra_environment_is_rejected_at_startup(self) -> None:
        self.binding["new"]["env"]["CLAUDE_CODE_SESSION_ID"] = "x"
        with self.assertRaises(ValueError):
            self.wrapper()


class McpProtocolTest(WrapperFixture):
    """Speaks JSON-RPC to the real server process over stdio."""

    def rpc(self, messages: list[dict]) -> list[dict]:
        binding = self.tmp / "binding.json"
        binding.write_text(json.dumps(self.binding))
        payload = "".join(json.dumps(message) + "\n" for message in messages)
        done = subprocess.run(
            [sys.executable, "-I", "-B", str(SERVER), str(binding)],
            input=payload.encode(),
            capture_output=True,
            timeout=30,
        )
        return [json.loads(line) for line in done.stdout.splitlines()]

    def test_initialize_list_and_call(self) -> None:
        replies = self.rpc(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "trial_tool",
                        "arguments": {"token": self.token, "args": ["files"]},
                    },
                },
                {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
            ]
        )
        self.assertEqual([reply["id"] for reply in replies], [0, 1, 2, 3])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        (tool,) = replies[1]["result"]["tools"]
        self.assertEqual(tool["name"], "trial_tool")
        self.assertEqual(tool["inputSchema"]["required"], ["token", "args"])
        content = replies[2]["result"]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertIn('"file_id":0', content[0]["text"])
        self.assertFalse(replies[2]["result"]["isError"])
        self.assertEqual(replies[3]["error"]["code"], -32601)
        self.assertEqual(mcp_reader.verify_chain(self.log), 1)

    def test_invalid_binding_exits_nonzero(self) -> None:
        del self.binding["token_sha256"]
        binding = self.tmp / "binding.json"
        binding.write_text(json.dumps(self.binding))
        done = subprocess.run(
            [sys.executable, "-I", "-B", str(SERVER), str(binding)],
            input=b"",
            capture_output=True,
            timeout=30,
        )
        self.assertNotEqual(done.returncode, 0)


@unittest.skipUnless(REPO_CLI.is_file(), "frozen OLD CLI not present")
class RealOldCliTest(unittest.TestCase):
    """The wrapper against a byte-identical copy of the frozen CLI."""

    def test_wrapper_ids_match_cli_outputs(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        arm = canaries.build_synthetic_old_arm(tmp / "old-arm", REPO_CLI)
        cli = Path(arm["cli"])
        self.assertEqual(
            hashlib.sha256(cli.read_bytes()).hexdigest(), FROZEN_CLI_SHA256
        )
        token = "ef" * 16
        log = tmp / "log.jsonl"
        wrapper = mcp_reader.Wrapper(
            {
                "unit_id": "OLD:CANARY",
                "arm": "OLD",
                "qid": arm["qid"],
                "scope": arm["scope"],
                "token_sha256": sha(token),
                "agent_id": "a",
                "log_path": str(log),
                "t_launch": time.time(),
                "max_calls": 300,
                "max_seconds": 3600,
                "call_timeout_s": 30,
                "exec_cwd": str(tmp),
                "old": {
                    "python": sys.executable,
                    "cli": str(cli),
                    "env": {"PATH": "/usr/bin:/bin", "PYTHONUTF8": "1"},
                },
            }
        )
        files, _ = wrapper.call({"token": token, "args": ["files"]})
        events, _ = wrapper.call({"token": token, "args": ["events", "1"]})
        opened, _ = wrapper.call({"token": token, "args": ["open", "3"]})
        wrapper.call({"token": token, "args": ["question"]})
        records = [json.loads(line) for line in log.read_text().splitlines()]
        pool = arm["refs"]
        self.assertEqual(
            [item["identity"] for item in records[0]["ids"]],
            [pool[0], pool[2]],
        )
        self.assertEqual(
            [item["identity"] for item in records[1]["ids"]],
            [pool[2], pool[3]],
        )
        self.assertEqual(records[2]["ids"][0]["identity"], pool[3])
        self.assertEqual(json.loads(opened)["source"], pool[3])
        self.assertEqual(json.loads(files)["items"][0]["file_id"], 0)
        self.assertEqual(len(json.loads(events)["items"]), 2)
        trace = cli.with_name("reader_trace.jsonl")
        self.assertEqual(
            mcp_reader.native_trace_count(trace, arm["qid"], 0), 3
        )
        self.assertFalse(any(cli.parent.glob("__pycache__")))


class PromptTest(unittest.TestCase):
    def test_placeholders_are_replaced_exactly(self) -> None:
        cfg = launch.load_config()
        prompt = launch.reader_prompt(
            cfg, "TOOL-DESC\nline two", "What {happened}?", "f" * 32
        )
        self.assertIn("TOOL\nTOOL-DESC\nline two\nPass your session", prompt)
        self.assertIn("QUESTION\nWhat {happened}?\n", prompt)
        self.assertIn("token " + "f" * 32 + " as the first", prompt)
        self.assertNotIn("{READER_TOKEN}", prompt)
        self.assertTrue(prompt.startswith("You are answering one question"))

    @unittest.skipUnless(PROTOCOL.is_file(), "protocol not present")
    def test_frozen_texts_equal_protocol_blocks(self) -> None:
        cfg = launch.load_config()
        blocks = launch.protocol_code_blocks(PROTOCOL.read_text())
        self.assertEqual(cfg["reader_prompt_template"], blocks[0])
        self.assertEqual(cfg["old_tool_description"], blocks[1])
        self.assertEqual(cfg["grader_preamble"], blocks[2])


class ArgvEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = launch.load_config()

    def test_isolated_reader_command(self) -> None:
        argv = launch.claude_argv(
            self.cfg,
            "isolated",
            role="reader",
            model="opus",
            effort="high",
            session_id="00000000-0000-4000-8000-000000000000",
            mcp_config="/tmp/mcp.json",
        )
        self.assertEqual(argv[0], self.cfg["claude_bin"])
        for flag in (
            "--restricted",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--include-hook-events",
        ):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--mcp-config") + 1], "/tmp/mcp.json")
        self.assertEqual(
            argv[argv.index("--allowedTools") + 1], "mcp__trial__trial_tool"
        )
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertIs(settings["disableAllHooks"], True)
        self.assertIs(settings["autoMemoryEnabled"], False)

    def test_isolated_grader_has_no_tools_or_mcp(self) -> None:
        argv = launch.claude_argv(
            self.cfg,
            "isolated",
            role="grader",
            model="sonnet",
            effort="high",
            session_id="00000000-0000-4000-8000-000000000000",
            mcp_config=None,
        )
        self.assertNotIn("--mcp-config", argv)
        self.assertNotIn("--allowedTools", argv)
        self.assertIn("--strict-mcp-config", argv)

    def test_isolated_env_is_an_allowlist(self) -> None:
        base = {
            "HOME": "/h",
            "USER": "u",
            "LOGNAME": "u",
            "TMPDIR": "/t",
            "CLAUDE_CODE_SESSION_ID": "leak",
            "CLAUDE_CODE_MESSAGING_SOCKET": "leak",
            "CODEX_THREAD_ID": "leak",
            "ANTHROPIC_API_KEY": "leak",
        }
        env = launch.child_env(self.cfg, "isolated", base)
        self.assertEqual(env["HOME"], "/h")
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertNotIn("CLAUDE_CODE_MESSAGING_SOCKET", env)
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"], "1")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")

    def test_default_profile_also_drops_parent_session_vars(self) -> None:
        env = launch.child_env(
            self.cfg, "default", {"HOME": "/h", "CLAUDE_CODE_SESSION_ID": "x"}
        )
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)

    def test_mcp_config_runs_server_isolated(self) -> None:
        config = launch.mcp_config(self.cfg, Path("/tmp/b.json"))
        server = config["mcpServers"]["trial"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["args"][:2], ["-I", "-B"])
        self.assertTrue(server["args"][2].endswith("mcp_reader.py"))
        self.assertEqual(server["args"][3], "/tmp/b.json")
        self.assertIs(server["alwaysLoad"], True)


class ExtractTest(unittest.TestCase):
    def test_last_valid_top_level_object_wins(self) -> None:
        text = (
            'draft {"answer": "a", "citations": [], "abstained": false}\n'
            'final {"answer": "b", "citations": [{"x": 1}], '
            '"abstained": true} trailing words'
        )
        self.assertEqual(
            launch.extract_final_answer(text),
            {"answer": "b", "citations": [{"x": 1}], "abstained": True},
        )

    def test_extra_or_mistyped_keys_are_not_answers(self) -> None:
        for text in (
            '{"answer": "a", "citations": [], "abstained": false, "x": 1}',
            '{"answer": "a", "citations": {}, "abstained": false}',
            '{"answer": "a", "citations": [], "abstained": "no"}',
            "no json here",
        ):
            self.assertIsNone(launch.extract_final_answer(text))

    def test_nested_answer_shaped_object_is_not_top_level(self) -> None:
        text = (
            '{"wrapper": {"answer": "a", "citations": [], '
            '"abstained": false}}'
        )
        self.assertIsNone(launch.extract_final_answer(text))

    def test_final_message_prefers_result_event(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        stream = tmp / "stream.jsonl"
        stream.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "x"}]},
                }
            )
            + "\n"
            + json.dumps({"type": "result", "result": "final text"})
            + "\n"
        )
        self.assertEqual(launch.final_message(stream), "final text")


class CloseUnitTest(unittest.TestCase):
    def test_close_unit_appends_hashes(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        unit = tmp / "unit"
        unit.mkdir()
        (unit / "transcript.jsonl").write_text("{}\n")
        (unit / "stream.jsonl").write_text(
            json.dumps(
                {
                    "type": "result",
                    "result": '{"answer": "a", "citations": [], '
                    '"abstained": true}',
                }
            )
            + "\n"
        )
        index = tmp / "index.jsonl"
        entry = launch.close_unit(unit, index, "OLD:X")
        self.assertEqual(json.loads(index.read_text()), entry)
        self.assertEqual(
            entry["transcript_sha256"],
            hashlib.sha256(b"{}\n").hexdigest(),
        )
        self.assertEqual(entry["status"], "ANSWERED")
        answer = json.loads((unit / "answer.json").read_text())
        self.assertIs(answer["abstained"], True)
        self.assertEqual(os.stat(unit / "answer.json").st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
