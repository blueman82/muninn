"""Prompt-time recall: paging through results and user-cited knowledge.

Synthetic rows in temp dirs; nothing touches a live data dir or a provider
root.
"""

from __future__ import annotations

from contextlib import closing
from typing import cast
from unittest import mock

from muninn import hook_frame, hook_recall, knowledge_read, store
from tests.hook_support import CLOSE, RecallCase


class RecallStressTests(RecallCase):
    """Result paging, the term rule and the order frame text is dropped in."""

    def test_prompt_hook_reads_on_to_later_pages_and_keeps_knowledge(
        self,
    ) -> None:
        prompt = "rareone raretwo commonone commontwo commonthree"
        for i in range(6):
            self.talk(f"fail{i}", "rareone raretwo")  # 2 of 5 terms, rank top
        for term in ("commonone", "commontwo", "commonthree"):
            for i in range(20):
                self.talk(f"{term}{i}", f"{term} filler")
        passing = self.talk("pass", "commonone commontwo commonthree both")
        self.add(content="Notes on rareone raretwo")
        out = self.ask(prompt)
        rows = self.lines(out)
        self.assertTrue(rows[0].startswith("- K"))  # knowledge survived
        self.assertEqual(len(rows), 2)
        self.assertIn(self.ref(passing), rows[1])  # found on the second page

    def test_min_terms_default_is_three_and_a_lower_floor_keeps_more(
        self,
    ) -> None:
        self.talk("two", "rareone raretwo filler")
        self.noise(self.repo)
        prompt = "rareone raretwo commonone"
        payload = {"prompt": prompt, "session_id": "sess-now"}
        request = hook_recall.RecallRequest(
            terms=["rareone", "raretwo", "commonone"],
            cwd=str(self.scope.project_id),
            notes=lambda: (),
            limit=4000,
        )

        def kept(**kw: int) -> list[object]:
            """Return the event ids a recall with these knobs keeps."""
            trace: dict[str, object] = {}
            with closing(store.connect_ro(store.db_path(self.home))) as conn:
                hook_recall.recall_block(
                    conn, payload, self.env, trace, request, **kw
                )
            return cast("list[object]", trace["returned_ids"])

        self.assertEqual(kept(), [])
        self.assertEqual(kept(min_terms=3), [])
        self.assertEqual(len(kept(min_terms=2)), 1)

    def test_recall_stops_paging_when_the_results_end(self) -> None:
        self.talk("a", "alphaterm betaterm gammaterm deltaterm all here")
        self.noise(self.repo)
        with mock.patch.object(
            hook_recall.query, "search", wraps=hook_recall.query.search
        ) as spy:
            self.assertIn("hookSpecificOutput", self.ask())
        self.assertEqual(spy.call_count, 1)

    def test_recall_reads_at_most_four_pages_of_five(self) -> None:
        for i in range(30):  # all match two of the five terms: none pass
            self.talk(f"two{i}", "rareone raretwo filler")
        prompt = "rareone raretwo commonone commontwo commonthree"
        with mock.patch.object(
            hook_recall.query, "search", wraps=hook_recall.query.search
        ) as spy:
            self.assertEqual(self.ask(prompt), {})
        calls = [c.kwargs for c in spy.call_args_list]
        self.assertEqual([c["page"] for c in calls], [1, 2, 3, 4])
        self.assertEqual({c["limit"] for c in calls}, {5})

    def test_a_phrase_of_identifier_parts_is_not_a_third_term(self) -> None:
        self.talk("a", "hook_core.py lives in the hooks dir")
        self.noise(self.repo)
        self.assertEqual(self.ask("hook_core.py"), {})  # 2 terms + a phrase
        self.assertIn("hookSpecificOutput", self.ask("hook_core.py lives"))

    def test_recall_text_drops_from_the_end_first(self) -> None:
        entries: list[hook_frame.RecallEntry] = [
            {
                "id": self.add(content=f"note {n}"),
                "revision": 1,
                "state": "active",
                "form": "note",
                "kind": "fact",
                "text": "e" * 200,
                "scope": self.scope.model_dump(),
                "evidence_refs": [self.evidence[0].id],
            }
            for n in (3, 2, 1)
        ]
        hits: list[hook_frame.RecallHit] = [
            {
                "id": n,
                "provider": "codex",
                "role": "user",
                "kind": "prompt",
                "session": "abcd1234",
                "ts": "2026-01-01T00:00:00Z",
                "ref": f"codex:thr:{n}.1",
                "snippet": "«h»" + "s" * 400,
            }
            for n in (1, 2, 3)
        ]

        def rows(text: str) -> list[str]:
            return [
                x.split(" ")[1] for x in text.split("\n") if x.startswith("- ")
            ]

        full = hook_frame.recall_text(entries, hits, ())
        self.assertLessEqual(len(full.encode()), 1500)
        self.assertEqual(rows(full), [item["id"] for item in entries])
        text = hook_frame.recall_text(entries[:1], hits, ())
        self.assertLessEqual(len(text.encode()), 1500)
        self.assertEqual(len(rows(text)), 3)
        self.assertEqual(rows(text)[0], entries[0]["id"])
        self.assertNotIn("codex:thr:3.1", text)
        self.assertNotIn("«", text)
        self.assertTrue(
            text.endswith("`muninn open <ref> --context 3`.\n" + CLOSE)
        )
        shrunk = hook_frame.recall_text(entries[:1], hits, ("x" * 180,))
        self.assertLessEqual(len(shrunk.encode()), 1500)
        self.assertIn("x" * 180, shrunk)

    def test_prompt_hook_lines_carry_current_revision_scope_and_support(
        self,
    ) -> None:
        ident = self.add(content="Decision on alphaterm betaterm gammaterm")
        self.talk("a", "alphaterm betaterm gammaterm deltaterm")
        self.noise(self.repo)
        text = self.body(self.ask())
        self.assertIn(f"{ident} revision 1", text)
        self.assertIn(str(self.scope.project_id), text)
        self.assertIn(f"(evidence: {self.evidence[0].id})", text)
        self.assertNotIn("«", text)
        self.assertNotIn("»", text)


