"""Erase normalized value fields and every ordinary retrieval surface.

Synthetic provider trees and a temp MUNINN_HOME only.
"""

from __future__ import annotations

import json
import time

from muninn import (
    erase_collect,
    erase_residue,
    knowledge_push,
    knowledge_read,
    query,
    store,
)
from muninn.knowledge_models import Scope
from tests.erase_support import EraseCase
from tests.test_ingest import TID, primary, rollout

MARKER = "zq9-secretproject,zq9-other"


class TypedEraseTests(EraseCase):
    """Erased revisions retain identities without text or explanation."""

    def setUp(self) -> None:
        super().setUp()
        self.write(rollout(), primary())
        self.write(rollout("thr-keep"), primary("thr-keep"))
        self.run_ingest()
        self.k1 = self.note(
            "zq9 plan text", TID, conditions=[MARKER], reason=MARKER
        )
        self.k2 = self.note("other entry", "thr-keep")
        row = self.conn.execute(
            "SELECT scope_json FROM knowledge_version WHERE knowledge_id=?",
            (self.k1,),
        ).fetchone()
        self.scope = Scope.model_validate_json(row[0])

    def everything(self) -> str:
        """Render normalized list, show, search and bounded context paths."""
        now = time.time()
        out: list[object] = [
            [
                view.model_dump(mode="json")
                for view in knowledge_read.current_knowledge(
                    self.conn, self.scope, now
                )
            ],
            knowledge_read.show_knowledge(self.conn, self.k1, None).model_dump(
                mode="json"
            ),
            [
                view.model_dump(mode="json")
                for view in knowledge_read.search_knowledge(
                    self.conn, "zq9", self.scope
                )
            ],
            query.search(
                self.conn, "zq9", cwd="/x", env={}, all_projects=True
            ),
            knowledge_push.knowledge_context(
                self.conn, self.scope, 10000, now
            ).model_dump(mode="json"),
        ]
        return json.dumps(out)

    def test_session_erase_clears_all_governed_values_and_bytes(self) -> None:
        self.assertIn(MARKER, self.everything())
        out = self.erase(session=TID)
        self.assertEqual((out["knowledge"], out["residue"]), (1, 0))
        row = self.conn.execute(
            "SELECT k.state,k.retracted_reason,v.content_json,v.reason,"
            "v.expiry_json FROM knowledge k JOIN knowledge_version v "
            "ON v.knowledge_id=k.id WHERE k.id=?",
            (self.k1,),
        ).fetchone()
        self.assertEqual(tuple(row), ("erased", None, None, None, None))
        self.assertEqual(
            self.conn.execute(
                "SELECT text,state FROM knowledge_claim WHERE knowledge_id=?",
                (self.k1,),
            ).fetchone()[:],
            (None, "erased"),
        )
        self.assertNotIn("zq9", self.everything())
        self.assertNotIn(
            MARKER.encode(), store.db_path(self.home).read_bytes()
        )
        self.assertEqual(
            erase_residue.residue_scan(self.home, [MARKER.encode()]), []
        )

    def test_erasing_one_source_leaves_independent_knowledge_intact(
        self,
    ) -> None:
        self.erase(session=TID)
        shown = knowledge_read.show_knowledge(self.conn, self.k2, None)
        self.assertEqual(shown.state, "active")
        self.assertIsNotNone(shown.body)
        self.assertIn("other entry", shown.model_dump_json())
        self.assertNotIn("zq9", shown.model_dump_json())

    def test_match_on_governed_conditions_erases_the_revision(self) -> None:
        out = self.erase(match=MARKER)
        self.assertEqual((out["knowledge"], out["residue"]), (1, 0))
        self.assertNotIn("zq9", self.everything())

    def test_residue_scans_normalized_reasons_and_claims(self) -> None:
        target = erase_collect.Target()
        target.knowledge.add(self.k1)
        target.review.versions.add((self.k1, 1))
        needles = erase_residue.pick_needles(self.conn, target)
        self.assertIn(MARKER.encode(), needles)
        self.assertTrue(erase_residue.still_stored(self.conn, MARKER.encode()))
        self.erase(session=TID)
        self.assertFalse(
            erase_residue.still_stored(self.conn, MARKER.encode())
        )
