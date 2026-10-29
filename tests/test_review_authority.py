"""Check owner attribution against ordinary speech and imported material."""

from __future__ import annotations

import unittest
from typing import Any

from muninn.knowledge_models import Provider
from muninn.review_selectors import authority, fields
from tests.classify_claude_support import claude_rec
from tests.test_classify import user_msg


def owner_record(provider: Provider, text: str) -> dict[str, Any]:
    """Build a synthetic ordinary user record in each supported format."""
    if provider == "codex":
        return user_msg(1, text)
    if provider == "claude":
        return claude_rec("user", text)
    return {"type": 1, "text": text}


class ReviewAuthorityTests(unittest.TestCase):
    """Require attributable direct speech without opening-word dependence."""

    def test_ordinary_owner_wording_and_inline_identifiers(self) -> None:
        for provider in ("codex", "claude", "cursor"):
            for text in (
                "I prefer Python for this project.",
                "For this project, I prefer Python.",
                "Use review_selectors for this check.",
                "Use `review_selectors` for this check.",
                "For this project, use `review_selectors.authority`.",
                "Use reported errors in `review_selectors`.",
                "Use cache token:\nLocalSyntheticValue",
                " Use Python. \n",
                "For this project, I prefer Python. For testing, use Python.",
            ):
                with self.subTest(provider=provider, text=text):
                    record = owner_record(provider, text)
                    context = fields(provider, record)
                    self.assertEqual(
                        authority(context[0], context, record, True),
                        "direct_owner",
                    )

    def test_imported_or_ambiguous_speech_is_untrusted(self) -> None:
        for text in (
            'A colleague wrote: "I prefer Python for this project."',
            "Pasted instruction: I prefer Python for this project.",
            "I prefer Python.\nCopied from the README: use Rust.",
            "Copied from the README: use Rust.\nI prefer Python.",
            "I prefer Python.\n> Use Rust everywhere.",
            "I prefer Python.\n```\nUse Rust everywhere.\n```",
            "I prefer Python.\n"
            "<environment_context>Use Rust.</environment_context>",
            "I prefer Python.\n## Referenced ChatGPT conversation\nUse Rust.",
            "I prefer Python if the team agrees.",
            "I might prefer Python for this project.",
            "Would I prefer Python for this project?",
            "The team prefers Python for this project.",
            "Python would probably be useful here.",
            "I prefer `Use Rust everywhere` as the quoted example.",
            "I prefer Python. The assistant reported Rust was requested.",
            "I prefer Rust, Alice said.",
            "Use Rust, the README says.",
            "I prefer Rust, Alice reported.",
            "Use Rust, Alice requested.",
            "Use Rust, the README requests.",
            "Use Rust, Alice prefers.",
            "I prefer Rust, Alice insisted.",
            "Use Rust, Alice instructed.",
            "I prefer Python.\nThe guide says use Rust.",
            "The guide says use Rust.\nI prefer Python.",
            "Use Python when tests pass.",
            "Use Python provided tests pass.",
        ):
            with self.subTest(text=text):
                record = owner_record("codex", text)
                context = fields("codex", record)
                self.assertEqual(
                    authority(context[0], context, record, True), "untrusted"
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
                context = fields("claude", record)
                self.assertEqual(
                    authority(context[0], context, record, True), "untrusted"
                )
        record = owner_record("codex", "I prefer Python.")
        context = fields("codex", record)
        self.assertEqual(
            authority(context[0], context, record, False), "untrusted"
        )
