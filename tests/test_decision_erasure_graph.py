"""Qualify real disposition and multi-hop erasure through public selections."""

from __future__ import annotations

import json
from contextlib import closing

from install import snapshot
from install.context import Ctx, StepFailedError
from muninn import (
    cli_rebuild,
    decision_erase,
    erase,
    knowledge_erase_links,
    query,
    review_commit,
    store,
    store_copy,
    tombstones,
)
from muninn.decision_candidates import candidate_view, show_candidate
from muninn.decision_proof import verify_decision
from muninn.decision_requests import request_application
from muninn.knowledge_erase import support_closure
from muninn.knowledge_erase_state import SupportClosure
from muninn.review_read_budget import ReadBudget
from tests.decision_erasure_graph_support import (
    ANSWER,
    DecisionGraph,
    DecisionGraphCase,
)
from tests.ingest_support import TID
from tests.policy_preview_support import no_command
from tests.review_pause_support import initialize


class DecisionErasureGraphTests(DecisionGraphCase):
    """Check actual identities and bodies across the full erasure graph."""

    def assert_graph_erased(self, graph: DecisionGraph) -> None:
        """Check affected bodies and history with independent SQL reads."""
        for ident in graph.candidates:
            view = candidate_view(self.conn, ident)
            self.assertEqual((view.state, view.candidate), ("erased", None))
            with self.assertRaisesRegex(ValueError, "decision_erased"):
                show_candidate(self.conn, ident, self.policy.context_bytes)
            for row in self.conn.execute(
                "SELECT "
                "state,locator_json,owner_evidence_id,display_evidence_id "
                "FROM memory_decision WHERE candidate_id=?",
                (ident,),
            ):
                self.assertEqual(tuple(row), ("erased", None, None, None))
        for ident in graph.questions:
            self.assertEqual(
                self.row(
                    "SELECT state,item_json FROM unresolved WHERE id=?",
                    (ident,),
                )[:],
                ("erased", None),
            )
        for version in graph.versions:
            self.assertEqual(
                self.row(
                    "SELECT content_json,reason,expiry_json FROM "
                    "knowledge_version "
                    "WHERE knowledge_id=? AND revision=?",
                    (version.knowledge_id, version.revision),
                )[:],
                (None, None, None),
            )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_request WHERE candidate_id "
                "IN (?,?,?) "
                "AND (state!='cancelled' OR current_attempt_id IS NOT NULL)",
                graph.candidates,
            )[0],
            0,
        )
        for evidence in graph.proof_ids:
            self.assertEqual(
                self.row(
                    "SELECT excerpt,state FROM review_evidence WHERE id=?",
                    (evidence,),
                )[:],
                (None, "erased"),
            )
            source = self.row(
                "SELECT provider,thread_id,line,part FROM "
                "review_evidence WHERE id=?",
                (evidence,),
            )
            reference = f"{source[0]}:{source[1]}:{source[2]}.{source[3]}"
            self.assertEqual(
                query.open_event(
                    self.conn, reference, roots=self.roots, context=0
                ).get("error"),
                "not_found",
            )
        assert self.scope.project_id is not None
        search = query.search(
            self.conn,
            "ERASEDISPOSITIONCHAIN",
            cwd=self.scope.project_id,
            env={},
            include_current=True,
            all_projects=True,
        )
        self.assertEqual(search.get("hits"), [])

    def assert_no_resurrection(self, graph: DecisionGraph) -> None:
        """Refuse late proof and retain only the historical receipt outcome."""
        before = self.projection()
        with self.assertRaises(ValueError):
            self.transaction(
                lambda: verify_decision(
                    self.conn,
                    self.home,
                    graph.pending_intent,
                    self.policy,
                    ReadBudget(1_000_000),
                )
            )
        self.assertEqual(before, self.projection())
        self.assertEqual(
            review_commit.commit_review(
                self.conn, graph.first_context, self.result([])
            ),
            graph.first_receipt,
        )
        self.assertEqual(
            request_application(self.home, graph.candidates[2], 40.0).state,
            "erased",
        )
        self.assert_graph_erased(graph)

    def test_disposition_multihop_all_public_erasure_routes(self) -> None:
        for selection in (
            "session",
            "original",
            "owner",
            "display",
            "text",
            "replay",
        ):
            with self.subTest(selection=selection):
                self.doCleanups()
                self.setUp()
                graph = self.graph()
                independent = candidate_view(
                    self.conn, graph.unrelated_candidate
                )
                question = tuple(
                    self.row(
                        "SELECT * FROM unresolved WHERE id=?",
                        (graph.unrelated_question,),
                    )
                )
                receipts = [
                    tuple(r)
                    for r in self.conn.execute(
                        "SELECT * FROM review_receipt ORDER BY request_id"
                    )
                ]
                history = [
                    tuple(r)
                    for r in self.conn.execute(
                        "SELECT "
                        "id,candidate_digest,action,"
                        "accepted_revision,accepted_at "
                        "FROM memory_decision ORDER BY id"
                    )
                ]
                originals = {
                    path: path.read_bytes()
                    for root in self.roots.values()
                    for path in root.rglob("*.jsonl")
                }
                self.select_graph(graph, selection)
                self.assert_graph_erased(graph)
                self.assertEqual(
                    candidate_view(self.conn, graph.unrelated_candidate),
                    independent,
                )
                self.assertEqual(
                    tuple(
                        self.row(
                            "SELECT * FROM unresolved WHERE id=?",
                            (graph.unrelated_question,),
                        )
                    ),
                    question,
                )
                self.assertEqual(
                    [
                        tuple(r)
                        for r in self.conn.execute(
                            "SELECT * FROM review_receipt ORDER BY request_id"
                        )
                    ],
                    receipts,
                )
                self.assertEqual(
                    [
                        tuple(r)
                        for r in self.conn.execute(
                            "SELECT "
                            "id,candidate_digest,action,"
                            "accepted_revision,accepted_at "
                            "FROM memory_decision ORDER BY id"
                        )
                    ],
                    history,
                )
                self.assertEqual(
                    {path: path.read_bytes() for path in originals}, originals
                )
                if selection not in ("session", "text"):
                    self.assert_original_questions_kept(graph)
                self.assert_no_resurrection(graph)

    def test_disposition_only_text_match_before_any_knowledge_exists(
        self,
    ) -> None:
        self.presentation_limit()
        origin = self.save(
            self.result(
                [],
                [
                    {
                        "claim": "Original question",
                        "question": "Which draft should apply?",
                        "affected": [],
                        "evidence_refs": [self.evidence[0].id],
                    }
                ],
            )
        ).unresolved_ids[0]
        support = self.fresh_request(
            "Prepare a draft with a disclosed answer."
        )
        draft = self.draft_answer(
            origin, (), support, ANSWER, content="Plain draft."
        )
        self.assertEqual(self.row("SELECT count(*) FROM knowledge")[0], 0)
        assert draft.candidate is not None
        self.assertNotIn(ANSWER, draft.candidate.result.model_dump_json())
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM event WHERE instr(text,?)>0", (ANSWER,)
            )[0],
            0,
        )
        before = self.projection()
        original = self.path.read_bytes()
        erase.erase(
            self.conn,
            home=self.home,
            match=ANSWER,
            dry_run=True,
            env={"HOME": str(self.tmp)},
        )
        self.assertEqual(self.projection(), before)
        erase.erase(
            self.conn,
            home=self.home,
            match=ANSWER,
            env={"HOME": str(self.tmp)},
        )
        self.assertEqual(candidate_view(self.conn, draft.id).state, "erased")
        self.assertEqual(
            self.row(
                "SELECT state,item_json FROM unresolved WHERE id=?", (origin,)
            )[:],
            ("erased", None),
        )
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.row("SELECT count(*) FROM knowledge")[0], 0)

    def test_real_chain_requires_another_graph_iteration(self) -> None:
        graph = self.graph()
        before = self.projection()
        plan = SupportClosure(
            {graph.first_proof.owner_evidence_id},
            set(),
            set(),
            set(),
            set(),
            set(),
        )
        knowledge_erase_links.collect_links(self.conn, plan)
        decision_erase.collect_links(self.conn, plan)
        self.assertNotIn(graph.candidates[2], plan.candidates)
        complete = support_closure(
            self.conn, (graph.first_proof.owner_evidence_id,)
        )
        self.assertEqual(complete.candidates, set(graph.candidates))
        self.assertNotIn(graph.unrelated_question, complete.unresolved)
        self.assertEqual(self.projection(), before)

    def test_replayed_proof_erasure_survives_copy_rebuild_and_restore_refusal(
        self,
    ) -> None:
        graph = self.graph()
        locator = graph.first_intent.owner_locator
        tombstones.append_tombstones(
            self.home,
            [
                tombstones.tombstone_row(
                    "codex",
                    "line",
                    thread_id=TID,
                    line=locator.line,
                    line_sha256=locator.line_sha256,
                )
            ],
        )
        tombstones.reapply_tombstones(self.conn, self.home)
        self.assert_graph_erased(graph)
        raw = store.db_path(self.home).read_bytes()
        journal = (self.home / tombstones.TOMBSTONE_FILE).read_bytes()
        tombstones.reapply_tombstones(self.conn, self.home)
        self.assertEqual(store.db_path(self.home).read_bytes(), raw)
        self.assertEqual(
            (self.home / tombstones.TOMBSTONE_FILE).read_bytes(), journal
        )
        with closing(store.connect_rw(self.home / "copy.sqlite")) as target:
            store_copy.copy_governed_state(self.conn, target)
            self.assertEqual(
                candidate_view(target, graph.candidates[0]).receipt,
                graph.first_receipt,
            )
            for ident in graph.candidates:
                self.assertIsNone(candidate_view(target, ident).candidate)
        self.conn.close()
        initialize(self.home)
        environment = {
            "HOME": str(self.tmp),
            "MUNINN_ROOTS": json.dumps(
                {name: str(path) for name, path in self.roots.items()}
            ),
        }
        output = cli_rebuild._run(
            environment, self.home, cli_rebuild._Status()
        )
        self.conn = store.connect_rw(store.db_path(self.home))
        self.addCleanup(self.conn.close)
        self.assert_graph_erased(graph)
        self.assertEqual(
            candidate_view(self.conn, graph.candidates[0]).receipt,
            graph.first_receipt,
        )
        self.assertIsNotNone(
            candidate_view(self.conn, graph.unrelated_candidate).candidate
        )
        self.assertIsNotNone(
            self.row(
                "SELECT item_json FROM unresolved WHERE id=?",
                (graph.unrelated_question,),
            )[0]
        )
        ctx = Ctx(self.tmp, no_command, "graph-restore", upgrade=True)
        ctx.data = self.home
        archive = output["old_kept_as"]
        assert isinstance(archive, str)
        with self.assertRaisesRegex(
            StepFailedError, "archive_restore_requires_review"
        ):
            snapshot.undo_store(ctx, {"snapshot": {"name": archive}})
        self.assertTrue((self.home / archive).is_file())
