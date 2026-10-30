"""Keep unmapped projects empty without hiding real context failures."""

from __future__ import annotations

from unittest import mock

from muninn import hook
from tests import test_hook_knowledge_seam as seam
from tests.hook_support import USAGE, HookCase
from tests.ingest_support import rollout
from tests.test_classify import codex_meta, user_msg


class NewProjectHookTests(seam.HookKnowledgeCase):
    """Use real store guards and unrelated nonempty project knowledge."""

    def test_unmapped_project_is_normal_empty_without_cross_project_leak(
        self,
    ) -> None:
        ident = self.add(content="PRIVATE_OTHER_PROJECT")
        before = list(self.conn.iterdump())
        with mock.patch.object(
            hook, "project_cwd", return_value=str(self.tmp / "new-project")
        ):
            for provider in ("claude", "codex"):
                answer = self.dispatch(provider=provider)
                text = self.body(answer)
                self.assertIn("no indexed project memory", text)
                self.assertNotIn("unavailable", text)
                self.assertNotIn(ident, text)
                self.assertNotIn("PRIVATE_OTHER_PROJECT", text)
                self.assertTrue(text.endswith("</muninn-memory>"))
                self.assertLessEqual(len(text.encode()), 2800)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_unknown_builder_error_has_safe_context_label(self) -> None:
        trace: dict[str, object] = {}
        with mock.patch.object(
            hook, "_start_block", side_effect=ValueError("PRIVATE_EXCEPTION")
        ):
            answer = hook.session_start(
                {"cwd": self.scope.project_id},
                "codex",
                {"MUNINN_HOME": str(self.home)},
                trace=trace,
            )
        text = str(answer)
        self.assertEqual(trace["error"], "context_unavailable")
        self.assertIn("context unavailable", text)
        self.assertNotIn("store unavailable", text)
        self.assertNotIn("PRIVATE_EXCEPTION", text)


class OddPayloadHookTests(HookCase):
    """Retain safe empty context for absent or invalid project hints."""

    def test_odd_payloads_never_raise(self) -> None:
        payloads: tuple[object, ...] = (
            "text",
            5,
            None,
            [],
            {"cwd": 5},
            {"transcript_path": 7},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                out = hook.session_start(payload, "claude", self.env)
                self.assertIn("no indexed project memory", self.body(out))
                self.assertNotIn("unavailable", self.body(out))
                self.assertIn(USAGE, self.body(out))
        self.assertIn(
            USAGE, self.body(self.start_hook(transcript_path="a\0b"))
        )
        self.assertIn(
            "no indexed project memory",
            self.body(hook.session_start({}, "nonsense", self.env)),
        )


class LaterProjectIngestTests(seam.HookKnowledgeCase):
    """Read a later real project mapping without a read-side fallback."""

    def test_later_ingest_creates_mapping_for_later_session_start(
        self,
    ) -> None:
        other = self.add(content="OTHER_PROJECT_SENTINEL")
        cwd = str(self.tmp / "new-project")
        with mock.patch.object(hook, "project_cwd", return_value=cwd):
            self.assertIn(
                "no indexed project memory", self.body(self.dispatch())
            )
            self.write(
                rollout("new-project"),
                [
                    codex_meta("user", "new-project", cwd=cwd),
                    user_msg(1, "A new project record."),
                ],
            )
            self.run_ingest()
            before = list(self.conn.iterdump())
            text = self.body(self.dispatch())
            self.assertIn("Project knowledge for", text)
            self.assertNotIn("no indexed project memory", text)
            self.assertNotIn("OTHER_PROJECT_SENTINEL", text)
            self.assertNotIn(other, text)
            self.assertEqual(list(self.conn.iterdump()), before)


class HookFailurePrivacyTests(seam.HookKnowledgeCase):
    """Expose only finite diagnostics, including unknown store failures."""

    def test_store_error_never_logs_raw_exception_text(self) -> None:
        trace: dict[str, object] = {}
        with mock.patch.object(
            hook,
            "_open",
            side_effect=hook.store.StoreUnavailableError("PRIVATE_EXCEPTION"),
        ):
            answer = hook.session_start(
                {"cwd": self.scope.project_id},
                "codex",
                {"MUNINN_HOME": str(self.home)},
                trace=trace,
            )
        self.assertEqual(trace["error"], "store_unavailable")
        self.assertEqual(trace["detail"], "store_detail_unknown")
        self.assertNotIn("PRIVATE_EXCEPTION", str(answer) + str(trace))
