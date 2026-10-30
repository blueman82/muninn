"""Preserve changed records when old decision proof is erased."""

from __future__ import annotations

import hashlib
import hmac
import json
from copy import deepcopy

from muninn import erase, query, store, tombstones
from muninn.decision_candidates import candidate_view
from tests.decision_erasure_graph_support import (
    DecisionGraph,
    DecisionGraphCase,
)
from tests.ingest_support import TID, line, rollout

REPLACEMENT = "Independent replacement KEEPCHANGEDPROOFRECORD"


class DecisionReplacementErasureTests(DecisionGraphCase):
    """Use real proof, changed source records and public replay/erase calls."""

    def replace_record(
        self, graph: DecisionGraph, role: str
    ) -> tuple[int, str, str]:
        """Replace one complete synthetic record and reindex its new hash."""
        locator = (
            graph.first_intent.owner_locator
            if role == "owner"
            else graph.first_intent.display_locator
        )
        original = self.records[locator.line - 1]
        self.assertEqual(
            hashlib.sha256(line(original).rstrip(b"\r\n")).hexdigest(),
            locator.line_sha256,
        )
        replacement = deepcopy(original)
        replacement["payload"]["content"][0]["text"] = REPLACEMENT
        self.records[locator.line - 1] = replacement
        expected = hashlib.sha256(
            line(replacement).rstrip(b"\r\n")
        ).hexdigest()
        self.assertNotEqual(expected, locator.line_sha256)
        self.assertNotEqual(
            expected, hashlib.sha256(REPLACEMENT.encode()).hexdigest()
        )
        self.path = self.write(rollout(), self.records)
        self.run_ingest()
        event = self.row(
            "SELECT e.line_sha256,e.role FROM event e JOIN source s "
            "ON s.id=e.source_id WHERE s.thread_id=? AND e.line=?",
            (TID, locator.line),
        )
        self.assertEqual(event[0], expected)
        return locator.line, locator.line_sha256, event[1]

    def assert_old_graph_erased(self, graph: DecisionGraph) -> None:
        """Require old draft, question, knowledge and proof bodies absent."""
        for ident in graph.candidates:
            view = candidate_view(self.conn, ident)
            self.assertEqual((view.state, view.candidate), ("erased", None))
            self.assertEqual(
                self.row(
                    "SELECT count(*) FROM memory_decision "
                    "WHERE candidate_id=? AND (locator_json IS NOT NULL "
                    "OR owner_evidence_id IS NOT NULL "
                    "OR display_evidence_id IS NOT NULL)",
                    (ident,),
                )[0],
                0,
            )
        for ident in graph.questions:
            self.assertEqual(
                self.row(
                    "SELECT state,item_json FROM unresolved WHERE id=?",
                    (ident,),
                )[:],
                ("erased", None),
            )
        for item in graph.versions:
            self.assertEqual(
                self.row(
                    "SELECT content_json FROM knowledge_version "
                    "WHERE knowledge_id=? AND revision=?",
                    (item.knowledge_id, item.revision),
                )[0],
                None,
            )
        for evidence in graph.proof_ids:
            self.assertEqual(
                self.row(
                    "SELECT state,excerpt FROM review_evidence WHERE id=?",
                    (evidence,),
                )[:],
                ("erased", None),
            )

    def assert_replacement_kept(
        self, number: int, old_hash: str, role: str
    ) -> None:
        """Check public reads and absence of new line or HMAC erasure keys."""
        reference = f"codex:{TID}:{number}.1"
        opened = query.open_event(
            self.conn, reference, roots=self.roots, context=0
        )
        self.assertIsNone(opened.get("error"))
        self.assertEqual(opened["text"], REPLACEMENT)
        assert self.scope.project_id is not None
        search = query.search(
            self.conn,
            "KEEPCHANGEDPROOFRECORD",
            cwd=self.scope.project_id,
            env={},
            include_current=True,
            all_projects=True,
        )
        self.assertTrue(search["hits"])
        current_hash = hashlib.sha256(
            line(self.records[number - 1]).rstrip(b"\r\n")
        ).hexdigest()
        key = (self.home / "tombstone.key").read_bytes()
        content_tag = (
            "ev2:"
            + hmac.new(
                key, f"{role}\n{REPLACEMENT}".encode(), hashlib.sha256
            ).hexdigest()
        )
        rows = [
            json.loads(raw)
            for raw in (self.home / tombstones.TOMBSTONE_FILE)
            .read_text()
            .splitlines()
        ]
        self.assertTrue(
            any(
                row["thread_id"] == TID
                and row["line"] == number
                and row["line_sha256"] == old_hash
                for row in rows
            )
        )
        self.assertFalse(
            any(
                row["line_sha256"] in (current_hash, content_tag)
                for row in rows
            )
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM tombstone WHERE line_sha256 IN (?,?)",
                (current_hash, content_tag),
            )[0],
            0,
        )

    def test_old_owner_or_display_proof_does_not_erase_a_replacement(
        self,
    ) -> None:
        for role in ("owner", "display"):
            for selector in ("replay", "event"):
                with self.subTest(role=role, selector=selector):
                    self.doCleanups()
                    self.setUp()
                    graph = self.graph()
                    number, old_hash, event_role = self.replace_record(
                        graph, role
                    )
                    independent = candidate_view(
                        self.conn, graph.unrelated_candidate
                    )
                    independent_question = tuple(
                        self.row(
                            "SELECT * FROM unresolved WHERE id=?",
                            (graph.unrelated_question,),
                        )
                    )
                    original = self.path.read_bytes()
                    selected = (
                        graph.first_intent.display_locator
                        if role == "owner"
                        else graph.first_intent.owner_locator
                    )
                    if selector == "replay":
                        owner = graph.first_intent.owner_locator
                        tombstones.append_tombstones(
                            self.home,
                            [
                                tombstones.tombstone_row(
                                    "codex",
                                    "line",
                                    thread_id=TID,
                                    line=owner.line,
                                    line_sha256=owner.line_sha256,
                                )
                            ],
                        )
                        tombstones.reapply_tombstones(self.conn, self.home)
                    else:
                        erase.erase(
                            self.conn,
                            home=self.home,
                            event_ref=f"codex:{TID}:{selected.line}.1",
                            env={"HOME": str(self.tmp)},
                        )
                    self.assert_replacement_kept(number, old_hash, event_role)
                    self.assert_old_graph_erased(graph)
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
                        independent_question,
                    )
                    raw = store.db_path(self.home).read_bytes()
                    journal = (
                        self.home / tombstones.TOMBSTONE_FILE
                    ).read_bytes()
                    tombstones.reapply_tombstones(self.conn, self.home)
                    self.assertEqual(
                        store.db_path(self.home).read_bytes(), raw
                    )
                    self.assertEqual(
                        (self.home / tombstones.TOMBSTONE_FILE).read_bytes(),
                        journal,
                    )
                    self.assert_replacement_kept(number, old_hash, event_role)
                    self.assertEqual(self.path.read_bytes(), original)

    def test_direct_selection_of_replacement_still_erases_current_content(
        self,
    ) -> None:
        graph = self.graph()
        number, _, _ = self.replace_record(graph, "display")
        original = self.path.read_bytes()
        erase.erase(
            self.conn,
            home=self.home,
            event_ref=f"codex:{TID}:{number}.1",
            env={"HOME": str(self.tmp)},
        )
        self.assertEqual(
            query.open_event(
                self.conn,
                f"codex:{TID}:{number}.1",
                roots=self.roots,
                context=0,
            ).get("error"),
            "not_found",
        )
        current_hash = hashlib.sha256(
            line(self.records[number - 1]).rstrip(b"\r\n")
        ).hexdigest()
        self.assertGreater(
            self.row(
                "SELECT count(*) FROM tombstone WHERE line_sha256=?",
                (current_hash,),
            )[0],
            0,
        )
        self.assertEqual(self.path.read_bytes(), original)
