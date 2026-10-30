"""Build decisions from disposable, physically backed admitted originals."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from copy import deepcopy

from muninn import classify, ingest, review_requests
from muninn.decision_apply import enrich_support
from muninn.decision_candidates import (
    candidate_view,
    show_candidate,
    stage_candidate,
)
from muninn.decision_models import (
    CandidateInput,
    DecisionIntent,
    DecisionView,
    VerifiedDecision,
)
from muninn.decision_proof import verify_decision
from muninn.decision_requests import admit_application
from muninn.decision_write import accept_decision, submit_intent
from muninn.knowledge_models import ReviewResult
from muninn.review_commit import CommitContext
from muninn.review_evidence_models import (
    EvidenceLocator,
    EvidenceSelector,
    SourceRef,
)
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_models import Receipt
from muninn.review_read_budget import ReadBudget
from tests.ingest_support import TID, line, rollout
from tests.test_classify import reply, user_msg
from tests.test_review_commit import CommitCase


class DecisionCase(CommitCase):
    """Reuse the real review preparation without granting owner decisions."""

    def setUp(self) -> None:
        super().setUp()
        self.policy = self.policy.model_copy(
            update={"max_input_bytes": 1_000_000, "context_bytes": 12000}
        )
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 14.0
            )
        )
        self.context = self.start()

    def stage(self, result: ReviewResult | None = None) -> DecisionView:
        """Stage inside a caller transaction and leave its ownership intact."""
        return self.transaction(
            lambda: stage_candidate(
                self.conn,
                self.home,
                CandidateInput(
                    request_id=self.context.request_id,
                    result=result or self.result([self.note()]),
                    disposition=None,
                ),
                14.0,
            )
        )

    def projection(self) -> dict[str, list[tuple[object, ...]]]:
        """Read every table, including FTS shadows, for mutation checks."""
        return {
            row[0]: [
                tuple(item)
                for item in self.conn.execute(f'SELECT * FROM "{row[0]}"')
            ]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }

    def display(self, ident: str) -> str:
        """Read the complete canonical representation shown by the agent."""
        return show_candidate(self.conn, ident, 65536).text

    def intent(
        self,
        ident: str,
        *,
        action: str = "approve",
        replacement_id: str | None = None,
        wording: str | None = None,
        display: str | None = None,
        owner_updates: Mapping[str, object] | None = None,
        display_after: bool = False,
    ) -> DecisionIntent:
        """Index appended synthetic original messages."""
        text = wording or (
            f"Please replace memory {ident} with memory {replacement_id}."
            if action == "edit"
            else f"Please {action} memory {ident}."
        )
        shown = (
            display
            if display is not None
            else self.display(replacement_id or ident)
        )
        owner = user_msg(5, text)
        owner["payload"].update(owner_updates or {})
        presented = reply(4, shown)
        start = len(self.records) + 1
        self.records.extend(
            [owner, presented] if display_after else [presented, owner]
        )
        self.path = self.write(rollout(), self.records)
        self.run_ingest()
        source = SourceRef(provider="codex", thread_id=TID)
        display_line, owner_line = (
            (start + 1, start) if display_after else (start, start + 1)
        )

        def locator(number: int) -> EvidenceLocator:
            return EvidenceLocator(
                source=source,
                line=number,
                part=1,
                line_sha256=classify.record_hash(
                    line(self.records[number - 1])
                ),
            )

        selector = EvidenceSelector(
            json_pointer="/payload/content/0/text", start=None, end=None
        )
        return DecisionIntent.model_validate_json(
            canonical_json_value(
                {
                    "candidate_id": ident,
                    "action": action,
                    "replacement_id": replacement_id,
                    "owner_locator": locator(owner_line).model_dump(
                        mode="json"
                    ),
                    "owner_selector": selector.model_dump(mode="json"),
                    "display_locator": locator(display_line).model_dump(
                        mode="json"
                    ),
                    "display_selector": selector.model_dump(mode="json"),
                }
            )
        )

    def approve(self, intent: DecisionIntent) -> VerifiedDecision:
        """Submit, verify and accept under one real caller transaction."""

        def apply() -> VerifiedDecision:
            submit_intent(self.conn, intent, 16.0)
            proof = verify_decision(
                self.conn,
                self.home,
                intent,
                self.policy,
                ReadBudget(1_000_000),
            )
            accept_decision(self.conn, proof, 18.0, policy=self.policy)
            return proof

        return self.transaction(apply)

    def rewrite(
        self, intent: DecisionIntent, *, display_updates: Mapping[str, object]
    ) -> DecisionIntent:
        """Reindex deliberately changed synthetic presentation provenance."""
        number = intent.display_locator.line
        self.records[number - 1]["payload"].update(display_updates)
        self.write(rollout(), self.records)
        self.run_ingest()
        locator = intent.display_locator.model_copy(
            update={
                "line_sha256": classify.record_hash(
                    line(self.records[number - 1])
                )
            }
        )
        return intent.model_copy(update={"display_locator": locator})

    def saved_fixture(self, ident: str) -> Receipt:
        """Commit through exact application without forcing candidate state."""
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 20.0
            )
        )
        request = self.transaction(
            lambda: admit_application(self.conn, self.home, ident, 1, 21.0)
        )
        attempt = self.transaction(
            lambda: review_requests.claim_attempt(
                self.conn, request.id, 1, 22.0
            )
        )
        ids = tuple(
            row[0]
            for row in self.conn.execute(
                "SELECT evidence_id FROM review_input WHERE request_id=?",
                (request.id,),
            )
        )
        self.context = CommitContext(
            home=self.home,
            request_id=request.id,
            attempt_id=attempt.id,
            generation=1,
            evidence_ids=ids,
            now=23.0,
        )
        view = candidate_view(self.conn, ident)
        assert view.candidate is not None
        owner = self.conn.execute(
            "SELECT owner_evidence_id FROM memory_decision "
            "WHERE candidate_id=? AND accepted_revision=?",
            (ident, view.revision),
        ).fetchone()[0]
        return self.save(enrich_support(view.candidate, owner))

    def fork_intent(
        self, ident: str, *, pending: bool = True
    ) -> DecisionIntent:
        """Create a real fork at the parent-commit interruption boundary."""
        metadata = deepcopy(self.records[0])
        metadata["payload"].update(id="pending-fork")
        self.fork_rows = [
            metadata,
            reply(4, self.display(ident)),
            user_msg(5, f"Please approve memory {ident}."),
        ]
        self.write(rollout("pending-fork"), self.fork_rows)
        self.run_ingest()
        source = SourceRef(provider="codex", thread_id="pending-fork")
        selector = EvidenceSelector(
            json_pointer="/payload/content/0/text", start=None, end=None
        )
        intent = DecisionIntent(
            candidate_id=ident,
            action="approve",
            replacement_id=None,
            owner_locator=EvidenceLocator(
                source=source,
                line=3,
                part=1,
                line_sha256=classify.record_hash(line(self.fork_rows[2])),
            ),
            display_locator=EvidenceLocator(
                source=source,
                line=2,
                part=1,
                line_sha256=classify.record_hash(line(self.fork_rows[1])),
            ),
            owner_selector=selector,
            display_selector=selector,
        )
        if pending:
            self.delay_fork()
        return intent

    def delay_fork(self) -> None:
        """Commit a newly available parent without reprocessing its fork."""
        self.fork_rows[0]["payload"]["forked_from_id"] = "pending-parent"
        self.write(rollout("pending-fork"), self.fork_rows)
        self.run_ingest()
        assert self.need_source("pending-fork")["replay_mode"] == "unverified"
        parent = deepcopy(self.fork_rows)
        parent[0]["payload"]["id"] = "pending-parent"
        parent[0]["payload"].pop("forked_from_id")
        self.write(rollout("pending-parent"), parent)

        def interrupted(source_id: int) -> None:
            row = self.conn.execute(
                "SELECT thread_id FROM source WHERE id=?", (source_id,)
            ).fetchone()
            if row[0] == "pending-parent":
                raise InterruptedError("synthetic post-parent interruption")

        with suppress(InterruptedError):
            ingest.ingest(self.conn, self.roots, on_committed=interrupted)
        assert self.need_source("pending-fork")["replay_mode"] == "unverified"
        assert self.need_source("pending-parent")["status"] == "active"
