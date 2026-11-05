"""Check erasure fences managed metadata and the carried old queue."""

from __future__ import annotations

import json

from muninn import knowledge_erase, review_evidence, review_runtime_bridge
from tests.test_review_runtime_bridge import RuntimeBridgeTests


class ManagedErasureTests(RuntimeBridgeTests):
    """Keep runtime facts while preventing erased evidence reuse."""

    def test_erased_added_evidence_cannot_be_restored_by_initial_capsule(
        self,
    ) -> None:
        record = review_evidence.initial_evidence(
            self.home, self.window(), 4096
        ).excerpts[0]
        data = record.model_dump(mode="json")
        for key in ("scope", "selector", "original_locator"):
            data[f"{key}_json"] = json.dumps(data.pop(key))
        self.conn.execute(
            f"INSERT INTO review_evidence({','.join(data)}) "
            f"VALUES({','.join('?' for _ in data)})",
            tuple(data.values()),
        )
        self.conn.execute(
            "INSERT INTO review_input VALUES(?,?)",
            (self.request.id, record.id),
        )
        self.assertEqual(
            review_runtime_bridge.effective_policy(self.conn, self.row()),
            self.new,
        )
        erased = self.transaction(
            lambda: knowledge_erase.erase_review_support(
                self.conn, (record.id,), 20.0
            )
        )
        self.assertEqual(erased.requests, 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT excerpt,state FROM review_evidence WHERE id=?",
                (record.id,),
            ).fetchone()[:],
            (None, "erased"),
        )
        self.assertEqual(self.row()["state"], "cancelled")
        with self.assertRaisesRegex(ValueError, "review_policy_required"):
            review_runtime_bridge.effective_policy(self.conn, self.row())
        self.assertTrue(
            (self.home / "review-capabilities/managed-bridges").is_dir()
        )
