"""Prompt-time recall: paging through results and user-cited knowledge.

Synthetic rows in temp dirs; nothing touches a live data dir or a provider
root.
"""

from __future__ import annotations

from unittest import mock

from pctx import hook_frame, hook_recall, knowledge
from tests import test_knowledge as tk
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
        self.add(text="Notes on rareone raretwo")
        out = self.ask(prompt)
        rows = self.lines(out)
        self.assertTrue(rows[0].startswith("- K"))  # knowledge survived
        self.assertEqual(len(rows), 2)
        self.assertIn(self.ref(passing), rows[1])  # found on the second page

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
                "id": f"K{n}", "kind": "fact", "date": "2026-09-30",
                "actor": "user", "text": "e" * 300, "cites": ["codex:t:1.1"],
            }
            for n in (3, 2, 1)
        ]  # fmt: skip
        hits: list[hook_frame.RecallHit] = [
            {
                "id": n, "provider": "codex", "role": "user",
                "kind": "prompt", "session": "abcd1234",
                "ts": "2026-01-01T00:00:00Z", "ref": f"codex:thr:{n}.1",
                "snippet": "«h»" + "s" * 400,
            }
            for n in (1, 2, 3)
        ]  # fmt: skip

        def rows(text: str) -> list[str]:
            """Return the id column of every bullet line.

            Args:
                text: Rendered recall block.

            Returns:
                The second word of each line that starts with ``- ``.
            """
            return [
                x.split(" ")[1] for x in text.split("\n") if x.startswith("- ")
            ]

        full = hook_frame.recall_text(entries, hits, ())
        self.assertLessEqual(len(full), 1500)
        self.assertEqual(rows(full), ["K3", "K2", "K1"])  # hits went first
        text = hook_frame.recall_text(entries[:1], hits, ())
        self.assertLessEqual(len(text), 1500)
        self.assertEqual(len(rows(text)), 3)  # K3 and the two best hits
        self.assertNotIn("codex:thr:3.1", text)
        self.assertNotIn("«", text)
        self.assertTrue(
            text.endswith("`pctx open <ref> --context 3`.\n" + CLOSE)
        )
        shrunk = hook_frame.recall_text(entries[:1], hits[:3], ("x" * 180,))
        self.assertLessEqual(len(shrunk), 1500)
        self.assertIn("x" * 180, shrunk)

    def test_prompt_hook_lines_carry_actor_and_cites(self) -> None:
        self.add(text="Decision on alphaterm betaterm gammaterm")
        self.talk("a", "alphaterm betaterm gammaterm deltaterm")
        self.noise(self.repo)
        text = self.body(self.ask())
        self.assertIn("by:claude:abc123]", text)
        self.assertIn("(cites: codex:thr-main:1.1)", text)
        self.assertNotIn("«", text)
        self.assertNotIn("»", text)


class UserCitedKnowledgeTests(RecallCase):
    """Only entries with a live user-prompt citation are pushed.

    The others stay pull-only (``pctx search``, ``pctx know``).
    """

    def via(self, event_id: int | None, quote: str, **kw: object) -> int:
        """Add an entry cited to one event.

        Args:
            event_id: Event the entry cites.
            quote: Verbatim words quoted from that event.
            **kw: Extra fields for the new entry.

        Returns:
            The new knowledge id.
        """
        assert event_id is not None
        return tk.kid(self.add(cites=[(self.ref(event_id), quote)], **kw))

    def test_user_cited_names_entries_with_a_live_user_prompt_cite(
        self,
    ) -> None:
        reply = self.via(self.reply, "wire the zebra cache", text="a reply")
        call = self.via(self.call, "pytest -q tests/test_lookup.py", text="c")
        user = self.via(self.prompt, "use the zebra cache", text="a prompt")
        both = tk.kid(
            self.add(
                cites=[
                    (self.ref(self.reply), "wire the zebra cache"),
                    (self.ref(self.prompt), "use the zebra cache"),
                ],
                text="both",
            )
        )
        # both halves of "role user, kind prompt" count, not either alone
        odd_a = self.talk(
            "oa", "an assistant wrote this prompt", role="assistant"
        )
        odd_b = self.talk("ob", "a user wrote this reply", kind="reply")
        a = self.via(odd_a, "an assistant wrote this", text="odd a")
        b = self.via(odd_b, "a user wrote this", text="odd b")
        ids = [reply, call, user, both, a, b]
        self.assertEqual(knowledge.user_cited(self.ro(), ids), {user, both})
        self.assertEqual(knowledge.user_cited(self.ro(), []), set())
        self.assertEqual(knowledge.user_cited(self.ro(), [999]), set())
        self.rw.execute(
            "UPDATE citation SET state = 'erased', quote = NULL"
            " WHERE knowledge_id = ?",
            (user,),
        )
        self.assertEqual(knowledge.user_cited(self.ro(), ids), {both})

    def test_prompt_hook_pushes_only_user_cited_knowledge(self) -> None:
        said = self.talk(
            "r",
            "alphaterm betaterm gammaterm deltaterm noted",
            kind="reply",
            role="assistant",
        )
        hidden = self.via(
            said,
            "alphaterm betaterm gammaterm",
            text="Reply note on alphaterm",
        )
        shown = tk.kid(self.add(text="User decision on alphaterm"))
        self.noise(self.repo)
        text = self.body(self.ask())
        self.assertIn(f"- K{shown} ", text)
        self.assertNotIn(f"K{hidden} ", text)
        self.assertNotIn("Reply note", text)
        listed = knowledge.list_entries(self.ro(), cwd="/repo")["entries"]
        self.assertIn(f"K{hidden}", [e["id"] for e in listed])  # pull-only

    def test_only_reply_cited_matches_push_nothing_at_prompt_time(
        self,
    ) -> None:
        self.via(
            self.reply,
            "wire the zebra cache",
            text="Note on alphaterm betaterm gammaterm",
        )
        self.noise(self.repo)
        self.assertEqual(self.ask("alphaterm betaterm gammaterm"), {})
