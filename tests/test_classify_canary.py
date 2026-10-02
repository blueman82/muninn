"""Canary-scenario and adversarial-input tests for classification."""

from __future__ import annotations

import hashlib
import json
import time
import unittest
from pathlib import Path

from pctx import classify as c

FIXTURES = Path(__file__).parent / "fixtures" / "classify"


def classify_file(
    name: str,
) -> tuple[c.ThreadInfo, list[tuple[c.EventRec, bytes]]]:
    """Classify a fixture read as raw JSONL.

    Args:
        name: File name under the classify fixtures; ``codex*`` names use
            the Codex classifier, anything else the Claude one.

    Returns:
        The thread facts and each event paired with its raw source line.
    """
    raws = (FIXTURES / name).read_bytes().splitlines(keepends=True)
    first, state = json.loads(raws[0]), c.CodexState()
    if name.startswith("codex"):
        thread = c.codex_thread(first)
        events = c.codex_events
    else:
        thread = c.claude_thread(f"-work-repo/{name}", first)
        events = c.claude_events
    found: list[tuple[c.EventRec, bytes]] = []
    for line, raw in enumerate(raws, start=1):
        found += [(e, raw) for e in events(json.loads(raw), line, state)]
    return thread, found


class CanaryScenarioTests(unittest.TestCase):
    """A canary string in another thread must not become eligible text.

    Runs the fixtures as raw bytes through the classifiers: only the native
    parent prompt may be both primary and unflagged.
    """

    NAMES = (
        "codex-parent.jsonl",
        "codex-fork.jsonl",
        "codex-guardian.jsonl",
        "claude-main.jsonl",
    )

    def test_default_eligible_canary_is_only_the_native_parent_prompt(
        self,
    ) -> None:
        eligible, seen = [], {}
        for name in self.NAMES:
            thread, found = classify_file(name)
            for event, raw in found:
                for canary in ("CANARY-A", "CANARY-B", "CANARY-C", "CANARY-D"):
                    if canary in event.text:
                        seen.setdefault(canary, []).append(
                            (
                                name,
                                thread.thread_class,
                                event.kind,
                                event.flags,
                            )
                        )
                if (
                    thread.thread_class == "primary"
                    and event.kind in ("prompt", "reply", "tool_call")
                    and not event.flags & 1
                    and "CANARY" in event.text
                ):
                    digest = hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest()
                    self.assertEqual(c.record_hash(raw), digest)
                    eligible.append((name, event.line, event.part))
        self.assertEqual(eligible, [("codex-parent.jsonl", 3, 1)])
        self.assertEqual(
            seen,
            {
                "CANARY-A": [
                    ("codex-guardian.jsonl", "reviewer", "prompt", 0)
                ],
                "CANARY-B": [("codex-parent.jsonl", "primary", "prompt", 0)],
                "CANARY-C": [
                    ("codex-fork.jsonl", "subagent", "delegation", 0)
                ],
                "CANARY-D": [("claude-main.jsonl", "primary", "prompt", 1)],
            },
        )


class AdversarialInputTests(unittest.TestCase):
    """Pathological transcript text must not stall the poller.

    Transcript text is untrusted input, so the regexes must stay
    near-linear (quadratic ones took minutes).
    """

    CASES = (
        "http://x" * 40_000,  # URLs with no whitespace
        '"a=' * 40_000,  # quote + env-assignment starts
        "-----BEGIN PRIVATE KEY----- x " * 20_000,  # BEGIN without END
        "token" + "\n" * 150_000,  # split-secret whitespace run
        "x\n" + "\n" * 150_000 + "Tests",  # blank lines at line starts
        "ok" + " \n" * 100_000,
    )

    def test_regexes_stay_fast_on_pathological_text(self) -> None:
        for text in self.CASES:
            with self.subTest(text=text[:12]):
                start = time.perf_counter()
                c.redact(text)
                c.PCTX_CALL.search(text)
                c._is_error(text)
                self.assertLess(time.perf_counter() - start, 5.0)
