"""Check truthful managed projections and exact persistent JSON history."""

from __future__ import annotations

import sqlite3

from muninn import review_budget, review_estimate, store, store_copy
from muninn import review_runtime_records as records
from muninn.review_json import canonical_json
from muninn.review_runtime_models import (
    ActivationEvent,
    ManagedCallRecord,
    RunnerConfig,
)
from tests.managed_codex_support import identity
from tests.test_review_managed_records import ManagedRecordTests


class ManagedPersistenceTests(ManagedRecordTests):
    """Keep unknown runtime estimates honest and preserve opaque call bytes."""

    def test_estimate_requires_checked_current_identity_and_actual_launch(
        self,
    ) -> None:
        context = self.context()
        observed = identity(self.selected.manager_root)
        self.transaction(
            lambda: review_budget.reserve_call(
                self.conn, context, 13.0, execution=observed
            )
        )
        record = ManagedCallRecord(
            record="codex-managed-call-v1",
            selection=self.selected,
            candidates=(observed,),
            launched_identity=records.identity_digest(observed),
            events=(
                ActivationEvent(
                    kind="offer",
                    identity_digest=observed.digest,
                    code=None,
                    at=13.0,
                ),
            ),
        )
        self.conn.execute(
            "UPDATE review_call SET requested_json=?,state='completed',launch"
            "ed_at=14.0,ended_at=16.0",
            (canonical_json(record),),
        )
        unknown = review_estimate.estimate(self.conn, self.policy, 1)
        self.assertEqual(unknown["sample_calls"], 0)
        self.assertEqual(unknown["runtime_status"], "unknown")
        known = review_estimate.estimate(
            self.conn, self.policy, 1, execution=observed
        )
        self.assertEqual(known["sample_calls"], 1)
        self.assertEqual(known["seconds_low"], 2.0)
        other = identity(self.selected.manager_root, "0.167.0")
        self.assertEqual(
            review_estimate.estimate(
                self.conn, self.policy, 1, execution=other
            )["sample_calls"],
            0,
        )
        self.conn.execute(
            "UPDATE review_call SET requested_json=?",
            (
                canonical_json(
                    record.model_copy(update={"launched_identity": None})
                ),
            ),
        )
        self.assertEqual(
            review_estimate.estimate(
                self.conn, self.policy, 1, execution=observed
            )["sample_calls"],
            0,
        )

    def test_legacy_reader_refuses_new_managed_authority(self) -> None:
        with self.assertRaises(ValueError):
            RunnerConfig.model_validate_json(canonical_json(self.selected))

    def test_current_copy_preserves_managed_json_and_full_row_fingerprint(
        self,
    ) -> None:
        context = self.context()
        observed = identity(self.selected.manager_root)
        self.transaction(
            lambda: review_budget.reserve_call(
                self.conn, context, 13.0, execution=observed
            )
        )
        target = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(target.close)
        store.initialize_store(target)
        before = tuple(
            self.conn.execute(
                "SELECT requested_json FROM review_call"
            ).fetchone()
        )
        fingerprint = store_copy._digest(self.conn, "review_call")
        result = store_copy.copy_governed_state(self.conn, target)
        self.assertEqual(result["review_call"], 1)
        self.assertEqual(
            tuple(
                target.execute(
                    "SELECT requested_json FROM review_call"
                ).fetchone()
            ),
            before,
        )
        self.assertEqual(
            store_copy._digest(target, "review_call"), fingerprint
        )
        self.assertEqual(
            target.execute(
                "SELECT policy_json FROM review_control"
            ).fetchone()[0],
            canonical_json(self.policy),
        )
        self.assertEqual(
            target.execute(
                "SELECT requested_json FROM review_attempt"
            ).fetchone()[0],
            canonical_json(self.selected),
        )
        self.assertEqual(store_copy.validate_source(target), 2)
        target.execute(
            "UPDATE review_call SET requested_json=?",
            (
                canonical_json(
                    ManagedCallRecord(
                        record="codex-managed-call-v1",
                        selection=self.selected,
                        candidates=(
                            identity(self.selected.manager_root, "0.167.0"),
                        ),
                    )
                ),
            ),
        )
        self.assertNotEqual(
            store_copy._digest(target, "review_call"), fingerprint
        )
