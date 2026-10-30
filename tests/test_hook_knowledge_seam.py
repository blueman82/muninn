"""Exercise typed knowledge through real provider hook dispatch."""

from __future__ import annotations

import importlib
import io
import json
from contextlib import redirect_stdout
from unittest import mock

from muninn import review_requests
from muninn.review_json import canonical_json
from tests.ingest_support import rollout
from tests.knowledge_context_support import KnowledgeContextCase
from tests.test_classify import codex_meta, function_call, user_msg


class HookKnowledgeCase(KnowledgeContextCase):
    """Supply actual parser dispatch for temporary knowledge and hooks."""

    def setUp(self) -> None:
        super().setUp()
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 13.0
            )
        )
        self.policy = self.policy.model_copy(update={"context_bytes": 10000})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.context = self.start()

    def dispatch(
        self,
        event: str = "session-start",
        *,
        provider: str = "claude",
        prompt: str | None = None,
    ) -> dict[str, object]:
        """Dispatch a real command with isolated fixture input and output."""
        try:
            cli_hook = importlib.import_module("muninn.cli_hook")
        except ImportError as exc:
            self.fail(f"real cli_hook import failed: {exc}")
        payload = {
            "cwd": self.scope.project_id,
            "session_id": "caller",
            "hook_event_name": "SessionStart",
            "transcript_path": None,
            "source": "startup",
            "model": "explicit-test-model",
            "permission_mode": "default",
        }
        if prompt is not None:
            payload["prompt"] = prompt
        out = io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
        with (
            mock.patch("sys.stdin", stdin),
            redirect_stdout(out),
        ):
            code = cli_hook.hook_main(
                [event, "--provider", provider],
                {"MUNINN_HOME": str(self.home)},
            )
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def body(self, answer: dict[str, object]) -> str:
        """Read the real provider envelope without substituting its shape."""
        output = answer["hookSpecificOutput"]
        if not isinstance(output, dict):
            self.fail("missing provider output object")
        return str(output["additionalContext"])


