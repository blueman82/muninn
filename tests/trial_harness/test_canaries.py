"""Tests for the transcript scanner and the K1-K5 canary evaluations."""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from trial_harness import canaries

TOOL = canaries.TRIAL_TOOL
SENTINEL = "sentinel-address-7f3a@example.invalid"  # synthetic, never real
EMAIL_KEY = canaries.EMAIL_KEY


def attachment(kind: str, **fields: object) -> dict:
    return {"type": "attachment", "attachment": {"type": kind} | fields}


def assistant_tool(name: str, tool_id: str, **inputs: object) -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": "claude-haiku-test",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": inputs,
                }
            ],
        },
    }


def tool_result(tool_id: str, text: str, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": text,
                    "is_error": is_error,
                }
            ]
        },
    }


def clean_records() -> list[dict]:
    return [
        {"type": "user", "message": {"content": "You are answering one q"}},
        attachment("date", date="2026-09-30"),
        attachment("model", identity={"modelId": "m"}),
        attachment("environment", snapshot={"workingDirectory": "/sb"}),
        attachment("prompt_snapshot", systemPrompt=["You are Claude."]),
        assistant_tool(TOOL, "t1", token="x", args=["files"]),
        tool_result("t1", '{"items": []}'),
    ]


def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def classes(result: dict) -> set[str]:
    return {finding["class"] for finding in result["findings"]}


def context_record(**context: object) -> dict:
    """A synthetic session_context record shaped like Claude Code's own.

    The attachment body holds the context; the model-visible rendering
    carries one ``# key`` heading per entry.
    """
    body = "".join(f"# {key}\n{value}\n" for key, value in context.items())
    record = attachment("session_context", context=context)
    record["rendered"] = [
        {"content": f"<system-reminder>\n{body}</system-reminder>"}
    ]
    record["renderedRole"] = "user"
    return record


def email_context(**extra: object) -> dict:
    """The waivable shape when ``extra`` is empty: userEmail alone."""
    return context_record(userEmail=f"Synthetic account {SENTINEL}.", **extra)


def write_waiver(directory: Path, text: str = "synthetic waiver\n") -> Path:
    """A waiver file and its `shasum`-style WAIVER.sha256 record."""
    path = Path(directory) / "WAIVER.md"
    path.write_text(text)
    digest = hashlib.sha256(text.encode()).hexdigest()
    (Path(directory) / "WAIVER.sha256").write_text(f"{digest}  {path}\n")
    return path


class ScannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def scan(self, records: list[dict], **kwargs: object) -> dict:
        path = write_jsonl(self.tmp / "t.jsonl", records)
        return canaries.scan(path, **kwargs)

    def test_clean_reader_transcript_has_no_findings(self) -> None:
        result = self.scan(clean_records())
        self.assertEqual(result["findings"], [])
        self.assertTrue(canaries.verdict(result)["pass"])
        self.assertEqual(result["tool_calls"], [{"name": TOOL, "ok": True}])
        self.assertEqual(result["models"], ["claude-haiku-test"])
        self.assertEqual(
            sorted(result["attachment_sha256"]),
            ["date", "environment", "model", "prompt_snapshot"],
        )

    def test_other_tool_call_is_fatal_only_when_successful(self) -> None:
        records = clean_records() + [
            assistant_tool("Bash", "t2", command="ls"),
            tool_result("t2", "denied", is_error=True),
        ]
        (finding,) = self.scan(records)["findings"]
        self.assertEqual(finding["class"], "other_tool_call")
        self.assertFalse(finding["fatal"])
        records[-1] = tool_result("t2", "file-list")
        (finding,) = self.scan(records)["findings"]
        self.assertTrue(finding["fatal"])

    def test_forbidden_strings_anywhere_and_in_results_only(self) -> None:
        records = clean_records()
        records[0]["message"]["content"] += " phrase-P"
        result = self.scan(records, forbidden_in_results=["phrase-P"])
        self.assertEqual(result["findings"], [])
        result = self.scan(records, forbidden=["phrase-P"])
        self.assertEqual(classes(result), {"canary_string"})
        records.append(tool_result("t1", "echo phrase-P"))
        result = self.scan(records, forbidden_in_results=["phrase-P"])
        self.assertEqual(classes(result), {"forbidden_in_result"})

    def test_other_trial_question_is_fatal_but_own_question_is_not(
        self,
    ) -> None:
        records = clean_records()
        records[0]["message"]["content"] = "QUESTION\nWhat   did we\nship?"
        questions = ["What did we ship?", "Which bug was fixed?"]
        result = self.scan(
            records, questions=questions, own_question="What did we ship?"
        )
        self.assertEqual(result["findings"], [])
        result = self.scan(
            records, questions=questions, own_question="Which bug was fixed?"
        )
        self.assertEqual(classes(result), {"other_question"})

    def test_non_allowlisted_attachment_is_flagged(self) -> None:
        records = clean_records() + [attachment("agent_listing_delta")]
        self.assertEqual(
            classes(self.scan(records)), {"non_allowlisted_attachment"}
        )

    def test_deferred_tools_listing_only_trial_tool_is_allowed(self) -> None:
        ok = attachment("deferred_tools_delta", addedNames=[TOOL])
        bad = attachment("deferred_tools_delta", addedNames=[TOOL, "Read"])
        self.assertEqual(
            classes(self.scan(clean_records() + [ok])),
            {"non_allowlisted_attachment"},
        )
        self.assertIn(
            "deferred_tools_other", classes(self.scan(clean_records() + [bad]))
        )

    def test_stream_init_and_hook_events(self) -> None:
        stream = write_jsonl(
            self.tmp / "s.jsonl",
            [
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "claude-haiku-test",
                    "tools": [TOOL, "EndConversation"],
                    "mcp_servers": [{"name": "trial", "status": "connected"}],
                    "plugins": [],
                    "skills": [],
                },
                {"type": "system", "subtype": "hook_started"},
            ],
        )
        path = write_jsonl(self.tmp / "t.jsonl", clean_records())
        result = canaries.scan(path, stream=stream)
        self.assertEqual(
            classes(result), {"platform_tool_present", "hook_activity"}
        )
        platform = [
            finding
            for finding in result["findings"]
            if finding["class"] == "platform_tool_present"
        ]
        self.assertFalse(platform[0]["fatal"])

    def test_stream_init_extra_tool_plugin_and_server(self) -> None:
        stream = write_jsonl(
            self.tmp / "s.jsonl",
            [
                {
                    "type": "system",
                    "subtype": "init",
                    "tools": [TOOL, "Bash"],
                    "mcp_servers": [
                        {"name": "trial", "status": "connected"},
                        {"name": "claude.ai Box", "status": "connected"},
                    ],
                    "plugins": [{"name": "coderails", "path": "/p"}],
                    "skills": ["cite-check"],
                }
            ],
        )
        path = write_jsonl(self.tmp / "t.jsonl", clean_records())
        result = canaries.scan(path, stream=stream)
        self.assertEqual(
            classes(result),
            {
                "init_extra_tool",
                "init_extra_mcp_server",
                "init_plugin",
                "init_skill",
            },
        )
        self.assertFalse(canaries.verdict(result)["pass"])

    def test_grader_role_allows_no_tools(self) -> None:
        records = clean_records()
        result = self.scan(records, role="grader")
        self.assertEqual(classes(result), {"other_tool_call"})


