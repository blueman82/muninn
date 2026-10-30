"""Bind owner decisions to complete, untransformed synthetic displays."""

from __future__ import annotations

from muninn import classify, decision_requests, review_commit, review_requests
from muninn.decision_apply import enrich_support
from muninn.decision_candidates import candidate_view
from muninn.decision_models import DecisionIntent
from muninn.decision_write import submit_intent
from muninn.review_evidence import select_evidence
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceSelector,
    SourceRef,
)
from tests.classify_claude_support import claude_rec
from tests.decision_support import DecisionCase
from tests.ingest_support import line, rollout
from tests.test_decision_apply import ApplicationCase


class DecisionPresentationTests(DecisionCase):
    """Check actual originals through acceptance and application."""

    def presentation(
        self, ident: str, provider: str, content: object, index: int = 0
    ) -> DecisionIntent:
        """Index complete content and select its canonical field."""
        if provider == "codex":
            intent = self.rewrite(
                self.intent(ident), display_updates={"content": content}
            )
            return intent.model_copy(
                update={
                    "display_selector": EvidenceSelector(
                        json_pointer=(
                            "/payload/content"
                            if isinstance(content, str)
                            else f"/payload/content/{index}/text"
                        ),
                        start=None,
                        end=None,
                    )
                }
            )
        rows = [
            claude_rec("assistant", content),
            claude_rec("user", f"Please approve memory {ident}."),
        ]
        self.write("project/sess-1.jsonl", rows, "claude-projects")
        self.run_ingest()
        source = SourceRef(provider="claude", thread_id="sess-1")
        return DecisionIntent(
            candidate_id=ident,
            action="approve",
            replacement_id=None,
            owner_locator=EvidenceLocator(
                source=source,
                line=2,
                part=1,
                line_sha256=classify.record_hash(line(rows[1])),
            ),
            display_locator=EvidenceLocator(
                source=source,
                line=1,
                part=index + 1,
                line_sha256=classify.record_hash(line(rows[0])),
            ),
            owner_selector=EvidenceSelector(
                json_pointer="/message/content", start=None, end=None
            ),
            display_selector=EvidenceSelector(
                json_pointer=(
                    "/message/content"
                    if isinstance(content, str)
                    else f"/message/content/{index}/text"
                ),
                start=None,
                end=None,
            ),
        )

    def refused(self, intent: DecisionIntent) -> None:
        """Require refusal before acceptance or application output."""
        before = self.projection()
        with self.assertRaisesRegex(ValueError, "^display_proof$"):
            self.approve(intent)
        self.assertEqual(self.projection(), before)
        self.transaction(lambda: submit_intent(self.conn, intent, 20.0))
        view = decision_requests.request_application(
            self.home, intent.candidate_id, 21.0
        )
        self.assertEqual(view.code, "display_proof")
        self.assertEqual(view.revision, 0)
        self.assertIsNone(view.receipt)
        self.assertEqual(
            tuple(
                self.row(
                    "SELECT accepted_revision,owner_evidence_id,"
                    "display_evidence_id "
                    "FROM memory_decision WHERE candidate_id=?",
                    (intent.candidate_id,),
                )
            ),
            (None, None, None),
        )
        after = self.projection()
        for name, rows in before.items():
            if name.startswith("knowledge") or name in (
                "review_evidence",
                "review_request",
                "review_receipt",
            ):
                self.assertEqual(after[name], rows, name)

    def test_extra_text_blocks_cannot_hide_prose_or_whitespace(self) -> None:
        for provider in ("codex", "claude"):
            for extra in (
                "This is only an example. Do not save this proposed memory.",
                "Additional display instruction: this proposal is only a "
                "preview and will not save anything.",
                " ",
                "\n",
                "",
            ):
                for index in (0, 1):
                    with self.subTest(
                        provider=provider, index=index, size=len(extra)
                    ):
                        draft = self.stage()
                        kind = "output_text" if provider == "codex" else "text"
                        blocks = [{"type": kind, "text": extra}]
                        blocks.insert(
                            index,
                            {"type": kind, "text": self.display(draft.id)},
                        )
                        self.refused(
                            self.presentation(
                                draft.id, provider, blocks, index
                            )
                        )

    def test_unprojected_material_cannot_hide_beside_display(self) -> None:
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                draft = self.stage()
                kind = "output_text" if provider == "codex" else "text"
                self.refused(
                    self.presentation(
                        draft.id,
                        provider,
                        [
                            {"type": kind, "text": self.display(draft.id)},
                            {"type": "image", "source": {"type": "synthetic"}},
                        ],
                    )
                )

    def test_whitespace_in_selected_field_is_not_trimmed(self) -> None:
        for provider in ("codex", "claude"):
            for prefix, suffix in ((" ", ""), ("", "\n")):
                with self.subTest(provider=provider, prefix=bool(prefix)):
                    draft = self.stage()
                    self.refused(
                        self.presentation(
                            draft.id,
                            provider,
                            prefix + self.display(draft.id) + suffix,
                        )
                    )

    def test_redaction_equivalent_original_cannot_authorize(self) -> None:
        token = "sk-" + "X" * 24
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                draft = self.stage(
                    self.result(
                        [
                            self.note(
                                content=(
                                    "Use [redacted:secret] as a marker. "
                                    "Another sentence. "
                                )
                            )
                        ]
                    )
                )
                canonical = self.display(draft.id)
                shown = canonical.replace("[redacted:secret]", token)
                self.assertTrue(shown != canonical)
                intent = self.presentation(draft.id, provider, shown)
                evidence = select_evidence(
                    self.home, intent.display_locator, intent.display_selector
                )
                self.assertTrue(evidence.excerpt == canonical)
                self.refused(intent)
                self.persist(evidence)
                self.assertFalse(token in str(self.projection()))
                for path in self.home.rglob("*.jsonl"):
                    self.assertFalse(token.encode() in path.read_bytes())

    def save_exact(
        self, provider: str, *, scalar: bool, marker: bool = False
    ) -> None:
        """Save one exact scalar or block through normal checked execution."""
        draft = self.stage(
            self.result(
                [
                    self.note(
                        content=(
                            (
                                "Use [redacted:secret] as a marker. "
                                "Another sentence. "
                            )
                            if marker
                            else "Use Python."
                        )
                    )
                ]
            )
        )
        shown = self.display(draft.id)
        kind = "output_text" if provider == "codex" else "text"
        content = shown if scalar else [{"type": kind, "text": shown}]
        intent = self.presentation(draft.id, provider, content)
        proof = self.approve(intent)
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 20.0
            )
        )
        view = decision_requests.request_application(self.home, draft.id, 21.0)
        self.assertEqual(view.state, "saved")
        assert view.receipt is not None
        self.assertEqual(len(view.receipt.applied), 1)
        self.assertEqual(self.row("SELECT count(*) FROM knowledge")[0], 1)
        self.assertEqual(
            self.row(
                "SELECT excerpt FROM review_evidence WHERE id=?",
                (proof.display_evidence_id,),
            )[0],
            shown,
        )

    def test_exact_codex_block_saves(self) -> None:
        self.save_exact("codex", scalar=False)

    def test_exact_codex_scalar_saves(self) -> None:
        self.save_exact("codex", scalar=True)

    def test_exact_claude_block_saves(self) -> None:
        self.save_exact("claude", scalar=False)

    def test_exact_claude_scalar_saves(self) -> None:
        self.save_exact("claude", scalar=True)

    def test_literal_codex_marker_saves(self) -> None:
        self.save_exact("codex", scalar=False, marker=True)

    def test_literal_claude_marker_saves(self) -> None:
        self.save_exact("claude", scalar=False, marker=True)

    def test_retained_approval_cannot_admit_a_changed_display(self) -> None:
        draft = self.stage()
        intent = self.intent(draft.id)
        self.approve(intent)
        retained = self.projection()
        self.rewrite(
            intent,
            display_updates={
                "content": [
                    {"type": "output_text", "text": self.display(draft.id)},
                    {"type": "output_text", "text": "Do not save this."},
                ]
            },
        )
        view = decision_requests.request_application(self.home, draft.id, 21.0)
        self.assertIsNone(view.receipt)
        self.assertEqual(view.revision, 1)
        for name in (
            "memory_decision",
            "review_evidence",
            "review_request",
            "knowledge",
            "review_receipt",
        ):
            self.assertEqual(self.projection()[name], retained[name], name)


class PresentationCommitTests(ApplicationCase):
    """Revalidate immutable physical proof after application admission."""

    def test_changed_original_cannot_commit_retained_approval(self) -> None:
        ident, context = self.application()
        candidate = candidate_view(self.conn, ident).candidate
        assert candidate is not None
        retained = self.governed()
        self.records[-2]["payload"]["content"].append(
            {"type": "output_text", "text": "Do not save this."}
        )
        self.write(rollout(), self.records)
        self.run_ingest()
        with self.assertRaisesRegex(ValueError, "rejected_evidence"):
            review_commit.commit_review(
                self.conn,
                context,
                enrich_support(candidate, self.proof.owner_evidence_id),
            )
        self.assertEqual(self.governed(), retained)
        self.assertIsNone(candidate_view(self.conn, ident).receipt)