class UserCitedKnowledgeTests(RecallCase):
    """Only current applicable guidance with live support is pushed."""

    def via(self, evidence: int, *, content: str) -> str:
        """Create a note using selected prompt or reply support."""
        return self.add(
            evidence_refs=[self.evidence[evidence].id], content=content
        )

    def test_current_eligibility_uses_live_support_not_actor_or_role_aliases(
        self,
    ) -> None:
        reply = self.via(1, content="reply supported")
        user = self.via(0, content="prompt supported")
        both = self.add(
            content="both supported",
            evidence_refs=[e.id for e in self.evidence],
        )
        unsupported = self.add(content="unsupported")
        self.conn.execute(
            "DELETE FROM knowledge_support WHERE knowledge_id=?",
            (unsupported,),
        )

        def ids() -> set[str]:
            return {
                view.id
                for view in knowledge_read.current_knowledge(
                    self.conn, self.scope, 20.0
                )
            }

        self.assertEqual(ids(), {reply, user, both})
        self.conn.execute(
            "UPDATE review_evidence SET state='erased' WHERE id=?",
            (self.evidence[0].id,),
        )
        self.assertEqual(ids(), {reply})
        self.assertNotIn(unsupported, ids())

    def test_prompt_hook_pushes_supported_and_withholds_unresolved(
        self,
    ) -> None:
        reply = self.via(1, content="Reply note on alphaterm")
        shown = self.via(0, content="User decision on alphaterm")
        hidden = self.add(content="Withheld note on alphaterm")
        self.conn.execute(
            "UPDATE knowledge SET state='withheld' WHERE id=?", (hidden,)
        )
        self.noise(self.repo)
        text = self.body(self.ask())
        self.assertIn(f"- {shown} ", text)
        self.assertIn(f"- {reply} ", text)
        self.assertNotIn(hidden, text)
        listed = knowledge_read.show_knowledge(self.conn, hidden, None)
        self.assertEqual(listed.state, "withheld")
        self.assertIsNotNone(listed.body)

    def test_only_erased_support_matches_push_nothing_at_prompt_time(
        self,
    ) -> None:
        ident = self.via(1, content="Note on alphaterm betaterm gammaterm")
        self.conn.execute(
            "UPDATE review_evidence SET state='erased' WHERE id=?",
            (self.evidence[1].id,),
        )
        self.noise(self.repo)
        self.assertEqual(self.ask("alphaterm betaterm gammaterm"), {})
        self.assertEqual(
            knowledge_read.show_knowledge(self.conn, ident, None).id, ident
        )
