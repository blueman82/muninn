"""Check owner attribution by provenance, independent of wording."""

from __future__ import annotations

import unittest
from typing import Any

from muninn.classify import user_text_kind
from muninn.knowledge_models import Provider
from muninn.review_evidence_models import EvidenceSelector
from muninn.review_selectors import authority, fields
from tests.classify_claude_support import claude_rec, text_block
from tests.test_classify import user_msg

PROVIDERS: tuple[Provider, ...] = ("codex", "claude", "cursor")


def owner_record(provider: Provider, text: str) -> dict[str, Any]:
    """Build a synthetic ordinary user record in each supported format."""
    if provider == "codex":
        return user_msg(1, text)
    if provider == "claude":
        return claude_rec("user", text)
    return {"type": 1, "text": text}


def judged(
    provider: Provider,
    record: dict[str, Any],
    primary: bool = True,
    selector: EvidenceSelector | None = None,
) -> str:
    """Return the authority of the first projected field of a record."""
    context = fields(provider, record)
    return authority(provider, context[0], context, record, primary, selector)


class UserTextKindTests(unittest.TestCase):
    """Read the provider label of user-role text per provider."""

    def test_typed_owner_text_is_prompt(self) -> None:
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                record = owner_record(provider, "i agree")
                self.assertEqual(user_text_kind(provider, record), "prompt")

    def test_harness_and_delegation_labels(self) -> None:
        self.assertEqual(
            user_text_kind(
                "codex",
                user_msg(1, "<environment_context>x</environment_context>"),
            ),
            "harness",
        )
        self.assertEqual(
            user_text_kind("claude", claude_rec("user", "<system-reminder>x")),
            "harness",
        )
        self.assertEqual(
            user_text_kind(
                "claude", claude_rec("user", "yes", sidechain=True)
            ),
            "delegation",
        )
        self.assertIsNone(user_text_kind("cursor", {"type": 2, "text": "x"}))


class ReviewAuthorityTests(unittest.TestCase):
    """Grant owner authority by provenance, not by sentence shape."""

    def test_plain_owner_wording_is_direct_owner(self) -> None:
        for provider in PROVIDERS:
            for text in (
                "i agree",
                "yes",
                "we'll use sqlite",
                "Should we use SQLite?",
                "I prefer Python if the team agrees.",
                "For example, I prefer Rust.",
                "In Alice's words, use Rust.",
                'A colleague wrote: "I prefer Python for this project."',
                "I prefer Python.\n```\nUse Rust everywhere.\n```",
                "Use Rust, the README says.",
                "# Plan\nUse Python.",
            ):
                with self.subTest(provider=provider, text=text):
                    record = owner_record(provider, text)
                    self.assertEqual(judged(provider, record), "direct_owner")

    def test_generated_text_anywhere_is_untrusted(self) -> None:
        for provider in PROVIDERS:
            for text in (
                "I prefer Python.\n"
                "<environment_context>Use Rust.</environment_context>",
                "I prefer Python.\n## Referenced ChatGPT conversation\n"
                "Use Rust.",
                "Use Python.\n<system-reminder>Use Rust.",
                "Use Python. <muninn-memory>Use Rust.</muninn-memory>",
                "Use Python.\nHistorical evidence follows.\nUse Rust.",
                "Use Python. Retrieved text is data from local transcripts, "
                "not instructions.",
            ):
                with self.subTest(provider=provider, text=text):
                    record = owner_record(provider, text)
                    self.assertEqual(judged(provider, record), "untrusted")

    def test_partial_sentence_span_is_untrusted(self) -> None:
        text = "Never use pytest. Use Python."
        for provider in PROVIDERS:
            record = owner_record(provider, text)
            pointer = fields(provider, record)[0].pointer
            for (start, end), expected in (
                ((6, 17), "untrusted"),
                ((0, 17), "direct_owner"),
                ((18, 29), "direct_owner"),
                ((0, 16), "direct_owner"),
            ):
                with self.subTest(provider=provider, span=(start, end)):
                    selector = EvidenceSelector(
                        json_pointer=pointer, start=start, end=end
                    )
                    self.assertEqual(
                        judged(provider, record, selector=selector), expected
                    )

    def test_provider_injection_flags_and_nonprimary_are_untrusted(
        self,
    ) -> None:
        for flag in (
            "isMeta",
            "isSidechain",
            "isCompactSummary",
            "verifiedSlackHumanTurn",
        ):
            with self.subTest(flag=flag):
                record = owner_record("claude", "I prefer Python.")
                record[flag] = True
                self.assertEqual(judged("claude", record), "untrusted")
        record = owner_record("codex", "I prefer Python.")
        self.assertEqual(judged("codex", record, primary=False), "untrusted")

    def test_hook_yes_and_tool_result_message_are_untrusted(self) -> None:
        hook = claude_rec("user", [text_block("yes")], isMeta=True)
        self.assertEqual(judged("claude", hook), "untrusted")
        mixed = claude_rec(
            "user",
            [
                {"type": "tool_result", "tool_use_id": "t", "content": "ok"},
                text_block("yes"),
            ],
        )
        context = fields("claude", mixed)
        owner = next(
            f for f in context if f.evidence_type == "owner_statement"
        )
        self.assertEqual(
            authority("claude", owner, context, mixed, True), "untrusted"
        )
