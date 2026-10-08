"""Erase and the typed ledger fields: tags, expiry, links, loop scope.

Synthetic provider trees and a temp MUNINN_HOME only.
"""

from __future__ import annotations

import json
import time

from muninn import erase_collect, erase_residue, knowledge, query, store
from muninn.knowledge_typed import loop_scope_id
from tests.erase_support import EraseCase
from tests.test_ingest import TID, primary, rollout

TAGS = "zq9-secretproject,zq9-other"
TAG = "zq9-secretproject"


class TypedEraseTests(EraseCase):
    """An erased entry keeps no value field and no link text."""

    def setUp(self) -> None:
        super().setUp()
        self.write(rollout(), primary())
        self.write(rollout("thr-keep"), primary("thr-keep"))
        self.run_ingest()
        self.k1 = self.knowledge("zq9 plan text", [(TID, 2, 1, "q one")])
        project = self.count(
            "SELECT scope_id FROM knowledge WHERE id = ?", self.k1
        )
        loop = loop_scope_id(self.conn, "run-7", create=True)
        self.assertNotEqual(project, loop)
        self.conn.execute(
            "UPDATE knowledge SET tags = ?, confidence = 'observed',"
            " valid_until = ?, sensitivity = 'restricted', scope_id = ?"
            " WHERE id = ?",
            (TAGS, time.time() + 9999, loop, self.k1),
        )
        self.k2 = self.knowledge("other entry", [("thr-keep", 2, 1, "q one")])
        self.conn.execute(
            "UPDATE knowledge SET contradicts = ?, scope_id = ? WHERE id = ?",
            (self.k1, project, self.k2),
        )

    def everything(self) -> str:
        """Render every read path an erased entry could still surface in."""
        out: list[object] = []
        for status in ("erased", "all", "expired", "current"):
            out.append(
                knowledge.list_entries(
                    self.conn, cwd="/x", status=status, all_projects=True
                )
            )
            out.append(
                knowledge.list_entries(
                    self.conn, cwd="/x", status=status, loop="run-7"
                )
            )
        out.append(knowledge.show(self.conn, self.k1))
        out.append(
            query.search(self.conn, "zq9", cwd="/x", env={}, all_projects=True)
        )
        out.append(knowledge.block_entries(self.conn, [1, 2, 3]))
        return json.dumps(out)

    def test_session_erase_clears_every_typed_field(self) -> None:
        self.assertIn(TAG, self.everything())
        out = self.erase(session=TID)
        self.assertEqual((out["knowledge"], out["residue"]), (1, 0))
        row = self.conn.execute(
            "SELECT status, text, tags, confidence, valid_until,"
            " sensitivity, contradicts FROM knowledge WHERE id = ?",
            (self.k1,),
        ).fetchone()
        self.assertEqual(
            tuple(row),
            ("erased", None, None, None, None, "normal", None),
        )
        self.assertNotIn("zq9", self.everything())
        raw = store.db_path(self.home).read_bytes()
        self.assertNotIn(TAG.encode(), raw)

    def test_erasing_the_contradicted_entry_leaves_the_other_intact(
        self,
    ) -> None:
        self.erase(session=TID)
        shown = knowledge.show(self.conn, self.k2)
        self.assertEqual(shown["entry"]["contradicts"], f"K{self.k1}")
        self.assertEqual(shown["entry"]["text"], "other entry")
        self.assertNotIn("zq9", json.dumps(shown))

    def test_erasing_the_contradicting_entry_drops_its_link(self) -> None:
        self.conn.execute(
            "UPDATE knowledge SET tags = 'zq9-linker-tag' WHERE id = ?",
            (self.k2,),
        )
        out = self.erase(session="thr-keep")
        self.assertEqual(out["knowledge"], 1)
        shown = knowledge.show(self.conn, self.k2)
        self.assertIsNone(shown["entry"]["contradicts"])
        self.assertEqual(shown["entry"]["tags"], [])
        self.assertIn(TAG, knowledge.show(self.conn, self.k1)["entry"]["tags"])

    def test_match_on_a_tag_erases_the_entry(self) -> None:
        out = self.erase(match=TAG)
        self.assertEqual((out["knowledge"], out["residue"]), (1, 0))
        self.assertNotIn("zq9", self.everything())

    def test_loop_scope_row_survives_and_names_no_content(self) -> None:
        self.erase(session=TID)
        row = self.conn.execute(
            "SELECT key, kind FROM scope WHERE key = 'loop:run-7'"
        ).fetchone()
        self.assertEqual(tuple(row), ("loop:run-7", "dir"))
        listed = knowledge.list_entries(
            self.conn, cwd="/x", status="all", loop="run-7"
        )
        self.assertEqual(
            [(e["status"], e["text"], e["scope"]) for e in listed["entries"]],
            [("erased", None, "loop:run-7")],
        )

    def test_residue_scan_reads_tags(self) -> None:
        target = erase_collect.Target()
        target.knowledge.add(self.k1)
        needles = erase_residue.pick_needles(self.conn, target)
        self.assertIn(TAGS.encode(), needles)
        self.conn.execute(
            "UPDATE knowledge SET tags = ? WHERE id = ?", (TAGS, self.k2)
        )
        self.assertTrue(erase_residue.still_stored(self.conn, TAGS.encode()))
