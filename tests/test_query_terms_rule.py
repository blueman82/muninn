"""The query-term rules: list, none and document-frequency filtering."""

from __future__ import annotations

from muninn import query
from muninn.query import terms
from tests.query_support import QueryCase

QUERIES = [
    "the of",
    "How does the hook_core.py handle a prompt?",
    'what is "the quick fox" and more',
    "",
    "a I",
    "alpha beta alpha gamma",
]


class TermsRuleTests(QueryCase):
    """Every rule against synthetic stores; nothing real is read."""

    def fill(self, events: int) -> None:
        """Add events that all hold 'the'; every tenth holds 'rareword'."""
        scope, source = self.add_scope(), self.add_source()
        for index in range(events):
            extra = " rareword" if index % 10 == 0 else ""
            self.add_event(source, scope, f"the item{index}{extra}")
        self.rw.commit()

    def test_list_rule_matches_build_fts_query(self) -> None:
        conn = self.ro()
        for text in QUERIES:
            fts, _ = terms.fts_terms(conn, text, "list")
            self.assertEqual(fts, query.build_fts_query(text), text)
        self.assertEqual(
            terms.fts_terms(conn, "How does the hook_core.py work", "list")[1],
            ["does", "hook_core", "py", "work"],
        )

    def test_none_rule_keeps_stopwords(self) -> None:
        fts, kept = terms.fts_terms(self.ro(), "the cat", "none")
        self.assertEqual((fts, kept), ('"the" OR "cat"', ["the", "cat"]))

    def test_df_drops_frequent_term_and_keeps_rare(self) -> None:
        self.fill(terms.MIN_EVENTS)
        fts, kept = terms.fts_terms(self.ro(), "the rareword", "df:0.5")
        self.assertEqual((fts, kept), ('"rareword"', ["rareword"]))
        # A tenth of the events hold rareword; ratio 1 drops nothing.
        _, kept = terms.fts_terms(self.ro(), "the rareword", "df:1")
        self.assertEqual(kept, ["the", "rareword"])

    def test_small_store_falls_back_to_list(self) -> None:
        self.fill(terms.MIN_EVENTS - 1)
        for text in QUERIES:
            self.assertEqual(
                terms.fts_terms(self.ro(), text, "df:0.1"),
                terms.fts_terms(self.ro(), text, "list"),
            )

    def test_all_frequent_keeps_the_rarest(self) -> None:
        self.fill(terms.MIN_EVENTS)
        _, kept = terms.fts_terms(self.ro(), "the rareword", "df:0.01")
        self.assertEqual(kept, ["rareword"])
        # Absent from the store, 'of' has the lowest frequency of the two.
        _, kept = terms.fts_terms(self.ro(), "the of", "df:0.5")
        self.assertEqual(kept, ["of"])
        _, kept = terms.fts_terms(self.ro(), "the", "df:0.5")
        self.assertEqual(kept, ["the"])

    def test_phrases_are_never_df_filtered(self) -> None:
        self.fill(terms.MIN_EVENTS)
        fts, kept = terms.fts_terms(
            self.ro(), 'rareword "the item1" hook_core.py', "df:0.5"
        )
        self.assertEqual(kept, ["rareword", "hook_core", "py"])
        self.assertEqual(
            fts,
            '"rareword" OR "hook_core" OR "py" OR "the item1"'
            ' OR "hook core py"',
        )

    def test_invalid_rules_raise(self) -> None:
        for rule in ("", "x", "df:", "df:0", "df:1.5", "df:-1", "df:nan"):
            with self.assertRaises(ValueError, msg=rule):
                terms.parse_rule(rule)
        self.assertEqual(terms.parse_rule("df:0.25"), 0.25)
        self.assertIsNone(terms.parse_rule("none"))

    def test_search_takes_the_rule(self) -> None:
        self.fill(terms.MIN_EVENTS)
        args = {"cwd": "/repo", "env": {}, "all_projects": True}
        listed = query.search(self.ro(), "the", **args)
        self.assertEqual(listed["note"], "no searchable terms")
        for rule in ("none", "df:0.5"):
            found = query.search(self.ro(), "the", terms=rule, **args)
            self.assertTrue(found["hits"], rule)
        bad = query.search(self.ro(), "the", terms="nope", **args)
        self.assertIn("error", bad)
