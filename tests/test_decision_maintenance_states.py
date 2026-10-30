"""Preserve every actual decision state and erased proof across maintenance."""

from __future__ import annotations

import json
from contextlib import closing

from muninn import cli_rebuild, erase, review_requests, store, store_copy
from muninn.decision_candidates import candidate_view, show_candidate
from muninn.decision_write import submit_intent
from tests.decision_erasure_graph_support import DecisionGraphCase
from tests.ingest_support import TID
from tests.knowledge_cutover_support import digest
from tests.review_pause_support import initialize


class DecisionStateMaintenanceTests(DecisionGraphCase):
    """Use real staging, proof, normal application and public erasure."""

    def idle_preparation(self) -> None:
        """Release an ordinary preparation attempt through its normal API."""
        row = self.row(
            "SELECT state FROM review_request WHERE id=?",
            (self.context.request_id,),
        )
        if row[0] == "running":
            self.transaction(
                lambda: review_requests.cancel_request(
                    self.conn, self.context.request_id, 30.0
                )
            )

    def catalog(self) -> dict[str, str]:
        """Construct the required states through their real lifecycle paths."""
        self.presentation_limit()
        result: dict[str, str] = {}
        pending_erasures: list[int] = []
        for state in (
            "waiting_verification",
            "approved_pending",
            "saved",
            "rejected",
            "superseded",
            "erased_original",
            "erased_owner",
            "erased_display",
        ):
            self.idle_preparation()
            support = self.fresh_request(
                "A distinct ordinary source for " + state
            )
            draft = self.stage(
                self.result(
                    [
                        self.note(
                            content="Draft for " + state,
                            evidence_refs=[support],
                        )
                    ]
                )
            )
            result[state] = draft.id
            if state == "waiting_verification":
                intent = self.intent(draft.id)
                self.transaction(
                    lambda intent=intent: submit_intent(
                        self.conn, intent, 20.0
                    )
                )
            elif state == "rejected":
                self.approve(self.intent(draft.id))
                self.approve(self.intent(draft.id, action="reject"))
            elif state == "superseded":
                replacement = self.stage(
                    self.result(
                        [
                            self.note(
                                content="A real edit replacement",
                                evidence_refs=[support],
                            )
                        ]
                    )
                )
                result["replacement"] = replacement.id
                self.approve(self.intent(draft.id))
                self.approve(
                    self.intent(
                        draft.id, action="edit", replacement_id=replacement.id
                    )
                )
            elif state == "approved_pending":
                self.approve(self.intent(draft.id))
            else:
                ident, context = self.application(draft)
                receipt = self.save_application(ident, context)
                self.assertEqual(
                    candidate_view(self.conn, ident).receipt, receipt
                )
                if state.startswith("erased_"):
                    evidence = {
                        "erased_original": support,
                        "erased_owner": self.proof.owner_evidence_id,
                        "erased_display": self.proof.display_evidence_id,
                    }[state]
                    coordinate = self.row(
                        "SELECT line FROM review_evidence WHERE id=?",
                        (evidence,),
                    )[0]
                    pending_erasures.append(coordinate)
        self.idle_preparation()
        for coordinate in pending_erasures:
            erase.erase(
                self.conn,
                home=self.home,
                event_ref=f"codex:{TID}:{coordinate}.1",
                env={"HOME": str(self.tmp)},
            )
        return result

    def test_all_states_proof_roles_and_history_survive_copy_and_rebuild(
        self,
    ) -> None:
        catalog = self.catalog()
        expected = {
            name: candidate_view(self.conn, ident)
            for name, ident in catalog.items()
        }
        self.assertEqual(
            {view.state for view in expected.values()},
            {
                "waiting_verification",
                "approved_pending",
                "saved",
                "rejected",
                "superseded",
                "erased",
            },
        )
        tables = tuple(
            name
            for name in store_copy.GOVERNED_TABLES
            if name != "review_control"
        )
        before = digest(self.conn, tables)
        displays = {
            ident: show_candidate(
                self.conn, ident, self.policy.context_bytes
            ).text
            for ident in catalog.values()
            if candidate_view(self.conn, ident).candidate is not None
        }
        with closing(
            store.connect_rw(self.home / "state-copy.sqlite")
        ) as target:
            store_copy.copy_governed_state(self.conn, target)
            target.execute("VACUUM")
            self.assertEqual(digest(target, tables), before)
            for name, ident in catalog.items():
                self.assertEqual(candidate_view(target, ident), expected[name])
                if ident in displays:
                    self.assertEqual(
                        show_candidate(
                            target, ident, self.policy.context_bytes
                        ).text,
                        displays[ident],
                    )
        originals = {
            path: path.read_bytes()
            for root in self.roots.values()
            for path in root.rglob("*.jsonl")
        }
        self.conn.close()
        initialize(self.home)
        output = cli_rebuild._run(
            {
                "HOME": str(self.tmp),
                "MUNINN_ROOTS": json.dumps(
                    {name: str(path) for name, path in self.roots.items()}
                ),
            },
            self.home,
            cli_rebuild._Status(),
        )
        self.assertTrue(output["rebuilt"])
        with closing(store.connect_ro(store.db_path(self.home))) as current:
            self.assertEqual(digest(current, tables), before)
            for name, ident in catalog.items():
                self.assertEqual(
                    candidate_view(current, ident), expected[name]
                )
                if ident in displays:
                    self.assertEqual(
                        show_candidate(
                            current, ident, self.policy.context_bytes
                        ).text,
                        displays[ident],
                    )
        self.assertEqual(
            {path: path.read_bytes() for path in originals}, originals
        )
