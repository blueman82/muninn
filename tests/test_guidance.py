"""Public help and rendered recall guide navigation and factual support."""

from tests.test_cli import CliCase
from tests.test_hook import RecallCase
from tests.test_ingest import TID


class GuidanceTests(CliCase):
    def test_help_explains_evidence_and_actual_paging_actions(self):
        code, out, help_text = self.pctx("--help")
        self.assertEqual((code, out), (2, ""))
        for phrase in (
            "navigation only",
            "answer_citable=false",
            "every factual claim",
            "opened",
            "cited",
            "unknown",
            "not a truth guarantee",
            "pctx open REF",
            "pctx know show K",
            "has_more",
            "--page N+1",
            "next_from",
            "next_offset",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, help_text)
        for command, phrases in (
            ("search", ("upper bound", "vary", "has_more")),
            ("session", ("next_from",)),
            ("open", ("next_offset",)),
        ):
            _, _, text = self.pctx(command, "--help")
            for phrase in phrases:
                with self.subTest(command=command, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_actual_result_notices_distinguish_eligibility_from_truth(self):
        self.session(TID, "zebra navigation evidence", "zebra reply")
        self.run_ingest()
        for args in (
            ("search", "zebra"),
            ("sessions",),
            ("session", TID),
            ("open", f"codex:{TID}:2.1"),
        ):
            code, out, _ = self.pctx(*args)
            self.assertEqual(code, 0, out)
            notice = out["preview_notice"]
            for phrase in (
                "metadata",
                "navigation only",
                "answer_citable=false",
                "every factual claim",
                "opened",
                "cited",
                "unknown",
                "truth",
            ):
                with self.subTest(command=args[0], phrase=phrase):
                    self.assertIn(phrase, notice)


class HookGuidanceTests(RecallCase):
    def test_bounded_start_and_recall_contain_support_guidance_once(self):
        self.talk("guidance", "alphaterm betaterm gammaterm deltaterm")
        for provider, cap in (("claude", 4000), ("codex", 2800)):
            text = self.body(self.start(provider))
            self.assertLessEqual(len(text), cap)
            for phrase in (
                "navigation only",
                "answer_citable=false",
                "every factual claim",
                "opened",
                "cited",
                "unknown",
                "truth",
                "has_more",
            ):
                with self.subTest(provider=provider, phrase=phrase):
                    self.assertIn(phrase, text)
            self.assertEqual(text.count("answer_citable=false"), 1)
            recall = self.body(self.ask(provider=provider))
            self.assertLessEqual(
                len(recall), 900 if provider == "codex" else 1500
            )
            for phrase in ("navigation only", "opened", "cited", "unknown"):
                self.assertIn(phrase, recall)
            self.assertEqual(recall.count("navigation only"), 1)