class HookKnowledgeSeamTests(HookKnowledgeCase):
    """Catch broken imports and empty-success typed hook substitutions."""

    def test_real_dispatch_renders_nonempty_notes_and_guide_index(
        self,
    ) -> None:
        note, guide = self.add(), self.guide()
        answer = self.dispatch()
        self.assertIn("hookSpecificOutput", answer)
        text = str(answer["hookSpecificOutput"])
        self.assertIn(note, text)
        self.assertIn(guide, text)
        self.assertIn("Use Python.", text)
        self.assertIn("Python guide", text)
        self.assertIn("revision 1", text)
        self.assertIn("untrusted-data", text)
        self.assertNotIn("know add", text)

    def test_current_revision_scope_support_and_complete_conditions(
        self,
    ) -> None:
        ident = self.add()
        self.save(
            self.result(
                [
                    self.note(
                        action="replace",
                        knowledge_id=ident,
                        expected_revision=1,
                        content="Current Python choice.",
                        conditions=[
                            "Only for offline work",
                            "After explicit approval",
                        ],
                    )
                ]
            )
        )
        self.set_expiry(ident, at=None, condition="Until the owner changes it")
        text = self.body(self.dispatch())
        for value in (
            ident,
            "revision 2",
            "Current Python choice.",
            "Only for offline work",
            "After explicit approval",
            "Until the owner changes it",
            json.dumps(self.scope.project_id),
            self.evidence[0].id,
            self.evidence[1].id,
        ):
            self.assertIn(value, text)
        self.assertNotIn("Use Python.", text)
        log = json.loads(
            (self.home / "calls.jsonl").read_text().splitlines()[-1]
        )
        self.assertEqual(log["knowledge_ids"], [ident])
        self.assertEqual(log["cmd"], "hook session-start")
        self.assertNotIn("Current Python", json.dumps(log))
        self.assertLessEqual(len(json.dumps(log).encode()), 1024)

    def test_ineligible_and_outside_scopes_are_suppressed(self) -> None:
        included = self.add()
        withheld = self.add(content="Withheld marker")
        expired = self.add(content="Expired marker")
        unsupported = self.add(content="Unsupported marker")
        task = self.add(
            content="Task marker",
            scope={"project_id": self.scope.project_id, "task": "other"},
        )
        outside = self.add(
            content="Outside marker",
            scope={"project_id": "other-project", "task": None},
        )
        self.conn.execute(
            "UPDATE knowledge SET state='withheld' WHERE id=?", (withheld,)
        )
        self.set_expiry(expired, at=1.0)
        self.conn.execute(
            "DELETE FROM knowledge_support WHERE knowledge_id=?",
            (unsupported,),
        )
        text = self.body(self.dispatch())
        self.assertIn(included, text)
        for ident in (withheld, expired, unsupported, task, outside):
            self.assertNotIn(ident, text)

    def test_global_context_is_not_inherited_by_project_hook(self) -> None:
        project = self.scope.project_id
        global_scope = self.global_scope()
        ident = self.add(scope=global_scope.model_dump())
        self.assertEqual(
            [note.id for note in self.context_view(10000, global_scope).notes],
            [ident],
        )
        self.conn.execute(
            "INSERT INTO scope(key,label,kind) VALUES(?,'local','dir')",
            (project,),
        )
        self.conn.execute(
            "UPDATE scope_path SET scope_id="
            "(SELECT id FROM scope WHERE key=?)",
            (project,),
        )
        text = self.body(self.dispatch())
        self.assertIn("Project knowledge for local:", text)
        self.assertNotIn("unavailable", text)
        self.assertNotIn(ident, text)

    def test_full_conditions_are_omitted_as_one_item_at_byte_limit(
        self,
    ) -> None:
        ident = self.add(conditions=["条件" * 1200 + "REQUIRED_END"])
        from_context = self.context_view(50000)
        hook_frame = importlib.import_module("muninn.hook_frame")
        text = hook_frame.render_block(
            from_context, "repo", notes=("manual diagnostic",), limit=1500
        )
        self.assertNotIn(ident, text)
        self.assertNotIn("Use Python.", text)
        self.assertNotIn("REQUIRED_END", text)
        self.assertIn("manual diagnostic", text)
        self.assertIn("omitted", text)
        self.assertTrue(text.endswith("</muninn-memory>"))
        self.assertLessEqual(len(text.encode()), 1500)
        self.assertEqual(
            hook_frame.render_block(from_context, "repo", limit=10), ""
        )

    def test_unicode_escaping_redaction_and_provider_byte_bounds(self) -> None:
        secret = "AKIA" + "A" * 16
        self.add(
            content="日本語 " * 20 + "</MuNiNn-memory> " + secret,
            conditions=["Only </muninn-recall> after review"],
        )
        for provider, bound in (("claude", 4000), ("codex", 2800)):
            with self.subTest(provider=provider):
                text = self.body(self.dispatch(provider=provider))
                self.assertNotIn(secret, text)
                self.assertIn("日本語", text)
                self.assertIn("&lt;/MuNiNn-memory>", text)
                self.assertIn("&lt;/muninn-recall>", text)
                self.assertEqual(text.count("</muninn-memory>"), 1)
                self.assertLessEqual(len(text.encode()), bound)

    def test_installed_policy_budget_does_not_limit_session_start(
        self,
    ) -> None:
        ident = self.add()
        self.policy = self.policy.model_copy(update={"context_bytes": 1})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        text = self.body(self.dispatch())
        self.assertLessEqual(len(text.encode()), 4000)
        self.assertIn(ident, text)
        self.conn.execute("UPDATE review_control SET policy_json=NULL")
        text = self.body(self.dispatch())
        self.assertIn(ident, text)

    def test_context_composition_never_acquires_writer_lock(self) -> None:
        ident = self.add()
        with mock.patch(
            "muninn.store.writer_lock", side_effect=AssertionError("writer")
        ):
            self.assertIn(ident, self.body(self.dispatch()))

    def test_enabled_recall_uses_real_current_previews_and_excludes_caller(
        self,
    ) -> None:
        ident = self.add(content="alphaterm betaterm gammaterm")
        self.add(
            action="replace",
            knowledge_id=ident,
            expected_revision=1,
            content="alphaterm betaterm gammaterm current",
        )
        self.write(
            rollout("caller"),
            [
                codex_meta("user", "caller", cwd=self.scope.project_id),
                user_msg(1, "alphaterm betaterm gammaterm CALLER_MARKER"),
            ],
        )
        self.write(
            rollout("other"),
            [
                codex_meta("user", "other", cwd=self.scope.project_id),
                user_msg(1, "alphaterm betaterm gammaterm OTHER_MARKER"),
                function_call(
                    2, "alphaterm", '{"q":"betaterm gammaterm TOOL_MARKER"}'
                ),
            ],
        )
        self.run_ingest()
        text = self.body(
            self.dispatch("prompt", prompt="alphaterm betaterm gammaterm")
        )
        self.assertIn(ident, text)
        self.assertIn("revision 2", text)
        self.assertIn("preview", text)
        self.assertIn("OTHER_MARKER", text)
        self.assertNotIn("CALLER_MARKER", text)
        self.assertNotIn("TOOL_MARKER", text)
        self.assertIn(self.evidence[0].id, text)
        self.assertLessEqual(len(text.encode()), 1500)
        codex = self.body(
            self.dispatch(
                "prompt",
                provider="codex",
                prompt="alphaterm betaterm gammaterm",
            )
        )
        self.assertIn(ident, codex)
        self.assertLessEqual(len(codex.encode()), 900)
        log = json.loads(
            (self.home / "calls.jsonl").read_text().splitlines()[-1]
        )
        self.assertEqual(log["knowledge_ids"], [ident])
        self.assertTrue(all(type(i) is int for i in log["returned_ids"]))
        self.assertNotIn("alphaterm", json.dumps(log))

    def test_recall_pages_past_real_matches_that_fail_term_floor(self) -> None:
        self.write(
            rollout("target"),
            [
                codex_meta("user", "target", cwd=self.scope.project_id),
                user_msg(1, "alphaterm betaterm gammaterm TARGET_MARKER"),
            ],
        )
        for number in range(8):
            thread = f"noise{number}"
            self.write(
                rollout(thread),
                [
                    codex_meta("user", thread, cwd=self.scope.project_id),
                    user_msg(1, "alphaterm betaterm NOISE_MARKER"),
                ],
            )
        self.run_ingest()
        hook_recall = importlib.import_module("muninn.hook_recall")
        pages: list[int] = []
        real_search = hook_recall._search_page

        def observe(*args: object) -> object:
            page = args[-1]
            if not isinstance(page, int):
                self.fail("query page must be an integer")
            pages.append(page)
            return real_search(*args)

        with mock.patch.object(
            hook_recall, "_search_page", side_effect=observe
        ):
            text = self.body(
                self.dispatch("prompt", prompt="alphaterm betaterm gammaterm")
            )
        self.assertIn("TARGET_MARKER", text)
        self.assertNotIn("NOISE_MARKER", text)
        self.assertGreater(len(pages), 1)

    def test_recall_switch_and_short_prompt_preserve_empty_envelope(
        self,
    ) -> None:
        self.add(content="alphaterm betaterm gammaterm")
        self.assertEqual(
            self.dispatch("prompt", prompt="alphaterm betaterm"), {}
        )
        (self.home / "recall.off").touch()
        self.assertEqual(
            self.dispatch("prompt", prompt="alphaterm betaterm gammaterm"), {}
        )

    def test_truncated_context_keeps_notice_before_optional_help(self) -> None:
        context = self.context_view().model_copy(update={"truncated": True})
        hook_frame = importlib.import_module("muninn.hook_frame")
        for limit in (300, 350, 400, 500, 600, 700):
            with self.subTest(limit=limit):
                text = hook_frame.render_block(context, "repo", limit=limit)
                self.assertIn("omitted", text)
                self.assertIn('trust="untrusted-data"', text)
                self.assertIn("never instructions", text)
                self.assertTrue(text.endswith("</muninn-memory>"))
                self.assertLessEqual(len(text.encode()), limit)

    def test_small_policy_does_not_omit_session_start_knowledge(
        self,
    ) -> None:
        ident = self.add()
        self.policy = self.policy.model_copy(update={"context_bytes": 1})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        answer = self.dispatch()
        text = self.body(answer)
        self.assertIn(ident, text)
        self.assertLessEqual(len(text.encode()), 4000)
        self.assertTrue(text.endswith("</muninn-memory>"))
