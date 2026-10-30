"""Qualify actual process ordering at the shared writer root."""

from __future__ import annotations

import multiprocessing
import sys
from contextlib import closing
from multiprocessing.process import BaseProcess
from pathlib import Path
from unittest import mock

from muninn import (
    classify,
    decision_requests,
    review_commit,
    review_evidence,
    store,
)
from muninn.decision_apply import enrich_support
from muninn.decision_candidates import candidate_view
from muninn.decision_models import DecisionIntent
from muninn.decision_proof import verify_decision
from muninn.decision_write import accept_decision, submit_intent
from muninn.review_commit import CommitContext
from muninn.review_evidence_models import EvidenceLocator, EvidenceSelector
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_read_budget import ReadBudget
from tests.ingest_support import line
from tests.test_classify import user_msg
from tests.test_decision_resolution import ResolutionCase
from tests.test_review_commit import LostResponseConnection

if sys.platform == "win32":
    from multiprocessing.connection import PipeConnection as Connection
else:
    from multiprocessing.connection import Connection


def await_writer_start(home: Path, channel: Connection) -> None:
    """Prove native contention before waiting for the parent's release."""
    command = channel.recv()
    if command not in ("go", "contend"):
        raise ValueError("unexpected writer command")
    channel.send("attempting")
    if command == "contend":
        try:
            with store.writer_lock(home, wait_s=0):
                raise AssertionError("writer did not contend")
        except store.BusyError:
            channel.send("contended")
        if channel.recv() != "retry":
            raise ValueError("unexpected writer command")


def race_writer(
    home: str,
    action: str,
    context_json: str,
    intent_json: str,
    channel: Connection,
) -> None:
    """Wait at process barriers before one guarded transaction."""
    context = CommitContext.model_validate_json(context_json)
    intent = DecisionIntent.model_validate_json(intent_json)
    channel.send("ready")
    await_writer_start(Path(home), channel)
    with (
        store.writer_lock(Path(home), wait_s=0),
        closing(store.connect_rw(store.db_path(Path(home)))) as conn,
    ):
        channel.send("locked")
        if channel.recv() != "finish":
            raise ValueError("unexpected writer command")
        error = None
        try:
            if action == "apply":
                candidate = candidate_view(conn, intent.candidate_id).candidate
                assert candidate is not None
                owner = conn.execute(
                    "SELECT owner_evidence_id FROM memory_decision "
                    "WHERE candidate_id=? AND accepted_revision=1",
                    (intent.candidate_id,),
                ).fetchone()[0]
                review_commit.commit_review(
                    conn, context, enrich_support(candidate, owner)
                )
            else:
                conn.execute("BEGIN IMMEDIATE")
                view = submit_intent(conn, intent, 24.0)
                if view.state != "saved":
                    policy = ReviewPolicy.model_validate_json(
                        conn.execute(
                            "SELECT policy_json FROM review_control"
                        ).fetchone()[0]
                    )
                    proof = verify_decision(
                        conn,
                        Path(home),
                        intent,
                        policy,
                        ReadBudget(policy.max_input_bytes),
                    )
                    accept_decision(conn, proof, 25.0, policy=policy)
                conn.commit()
        except ValueError as exc:
            conn.rollback()
            error = str(exc)
        view = candidate_view(conn, intent.candidate_id)
        result = (
            view.state,
            view.receipt.request_id if view.receipt else None,
            error,
        )
    channel.send(("released", result))
    channel.close()


