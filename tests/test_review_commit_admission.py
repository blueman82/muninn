"""Check request-owned admission independently from the trigger window."""

from __future__ import annotations

from unittest import mock

from muninn import review_commit, review_evidence, review_originals
from muninn.knowledge_models import AffectedRevision
from muninn.review_json import canonical_json
from tests.test_review_commit import CommitCase


class ReviewCommitAdmissionTests(CommitCase):
    """Model controlled knowledge reads and reject unadmitted support."""

    def admit_current_knowledge(self) -> AffectedRevision:
        """Link support derived from a permitted current stored revision."""
        saved = self.save(self.result([self.note()])).applied[0]
        self.context = self.start(3, 3)
        ids = tuple(
            row[0]
            for row in self.conn.execute(
                "SELECT DISTINCT s.evidence_id FROM knowledge k "
                "JOIN knowledge_version v ON v.knowledge_id=k.id "
                "AND v.revision=k.current_revision "
                "JOIN knowledge_support s ON s.knowledge_id=k.id "
                "AND s.revision=k.current_revision WHERE k.id=? "
                "AND k.state='active' AND v.scope_json=?",
                (saved.knowledge_id, canonical_json(self.scope)),
            )
        )
        self.assertEqual(ids, (self.evidence[0].id,))
        self.conn.executemany(
            "INSERT INTO review_input VALUES(?,?)",
            [(self.context.request_id, ident) for ident in ids],
        )
        self.context = self.context.model_copy(
            update={"evidence_ids": (*self.context.evidence_ids, *ids)}
        )
        return saved

    def replacement(self, saved: AffectedRevision) -> dict[str, object]:
        """Create a replacement with its exact program-observed revision."""
        return self.note(
            action="replace",
            knowledge_id=saved.knowledge_id,
            expected_revision=saved.revision,
        )

    def test_context_requires_complete_unique_durable_input(self) -> None:
        for fault in ("missing", "extra", "duplicate"):
            with self.subTest(fault=fault):
                case = CommitCase()
                case.setUp()
                self.addCleanup(case.doCleanups)
                ids = case.context.evidence_ids
                values = {
                    "missing": ids[:-1],
                    "extra": (*ids, "E_" + "f" * 32),
                    "duplicate": (*ids, ids[0]),
                }
                case.context = case.context.model_copy(
                    update={"evidence_ids": values[fault]}
                )
                with (
                    mock.patch.object(
                        review_evidence,
                        "originals",
                        side_effect=AssertionError("unexpected read"),
                    ),
                    self.assertRaisesRegex(ValueError, "rejected_evidence"),
                ):
                    case.save(case.result([]))
                self.assertEqual(case.counts(), (0, 0, 0, 0, 0, 0, 0))

    def test_admitted_off_trigger_support_uses_current_request_boundary(
        self,
    ) -> None:
        saved = self.admit_current_knowledge()
        with (
            mock.patch.object(
                review_originals, "_jsonl", wraps=review_originals._jsonl
            ) as prefix,
            mock.patch.object(
                review_evidence, "reread", wraps=review_evidence.reread
            ) as selected,
            mock.patch.object(
                review_commit, "request_view", wraps=review_commit.request_view
            ) as views,
        ):
            receipt = self.save(self.result([self.replacement(saved)]))
        self.assertEqual(receipt.applied[0].revision, 2)
        self.assertEqual(prefix.call_count, 1)
        self.assertEqual(prefix.call_args.args[1:3], (3, 3))
        budget = prefix.call_args.kwargs["budget"]
        self.assertTrue(
            all(c.kwargs["budget"] is budget for c in selected.call_args_list)
        )
        self.assertEqual(selected.call_count, 2)
        self.assertTrue(
            all(
                c.args[0]["id"] == self.context.request_id
                for c in views.call_args_list
            )
        )
        self.assertEqual(self.counts(), (1, 2, 2, 2, 0, 1, 2))

    def test_unrelated_request_does_not_admit_proposed_support(self) -> None:
        saved = self.save(self.result([self.note()])).applied[0]
        self.context = self.start(3, 3)
        before = self.counts()
        with self.assertRaisesRegex(ValueError, "rejected_evidence"):
            self.save(self.result([self.replacement(saved)]))
        self.assertEqual(self.counts(), before)
        self.assertEqual(
            self.row("SELECT current_revision FROM knowledge")[0], 1
        )

    def test_off_trigger_input_is_revalidated_even_when_uncited(self) -> None:
        for fault in ("scope", "projection", "authority", "erasure", "hash"):
            with self.subTest(fault=fault):
                case = ReviewCommitAdmissionTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                case.admit_current_knowledge()
                ident = case.evidence[0].id
                if fault == "erasure":
                    case.conn.execute(
                        "INSERT INTO tombstone(provider,level,thread_id,line,"
                        "line_sha256,created_at) VALUES(?,?,?,?,?,14.0)",
                        (
                            "codex",
                            "line",
                            case.evidence[0].thread_id,
                            2,
                            case.evidence[0].line_sha256,
                        ),
                    )
                elif fault == "hash":
                    case.path.write_bytes(
                        case.path.read_bytes().replace(b"Python", b"Golang")
                    )
                else:
                    column, value = {
                        "scope": (
                            "scope_json",
                            '{"project_id":"other","task":null}',
                        ),
                        "projection": ("projection_version", "obsolete"),
                        "authority": ("authority", "reported"),
                    }[fault]
                    case.conn.execute(
                        f"UPDATE review_evidence SET {column}=? WHERE id=?",
                        (value, ident),
                    )
                before = case.counts()
                expected = (
                    "^erasure_pending$"
                    if fault == "erasure"
                    else "rejected_erased|rejected_evidence"
                )
                with self.assertRaisesRegex(ValueError, expected):
                    case.save(case.result([]))
                self.assertEqual(case.counts(), before)

    def test_stale_current_revision_refuses_entire_batch(self) -> None:
        saved = self.save(self.result([self.note()])).applied[0]
        self.context = self.start(2, 2)
        self.save(self.result([self.replacement(saved)]))
        self.context = self.start(3, 3)
        stale = self.replacement(saved)
        stale["evidence_refs"] = [self.evidence[1].id]
        before = self.counts()
        with self.assertRaisesRegex(ValueError, "rejected_revision"):
            self.save(
                self.result(
                    [self.note(evidence_refs=[self.evidence[1].id]), stale]
                )
            )
        self.assertEqual(self.counts(), before)
        self.assertEqual(
            self.row("SELECT current_revision FROM knowledge")[0], 2
        )

    def test_supported_correction_of_withheld_current_revision(self) -> None:
        saved = self.save(self.result([self.note()])).applied[0]
        self.context = self.start(3, 3)
        proposal = {
            "claim": "Applicability uncertain",
            "affected": [saved.model_dump()],
            "evidence_refs": [],
            "question": "Which missing evidence establishes applicability?",
        }
        self.save(self.result([], [proposal]))
        self.assertEqual(
            self.row("SELECT state FROM knowledge")[0], "withheld"
        )
        self.context = self.start(2, 2)
        receipt = self.save(self.result([self.replacement(saved)]))
        self.assertEqual(receipt.applied[0].revision, 2)
        self.assertEqual(self.row("SELECT state FROM knowledge")[0], "active")
        self.assertEqual(
            self.row("SELECT count(*) FROM knowledge_version")[0], 2
        )
        self.assertEqual(self.row("SELECT revision FROM knowledge_fts")[0], 2)