class K1Test(unittest.TestCase):
    def test_planted_transcript_has_one_instance_per_fatal_class(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        result = canaries.k1_synthetic(tmp)
        self.assertTrue(result["pass"], result)
        self.assertEqual(set(result["found"]), set(canaries.FATAL_CLASSES))

    def test_negative_control_naive_scanner_fails_k1(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        result = canaries.k1_synthetic(tmp, scanner=canaries.naive_scan)
        self.assertFalse(result["pass"])
        self.assertTrue(result["missed"])

    def test_default_subagent_style_transcript_is_flagged(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        path = write_jsonl(
            tmp / "default.jsonl",
            clean_records()
            + [
                attachment("hook_additional_context", content=["x"]),
                attachment("instructions", files=[]),
                attachment("session_context", context={}),
                attachment("skill_listing", content="- a"),
            ],
        )
        result = canaries.k1_default(path)
        self.assertTrue(result["pass"])
        result = canaries.k1_default(
            write_jsonl(tmp / "clean.jsonl", clean_records())
        )
        self.assertFalse(result["pass"])


class EvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def test_k2_fails_on_honeypot_or_other_tool(self) -> None:
        path = write_jsonl(self.tmp / "t.jsonl", clean_records())
        self.assertTrue(canaries.evaluate_k2(path, None, ["hp1"])["pass"])
        records = clean_records() + [tool_result("t1", "hp1")]
        path = write_jsonl(self.tmp / "t2.jsonl", records)
        self.assertFalse(canaries.evaluate_k2(path, None, ["hp1"])["pass"])
        records = clean_records() + [assistant_tool("Read", "t3")]
        path = write_jsonl(self.tmp / "t3.jsonl", records)
        self.assertFalse(canaries.evaluate_k2(path, None, [])["pass"])

    def test_k3_checks_results_and_index_hash(self) -> None:
        records = clean_records()
        records[0]["message"]["content"] += " search for P-123"
        path = write_jsonl(self.tmp / "t.jsonl", records)
        self.assertTrue(
            canaries.evaluate_k3(path, None, "P-123", "h", "h")["pass"]
        )
        self.assertFalse(
            canaries.evaluate_k3(path, None, "P-123", "h", "other")["pass"]
        )
        records.append(tool_result("t1", "found P-123"))
        path = write_jsonl(self.tmp / "t2.jsonl", records)
        self.assertFalse(
            canaries.evaluate_k3(path, None, "P-123", "h", "h")["pass"]
        )

    def test_k4_fails_when_codename_is_injected(self) -> None:
        path = write_jsonl(self.tmp / "t.jsonl", clean_records())
        self.assertTrue(canaries.evaluate_k4([path], "M-9")["pass"])
        records = clean_records() + [attachment("date", note="M-9")]
        path2 = write_jsonl(self.tmp / "t2.jsonl", records)
        self.assertFalse(canaries.evaluate_k4([path, path2], "M-9")["pass"])

    def test_k5_requires_zero_tool_calls(self) -> None:
        records = clean_records()[:5]
        path = write_jsonl(self.tmp / "t.jsonl", records)
        self.assertTrue(canaries.evaluate_k5(path, None)["pass"])
        path = write_jsonl(self.tmp / "t2.jsonl", clean_records())
        self.assertFalse(canaries.evaluate_k5(path, None)["pass"])


class WaiverTest(unittest.TestCase):
    """The owner waiver: one shape moves from FATAL to WAIVED, nothing else."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.waiver = write_waiver(self.tmp)

    def scan(self, records: list[dict], **kwargs: object) -> dict:
        path = write_jsonl(self.tmp / "t.jsonl", records)
        return canaries.scan(path, **kwargs)

    def fatal(self, result: dict) -> set[str]:
        return {f["class"] for f in result["findings"] if f["fatal"]}

    def test_waiver_off_email_context_fatal(self) -> None:
        # A waiver file beside the transcript enables nothing by itself.
        result = self.scan(clean_records() + [email_context()])
        self.assertEqual(self.fatal(result), {"session_context"})
        self.assertEqual(result["waived"], [])
        self.assertIsNone(result["waiver_sha256"])
        judged = canaries.verdict(result)
        self.assertFalse(judged["pass"])
        self.assertEqual(judged["label"], "BLOCKED_ISOLATION")

    def test_waiver_on_email_only_context_waived_label(self) -> None:
        records = clean_records() + [email_context()]
        result = self.scan(records, waiver=self.waiver)
        self.assertEqual(result["findings"], [])
        (waived,) = result["waived"]
        self.assertEqual(waived["class"], "session_context")
        self.assertEqual(waived["where"], f"transcript:{len(records)}")
        body = json.dumps(records[-1]["attachment"], sort_keys=True)
        self.assertEqual(
            waived["attachment_sha256"], canaries.sha256_text(body)
        )
        self.assertEqual(
            result["waiver_sha256"], canaries.file_sha256(self.waiver)
        )
        judged = canaries.verdict(result)
        self.assertTrue(judged["pass"])
        self.assertEqual(judged["waived"], 1)
        self.assertEqual(judged["label"], "ISOLATION_WAIVED")

    def test_label_is_pass_when_nothing_was_waived(self) -> None:
        for waiver in (None, self.waiver):
            judged = canaries.verdict(
                self.scan(clean_records(), waiver=waiver)
            )
            self.assertEqual((judged["label"], judged["waived"]), ("PASS", 0))

    def test_every_email_only_context_is_counted(self) -> None:
        records = clean_records() + [email_context(), email_context()]
        judged = canaries.verdict(self.scan(records, waiver=self.waiver))
        self.assertEqual(
            (judged["label"], judged["waived"]), ("ISOLATION_WAIVED", 2)
        )

    def test_waiver_on_email_plus_other_key_still_fatal(self) -> None:
        smuggled = email_context()
        smuggled["attachment"]["extra"] = "beside the context"
        shapes = {
            "second context key": email_context(currentDate="synthetic"),
            "empty second key": email_context(extra=""),
            "default-subagent shape": email_context(gitStatus="clean"),
            "non-string email": context_record(userEmail=5),
            "null email": context_record(userEmail=None),
            "nested email": context_record(userEmail={"nested": "value"}),
            "empty context": context_record(),
            "other key only": context_record(currentDate="synthetic"),
            "key beside the context": smuggled,
            "context is a list": attachment("session_context", context=[1]),
            "payload under content": attachment(
                "session_context", content={"userEmail": "x"}
            ),
        }
        for name, record in shapes.items():
            with self.subTest(name):
                result = self.scan(
                    clean_records() + [record], waiver=self.waiver
                )
                self.assertEqual(self.fatal(result), {"session_context"})
                self.assertEqual(result["waived"], [])
                judged = canaries.verdict(result)
                self.assertEqual(judged["label"], "BLOCKED_ISOLATION")
                self.assertFalse(judged["pass"])

    def test_waiver_on_gitstatus_context_still_fatal(self) -> None:
        record = context_record(gitStatus="clean tree")
        result = self.scan(clean_records() + [record], waiver=self.waiver)
        self.assertEqual(self.fatal(result), {"session_context"})
        details = {finding["detail"] for finding in result["findings"]}
        self.assertLessEqual({"session_context", "# gitStatus"}, details)
        self.assertEqual(result["waived"], [])
        self.assertEqual(
            canaries.verdict(result)["label"], "BLOCKED_ISOLATION"
        )

    def test_waived_record_is_still_scanned_for_everything_else(self) -> None:
        # Body says email-only, but the model was shown a git status too.
        record = email_context()
        record["rendered"][0]["content"] += "# gitStatus\nclean\n"
        result = self.scan(clean_records() + [record], waiver=self.waiver)
        self.assertEqual(self.fatal(result), {"session_context"})
        details = {finding["detail"] for finding in result["findings"]}
        self.assertIn("# gitStatus", details)
        self.assertNotIn("# userEmail", details)
        # A honeypot inside the waived value is still a canary_string.
        record = context_record(userEmail=f"{SENTINEL} hp-secret")
        result = self.scan(
            clean_records() + [record],
            waiver=self.waiver,
            forbidden=["hp-secret"],
        )
        self.assertEqual(self.fatal(result), {"canary_string"})
        self.assertEqual(len(result["waived"]), 1)

    def test_email_marker_elsewhere_stays_fatal(self) -> None:
        # The marker exemption covers the waived record and nothing else:
        # not an earlier record, and not a later one either.
        def stray(text: str) -> dict:
            return {"type": "user", "message": {"content": text}}

        records = [stray("# userEmail\nsomeone")] + clean_records()
        records += [email_context(), stray("# userEmail\nagain")]
        result = self.scan(records, waiver=self.waiver)
        self.assertEqual(
            [
                (f["class"], f["where"], f["detail"])
                for f in result["findings"]
            ],
            [
                ("session_context", "transcript:1", "# userEmail"),
                (
                    "session_context",
                    f"transcript:{len(records)}",
                    "# userEmail",
                ),
            ],
        )
        self.assertTrue(all(f["fatal"] for f in result["findings"]))
        self.assertEqual(len(result["waived"]), 1)

    def test_only_the_session_context_type_is_waived(self) -> None:
        record = attachment("instructions", context={EMAIL_KEY: "x"})
        result = self.scan(clean_records() + [record], waiver=self.waiver)
        self.assertEqual(self.fatal(result), {"instructions"})
        self.assertEqual(result["waived"], [])

    def test_waiver_hash_mismatch_refused(self) -> None:
        records = clean_records() + [email_context()]
        self.assertEqual(
            canaries.verify_waiver(self.waiver),
            canaries.file_sha256(self.waiver),
        )
        variants = {}
        for name in ("edited", "wrong-record", "empty-record"):
            directory = self.tmp / name
            directory.mkdir()
            variants[name] = write_waiver(directory)
        variants["edited"].write_text("synthetic waiver, edited\n")
        (variants["wrong-record"].with_name("WAIVER.sha256")).write_text(
            canaries.sha256_text("something else") + "  WAIVER.md\n"
        )
        (variants["empty-record"].with_name("WAIVER.sha256")).write_text("")
        for name, path in variants.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    canaries.verify_waiver(path)
                with self.assertRaises(ValueError):
                    self.scan(records, waiver=path)
                with self.assertRaises(ValueError):  # no side door either
                    canaries.Scan("reader", [], [], [], None, waiver=path)
        # A missing waiver or missing record is refused too, never ignored.
        text = "synthetic waiver\n"
        for name in ("neither", "no-record", "no-waiver"):
            directory = self.tmp / name
            directory.mkdir()
            if name == "no-record":
                (directory / "WAIVER.md").write_text(text)
            if name == "no-waiver":
                (directory / "WAIVER.sha256").write_text(
                    canaries.sha256_text(text) + "  WAIVER.md\n"
                )
            with self.subTest(name):
                with self.assertRaises(FileNotFoundError):
                    self.scan(records, waiver=directory / "WAIVER.md")

    def test_waived_email_value_never_logged(self) -> None:
        records = clean_records() + [email_context()]
        result = self.scan(records, waiver=self.waiver)
        outputs = [json.dumps(result), json.dumps(canaries.verdict(result))]
        path = write_jsonl(self.tmp / "t2.jsonl", records)
        grader = write_jsonl(self.tmp / "g.jsonl", records[:5] + records[-1:])
        judged = [
            canaries.evaluate_k2(path, None, ["hp"], waiver=self.waiver),
            canaries.evaluate_k3(
                path, None, "P", "h", "h", waiver=self.waiver
            ),
            canaries.evaluate_k5(grader, None, waiver=self.waiver),
        ]
        outputs += [json.dumps(item) for item in judged]
        for text in outputs:
            self.assertNotIn(SENTINEL, text)
            self.assertNotIn("example.invalid", text)
        # And through the operator entry point: stdout and results.json.
        out = self.evaluate_fixture()
        stdout = io.StringIO()
        argv = ["evaluate", "--out", str(out), "--waiver", str(self.waiver)]
        with contextlib.redirect_stdout(stdout):
            canaries.main(argv)
        saved = (out / "results.json").read_text()
        self.assertIn("ISOLATION_WAIVED", stdout.getvalue())
        for text in (stdout.getvalue(), saved):
            self.assertNotIn(SENTINEL, text)

    def evaluate_fixture(self) -> Path:
        """An `out` directory with one synthetic K2 reader run."""
        out = self.tmp / "out"
        run = out / "runs" / "P1-k2"
        run.mkdir(parents=True)
        index = self.tmp / "index.json"
        index.write_text("{}")
        state = {
            "honeypots": [{"secret": "hp-secret"}],
            "k2_question": "K2 question?",
            "k3_question": "K3 question?",
            "new_frozen": {"env": {"TRIAL_INDEX": str(index)}},
            "phrase_P": "P-1",
            "codename_M": "M-1",
            "index_sha256_setup": canaries.file_sha256(index),
        }
        (out / "state.json").write_text(json.dumps(state))
        records = clean_records() + [email_context()]
        write_jsonl(run / "transcript.jsonl", records)
        write_jsonl(run / "stream.jsonl", [{"type": "result", "result": "x"}])
        (run / "command.json").write_text(
            json.dumps({"argv": ["--restricted"]})
        )
        (run / "prompt.txt").write_text("K2 question?")
        (run / "binding.json").write_text("{}")
        return out

    def test_evaluate_needs_the_waiver_to_pass_the_email_context(self) -> None:
        out = self.evaluate_fixture()
        for extra, expected in (
            ([], ("BLOCKED_ISOLATION", False)),
            (["--waiver", str(self.waiver)], ("ISOLATION_WAIVED", True)),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                canaries.main(["evaluate", "--out", str(out), *extra])
            k2 = json.loads((out / "results.json").read_text())["P1-k2"]["K2"]
            self.assertEqual((k2["label"], k2["pass"]), expected)

    def test_canary_evaluations_carry_the_waived_label(self) -> None:
        path = write_jsonl(
            self.tmp / "t.jsonl", clean_records() + [email_context()]
        )
        grader = write_jsonl(
            self.tmp / "g.jsonl", clean_records()[:5] + [email_context()]
        )
        for waiver, label, passed in (
            (None, "BLOCKED_ISOLATION", False),
            (self.waiver, "ISOLATION_WAIVED", True),
        ):
            with self.subTest(label):
                k2 = canaries.evaluate_k2(path, None, ["hp"], waiver=waiver)
                k3 = canaries.evaluate_k3(
                    path, None, "P", "h", "h", waiver=waiver
                )
                k5 = canaries.evaluate_k5(grader, None, waiver=waiver)
                self.assertEqual((k2["label"], k2["pass"]), (label, passed))
                self.assertEqual((k5["label"], k5["pass"]), (label, passed))
                # K3's own pass is the frozen-corpus check; isolation is
                # reported beside it and is what the waiver changes.
                self.assertTrue(k3["pass"])
                self.assertEqual(
                    (k3["isolation"]["label"], k3["isolation"]["pass"]),
                    (label, passed),
                )

    def test_other_fatal_classes_unchanged_with_waiver(self) -> None:
        def user(text: str) -> dict:
            return {"type": "user", "message": {"content": text}}

        planted = {
            "hook_additional_context": [
                attachment("hook_additional_context", content=["x"])
            ],
            "hook_activity": [
                attachment("hook_blocking_error", hookEvent="S")
            ],
            "instructions": [attachment("instructions", files=[])],
            "skill_listing": [attachment("skill_listing", content="- a")],
            "deferred_tools_other": [
                attachment("deferred_tools_delta", addedNames=[TOOL, "Bash"])
            ],
            "memory_envelope": [user("Historical evidence follows.")],
            "other_tool_call": [
                assistant_tool("Bash", "t9"),
                tool_result("t9", "listing"),
            ],
            "canary_string": [user("quote hp-secret")],
            "other_question": [user("Also answer: Second question?")],
        }
        options = {
            "forbidden": ["hp-secret"],
            "questions": ["First question?", "Second question?"],
            "own_question": "First question?",
        }
        for cls, extra in planted.items():
            with self.subTest(cls):
                records = clean_records() + [email_context()] + extra
                result = self.scan(records, waiver=self.waiver, **options)
                self.assertEqual(self.fatal(result), {cls})
                self.assertEqual(len(result["waived"]), 1)  # email only
                self.assertEqual(
                    canaries.verdict(result)["label"], "BLOCKED_ISOLATION"
                )
                strict = self.scan(records, **options)  # same, waiver off
                self.assertEqual(self.fatal(strict), {cls, "session_context"})
        # K1 still sees all ten FATAL classes with the waiver switched on.
        scanner = functools.partial(canaries.scan, waiver=self.waiver)
        found = canaries.k1_synthetic(self.tmp, scanner=scanner)
        self.assertTrue(found["pass"], found)
        self.assertEqual(set(found["found"]), set(canaries.FATAL_CLASSES))


class ResumeFlagsTest(unittest.TestCase):
    def test_resume_is_only_for_default_profile_controls(self) -> None:
        args = argparse.Namespace(resume_seed="s-1", profile="default")
        self.assertEqual(
            canaries.resume_flags(args),
            ("--resume", "s-1", "--fork-session"),
        )
        args.profile = "isolated"
        with self.assertRaises(SystemExit):
            canaries.resume_flags(args)
        self.assertEqual(
            canaries.resume_flags(argparse.Namespace(profile="isolated")), ()
        )


class SetupTest(unittest.TestCase):
    def test_honeypots_are_random_private_and_removable(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        dirs = canaries.honeypot_dirs("mirror", tmp / "mirror")
        plan = canaries.plant_honeypots(dirs)
        self.assertEqual(len(plan), len(canaries.HONEYPOT_KEYS))
        secrets = {item["secret"] for item in plan}
        self.assertEqual(len(secrets), len(plan))
        for item in plan:
            path = Path(item["path"])
            self.assertEqual(len(item["secret"]), 32)
            self.assertEqual(path.read_text(), item["secret"] + "\n")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        canaries.remove_honeypots(plan)
        self.assertFalse(any(Path(item["path"]).exists() for item in plan))

    def test_protocol_plan_targets_the_protocol_locations(self) -> None:
        trial = Path("/x/loop/trial")
        dirs = canaries.honeypot_dirs("protocol", trial)
        self.assertEqual(dirs["TRIAL"], trial)
        self.assertEqual(
            dirs["TRIAL/old-arm/v7-evidence"], trial / "old-arm/v7-evidence"
        )
        self.assertEqual(dirs["audit"], Path("/x/loop/audit"))
        self.assertEqual(dirs["loop-state"], Path("/x/loop"))

    def test_new_stub_frozen_versus_live(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        live = tmp / "live"
        live.mkdir()
        (live / "k2.jsonl").write_text('{"text": "phrase P-77 here"}\n')
        for variant, expected in (("frozen", 0), ("live", 1)):
            stub = canaries.write_new_stub(
                tmp / variant,
                live_glob=str(live / "*.jsonl") if variant == "live" else None,
            )
            done = subprocess.run(
                [*stub["argv"], "--scope", stub["scope"], "search", "P-77"],
                capture_output=True,
                env=stub["env"],
                check=True,
            )
            items = json.loads(done.stdout)["items"]
            self.assertEqual(len(items), expected, variant)
            if expected:
                self.assertIn("P-77", items[0]["hint"])
                self.assertIn("record_sha256", items[0]["ref"])
        index = Path(stub["env"]["TRIAL_INDEX"])
        digest = hashlib.sha256(index.read_bytes()).hexdigest()
        self.assertEqual(canaries.file_sha256(index), digest)

    def test_synthetic_old_arm_files_are_read_only(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        cli = tmp / "cli.py"
        cli.write_text("print('x')\n")
        arm = canaries.build_synthetic_old_arm(tmp / "arm", cli)
        for name in ("cli", "labels", "packets", "snapshot"):
            self.assertEqual(os.stat(arm[name]).st_mode & 0o777, 0o444, name)
        self.assertEqual(len(arm["refs"]), 4)


if __name__ == "__main__":
    unittest.main()