class DecisionRaceTests(ResolutionCase):
    """Require all four outputs or none in both actual writer orders."""

    def receive(self, channel: Connection) -> object:
        """Bound each barrier so a broken lock cannot hang the test runner."""
        self.assertTrue(channel.poll(5), "writer barrier timed out")
        return channel.recv()

    def receive_released(self, channel: Connection) -> object:
        """Require resource release before returning a worker's result."""
        message = self.receive(channel)
        if not isinstance(message, tuple) or len(message) != 2:
            self.fail("writer did not confirm release")
        self.assertEqual(message[0], "released")
        return message[1]

    def ordered_race(self, action: str, first: str) -> None:
        """Run competing apply and negative transactions in a forced order."""
        question, refs = self.question()
        draft = self.disposition(question, refs)
        replacement = self.stage()
        ident, context = self.application(draft)
        intent = self.intent(
            ident,
            action=action,
            replacement_id=replacement.id if action == "edit" else None,
        )
        self.transaction(lambda: submit_intent(self.conn, intent, 23.5))
        approved = tuple(
            self.row(
                "SELECT * FROM memory_decision WHERE candidate_id=? "
                "AND accepted_revision=1",
                (ident,),
            )
        )
        before = self.governed()
        process_context = multiprocessing.get_context("spawn")
        children: dict[str, tuple[BaseProcess, Connection]] = {}
        for operation in ("apply", "negative"):
            parent, child = process_context.Pipe()
            process = process_context.Process(
                target=race_writer,
                args=(
                    str(self.home),
                    operation,
                    canonical_json(context),
                    canonical_json(intent),
                    child,
                ),
            )
            process.start()
            child.close()
            self.addCleanup(parent.close)
            self.addCleanup(self.stop_process, process)
            children[operation] = process, parent
            self.assertEqual(self.receive(parent), "ready")
        second = "negative" if first == "apply" else "apply"
        first_pipe, second_pipe = children[first][1], children[second][1]
        first_pipe.send("go")
        self.assertEqual(self.receive(first_pipe), "attempting")
        self.assertEqual(self.receive(first_pipe), "locked")
        second_pipe.send("contend")
        self.assertEqual(self.receive(second_pipe), "attempting")
        self.assertEqual(self.receive(second_pipe), "contended")
        first_pipe.send("finish")
        first_result = self.receive_released(first_pipe)
        second_pipe.send("retry")
        self.assertEqual(self.receive(second_pipe), "locked")
        second_pipe.send("finish")
        second_result = self.receive_released(second_pipe)
        for process, _ in children.values():
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(
            tuple(
                self.row(
                    "SELECT * FROM memory_decision WHERE candidate_id=? "
                    "AND accepted_revision=1",
                    (ident,),
                )
            ),
            approved,
        )
        self.assert_race_outputs(
            ident,
            replacement.id,
            question,
            action,
            (first, first_result, second_result),
            context,
            before,
        )

    def assert_race_outputs(
        self,
        ident: str,
        replacement: str,
        question: str,
        action: str,
        results: tuple[str, object, object],
        context: CommitContext,
        before: dict[str, list[tuple[object, ...]]],
    ) -> None:
        """Assert terminal race outcomes independently of process timing."""
        first, first_result, second_result = results
        view = candidate_view(self.conn, ident)
        if first == "negative":
            self.assertEqual(
                view.state, "rejected" if action == "reject" else "superseded"
            )
            self.assertIsNone(view.receipt)
            for table in (
                "knowledge",
                "knowledge_version",
                "knowledge_claim",
                "knowledge_support",
                "knowledge_fts",
                "review_receipt",
                "unresolved",
            ):
                self.assertEqual(self.governed()[table], before[table])
            self.assertEqual(
                second_result, (view.state, None, "rejected_fenced")
            )
        else:
            self.assertEqual(view.state, "saved")
            assert view.receipt is not None
            self.assertEqual(first_result, second_result)
            self.assertEqual(view.receipt.request_id, context.request_id)
            self.assertEqual(
                self.row(
                    "SELECT state FROM unresolved WHERE id=?", (question,)
                )[0],
                "resolved",
            )
            self.assertEqual(self.row("SELECT count(*) FROM knowledge")[0], 3)
            self.assertEqual(
                tuple(
                    self.row(
                        "SELECT state,accepted_revision,owner_evidence_id "
                        "FROM memory_decision "
                        "WHERE candidate_id=? AND action=?",
                        (ident, action),
                    )
                ),
                ("blocked", None, None),
            )
            self.assertEqual(view.revision, 1)
        self.assertEqual(
            candidate_view(self.conn, replacement).state,
            "waiting_verification",
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM review_request WHERE candidate_id=?",
                (ident,),
            )[0],
            1,
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_call")[0], 0)

    def stop_process(self, process: BaseProcess) -> None:
        """Contain a failed barrier without leaving a writer behind."""
        if process.is_alive():
            process.terminate()
        process.join(5)

    def test_both_decision_application_race_orders_reject_first(self) -> None:
        self.ordered_race("reject", "negative")

    def test_both_decision_application_race_orders_supersession_first(
        self,
    ) -> None:
        self.ordered_race("edit", "negative")

    def test_both_decision_application_race_orders_apply_before_reject(
        self,
    ) -> None:
        self.ordered_race("reject", "apply")

    def test_both_decision_application_race_orders_apply_before_edit(
        self,
    ) -> None:
        self.ordered_race("edit", "apply")

    def test_lost_response_replays_one_complete_result(self) -> None:
        question, refs = self.question()
        ident, context = self.application(self.disposition(question, refs))
        candidate = candidate_view(self.conn, ident).candidate
        assert candidate is not None
        with (
            store.writer_lock(self.home, wait_s=0),
            closing(
                LostResponseConnection(
                    store.db_path(self.home), isolation_level=None
                )
            ) as conn,
        ):
            conn.row_factory = self.conn.row_factory
            with self.assertRaises(InterruptedError):
                review_commit.commit_review(
                    conn,
                    context,
                    enrich_support(candidate, self.proof.owner_evidence_id),
                )
        before = self.governed()
        with mock.patch(
            "muninn.review_commit.ReadBudget", side_effect=AssertionError
        ):
            replay = review_commit.commit_review(
                self.conn, context, self.result([])
            )
        self.assertEqual(candidate_view(self.conn, ident).receipt, replay)
        self.assertEqual(self.governed(), before)
        self.assertEqual(
            self.row("SELECT state FROM unresolved WHERE id=?", (question,))[
                0
            ],
            "resolved",
        )

    def test_unverified_negative_does_not_suspend_active_approval(
        self,
    ) -> None:
        question, refs = self.question()
        draft = self.disposition(question, refs)
        self.approve(self.intent(draft.id))
        negative = self.intent(
            draft.id, action="reject", wording="unverified rejection"
        )
        self.transaction(lambda: submit_intent(self.conn, negative, 23.0))
        view = decision_requests.request_application(self.home, draft.id, 25.0)
        self.assertEqual(view.state, "saved")
        self.assertIsNotNone(view.receipt)
        self.assertEqual(
            self.row("SELECT state FROM unresolved WHERE id=?", (question,))[
                0
            ],
            "resolved",
        )
        self.assertEqual(
            tuple(
                self.row(
                    "SELECT state,accepted_revision FROM memory_decision "
                    "WHERE candidate_id=? AND action='reject'",
                    (draft.id,),
                )
            ),
            ("blocked", None),
        )

    def test_multiple_support_selections_reuse_one_actual_original(
        self,
    ) -> None:
        self.records[1] = user_msg(1, "Use Python. Please test.")
        self.path.write_bytes(
            b"".join(line(record) for record in self.records)
        )
        self.run_ingest(full=True)
        selector = EvidenceSelector(
            json_pointer="/payload/content/0/text", start=None, end=None
        )
        records = tuple(
            review_evidence.select_evidence(
                self.home,
                EvidenceLocator(
                    source=self.window().source,
                    line=2,
                    part=1,
                    line_sha256=classify.record_hash(line(self.records[1])),
                ),
                selector.model_copy(update={"start": start, "end": end}),
            )
            for start, end in ((0, 11), (12, 24))
        )
        for record in records:
            self.persist(record)
            self.conn.execute(
                "INSERT INTO review_input VALUES(?,?)",
                (self.context.request_id, record.id),
            )
        draft = self.stage(
            self.result(
                [self.note(evidence_refs=[record.id for record in records])]
            )
        )
        ident, context = self.application(draft)
        candidate = candidate_view(self.conn, ident).candidate
        assert candidate is not None
        with mock.patch.object(
            review_evidence, "reread", wraps=review_evidence.reread
        ) as reads:
            receipt = review_commit.commit_review(
                self.conn,
                context,
                enrich_support(candidate, self.proof.owner_evidence_id),
            )
        self.assertEqual(len(receipt.applied), 1)
        self.assertEqual(reads.call_count, 3)
        self.assertEqual(
            self.row("SELECT count(*) FROM knowledge_support")[0], 3
        )
