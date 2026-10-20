"""Exercise typed learning controls against isolated physical evidence."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import cli, cli_core, query, review_requests
from muninn.knowledge_models import KnowledgeContext, KnowledgeView, Scope
from muninn.review_models import Receipt, RequestView, ReviewDetail
from tests.test_review_commit import CommitCase


class CliLearningCase(CommitCase):
    """Run learning commands against mapped disposable physical evidence."""

    def setUp(self) -> None:
        super().setUp()
        self.cwd = self.tmp / "project"
        self.cwd.mkdir()
        self.conn.execute(
            "INSERT INTO scope_path(cwd,scope_id,method) SELECT ?,id,'cwd' "
            "FROM scope WHERE key=?",
            (str(self.cwd), self.scope.project_id),
        )

    def call(self, *argv: str) -> tuple[int, dict[str, Any]]:
        """Run the real CLI with synthetic HOME and indexed cwd only."""
        output, errors = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        with (
            mock.patch.dict(
                os.environ,
                {
                    "MUNINN_HOME": str(self.home),
                    "HOME": str(self.tmp / "userhome"),
                },
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            os.chdir(self.cwd)
            try:
                code = cli.main(argv)
            finally:
                os.chdir(previous)
        raw = output.getvalue()
        return code, json.loads(raw) if raw else {}

    def propose(self, **updates: object) -> tuple[int, dict[str, Any]]:
        """Submit an explicit proposal file using real admitted support."""
        path = self.tmp / "proposal.json"
        path.write_text(
            json.dumps(
                {
                    "request_id": self.context.request_id,
                    "result": self.result([self.note(**updates)]).model_dump(),
                }
            )
        )
        return self.call("know", "propose", "--input", str(path))


class CliLearningTests(CliLearningCase):
    """Catch retired adapters, unsafe local writes and scoped retrieval."""

    def test_show_maps_evidence_id_to_exact_openable_original(self) -> None:
        """Expose the cited original rather than guessing from search text."""
        code, receipt = self.propose()
        self.assertEqual(code, 0)
        ident = receipt["applied"][0]["knowledge_id"]
        code, shown = self.call("know", "show", ident)
        self.assertEqual(code, 0)
        source = shown["evidence_sources"][0]
        evidence = self.evidence[0]
        self.assertEqual(source["evidence_id"], evidence.id)
        self.assertEqual(
            source["ref"],
            f"{evidence.provider}:{evidence.thread_id}:"
            f"{evidence.line}.{evidence.part}",
        )
        self.assertEqual(source["line_sha256"], evidence.line_sha256)
        code, opened = self.call("open", source["ref"], "--context", "3")
        self.assertEqual(code, 0)
        self.assertTrue(opened["hash_ok"])
        self.assertEqual(
            opened["provenance"]["line_sha256"], source["line_sha256"]
        )

    def test_all_command_shapes_and_json(self) -> None:
        code, out = self.call("learn", "list", "--state", "running")
        self.assertEqual(code, 0)
        self.assertEqual(len(out["requests"]), 1)
        request = RequestView.model_validate(out["requests"][0])
        self.assertEqual(request.id, self.context.request_id)
        code, out = self.call("learn", "show", request.id)
        self.assertEqual(code, 0)
        detail = ReviewDetail.model_validate_json(
            json.dumps({k: out[k] for k in ReviewDetail.model_fields})
        )
        self.assertEqual(len(detail.evidence), 2)
        self.assertIsNone(detail.receipt)
        code, out = self.propose()
        self.assertEqual(code, 0)
        receipt = Receipt.model_validate(
            {k: out[k] for k in Receipt.model_fields}
        )
        ident = receipt.applied[0].knowledge_id
        self.assertEqual(receipt.outcome, "completed")
        for argv in (
            ("know", "list"),
            ("know", "search", "Python"),
            ("know", "list", "--all-projects"),
        ):
            code, out = self.call(*argv)
            self.assertEqual(code, 0)
            self.assertEqual(
                KnowledgeView.model_validate(out["knowledge"][0]).id, ident
            )
        code, out = self.call("know", "show", ident, "--revision", "1")
        self.assertEqual(code, 0)
        self.assertEqual(out["revision"], 1)
        code, out = self.call("know", "context")
        self.assertEqual(code, 0)
        context = KnowledgeContext.model_validate(
            {k: out[k] for k in KnowledgeContext.model_fields}
        )
        self.assertEqual(context.notes[0].id, ident)
        code, out = self.call(
            "know",
            "retract",
            ident,
            "--expected-revision",
            "1",
            "--reason",
            "Withdraw.",
        )
        self.assertEqual((code, out["state"]), (0, "retracted"))

    def test_uuid_ids_reject_old_integer_aliases(self) -> None:
        for argv in (
            ("know", "show", "K1"),
            ("know", "show", "1"),
            ("learn", "show", "R1"),
            ("know", "add"),
            ("know", "check"),
            ("know", "list", "--tag", "x"),
            ("know", "list", "--scope-loop", "x"),
            ("know", "retract", "K_" + "a" * 32, "--reason", "x"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.call(*argv)[0], 2)
        ident = "K_" + "a" * 32
        self.assertEqual(cli_core.knowledge_id({"id": ident}), ident)

    def test_propose_uses_current_attempt_and_receipt_path(self) -> None:
        previous = self.context.attempt_id
        code, out = self.propose()
        self.assertEqual(code, 0)
        self.assertNotEqual(out["attempt_id"], previous)
        self.assertEqual(
            self.row("SELECT generation FROM review_control")[0], 1
        )
        self.assertIsNotNone(
            self.row(
                "SELECT ended_at FROM review_attempt WHERE id=?", (previous,)
            )[0]
        )
        self.assertEqual(
            self.row("SELECT origin FROM knowledge_version")[0], "proposal"
        )
        self.assertIsNone(
            self.row(
                "SELECT requested_json FROM review_attempt WHERE id=?",
                (out["attempt_id"],),
            )[0]
        )
        self.assertIsNotNone(
            self.row(
                "SELECT requested_json FROM review_attempt WHERE id=?",
                (previous,),
            )[0]
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_call")[0], 0)
        repeat_code, repeat = self.propose()
        self.assertEqual(
            (repeat_code, repeat["attempt_id"]), (0, out["attempt_id"])
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 1)
        self.assertEqual(self.row("SELECT count(*) FROM review_attempt")[0], 2)

    def test_manual_proposal_cannot_invent_owner_authority(self) -> None:
        code, out = self.propose(
            kind="preference", evidence_refs=[self.evidence[1].id]
        )
        self.assertEqual((code, out["error"]), (2, "rejected_authority"))
        self.assertEqual(self.row("SELECT count(*) FROM knowledge")[0], 0)

    def test_proposal_physically_rechecks_evidence(self) -> None:
        self.path.write_text(
            self.path.read_text().replace("Use Python", "Use Rust")
        )
        code, out = self.propose()
        self.assertEqual(code, 2)
        self.assertIn(out["error"], {"rejected_evidence", "evidence_changed"})
        self.assertEqual(self.row("SELECT count(*) FROM knowledge")[0], 0)

    def test_retry_reopens_same_cancelled_request(self) -> None:
        ident = self.context.request_id
        code, out = self.call("learn", "cancel", ident)
        self.assertEqual((code, out["state"]), (0, "cancelled"))
        code, out = self.call("learn", "retry", ident)
        self.assertEqual(
            (code, out["id"], out["state"]), (0, ident, "pending")
        )
        self.assertEqual(self.row("SELECT count(*) FROM review_request")[0], 1)

    def test_replay_requires_bounds_and_policy(self) -> None:
        ref = self.window().source
        session = f"{ref.provider}:{ref.thread_id}"
        for args in (
            ("--session", session),
            ("--session", session, "--from-line", "2", "--through-line", "3"),
        ):
            self.assertEqual(self.call("learn", "replay", *args)[0], 2)
        policy = self.tmp / "policy.json"
        policy.write_text(self.policy.model_dump_json())
        code, out = self.call(
            "learn",
            "replay",
            "--session",
            session,
            "--from-line",
            "2",
            "--through-line",
            "3",
            "--policy",
            str(policy),
        )
        self.assertEqual((code, out["id"]), (0, self.context.request_id))
        code, out = self.call(
            "learn", "request", "--session", session, "--from-line", "2"
        )
        self.assertEqual((code, out["error"]), (2, "paired_bounds_required"))
        self.conn.execute(
            "UPDATE review_control SET policy_json=NULL,policy_digest=NULL"
        )
        code, out = self.call("learn", "request", "--session", session)
        self.assertEqual((code, out["error"]), (2, "review_policy_required"))

    def test_search_joins_current_support_despite_stale_fts(self) -> None:
        receipt = self.save(self.result([self.note()]))
        ident = receipt.applied[0].knowledge_id
        cwd = self.row("SELECT cwd FROM event LIMIT 1")[0]
        out = query.search(self.conn, "Python", cwd=cwd, env={})
        self.assertEqual(out["knowledge"][0]["id"], ident)
        self.assertEqual(out["knowledge"][0]["revision"], 1)
        self.assertIn("evidence_refs", out["knowledge"][0])
        self.conn.execute("UPDATE review_evidence SET authority='untrusted'")
        self.conn.execute(
            "UPDATE knowledge_version SET "
            "kind='preference',content_json=json_set(content_json,'$.kind','preference')"
        )
        out = query.search(self.conn, "Python", cwd=cwd, env={})
        self.assertEqual(out["knowledge"], [])
        self.assertTrue(out["hits"])

    def test_policy_digest_and_context_limits_are_explicit(self) -> None:
        self.conn.execute(
            "UPDATE review_control SET policy_digest=?", ("f" * 64,)
        )
        code, out = self.call("know", "context")
        self.assertEqual((code, out["error"]), (2, "review_policy_required"))
        self.assertEqual(self.row("SELECT count(*) FROM review_request")[0], 1)

    def test_proposal_invalid_input_returns_content_free_code(self) -> None:
        path = self.tmp / "invalid-proposal.json"
        path.write_text('{"request_id": "R1", "result": "SECRET-VALIDATION"}')
        code, out = self.call("know", "propose", "--input", str(path))
        self.assertEqual((code, out["error"]), (2, "rejected_validation"))
        self.assertNotIn("SECRET-VALIDATION", json.dumps(out))
        self.assertEqual(self.row("SELECT count(*) FROM review_attempt")[0], 1)

    def test_request_uses_only_uncompleted_activation_interval(self) -> None:
        source = self.window().source
        self.conn.execute("DELETE FROM review_input")
        self.conn.execute("DELETE FROM review_attempt")
        self.conn.execute("DELETE FROM review_request")
        self.conn.execute(
            "UPDATE review_source SET activation_line=1,completed_line=1"
        )
        code, out = self.call(
            "learn", "request", "--session", source.thread_id
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            (out["window"]["from_line"], out["window"]["through_line"]), (2, 3)
        )
        self.assertEqual(out["state"], "pending")
        self.assertEqual(self.row("SELECT count(*) FROM review_attempt")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM review_receipt")[0], 0)

    def test_unknown_session_is_a_json_refusal(self) -> None:
        code, out = self.call(
            "learn", "request", "--session", "unknown-session"
        )
        self.assertEqual((code, out["error"]), (2, "unknown_session"))

    def test_expired_and_retracted_stale_fts_never_return(self) -> None:
        receipt = self.save(self.result([self.note()]))
        ident = receipt.applied[0].knowledge_id
        expiry = {
            "at": 1.0,
            "condition": None,
            "evidence_refs": [self.evidence[0].id],
        }
        self.conn.execute(
            "UPDATE knowledge_version SET expiry_json=?", (json.dumps(expiry),)
        )
        cwd = self.row("SELECT cwd FROM event LIMIT 1")[0]
        result = query.search(self.conn, "Python", cwd=cwd, env={})
        self.assertEqual(result["knowledge"], [])
        self.assertEqual(result["knowledge_expired_omitted"], 1)
        self.conn.execute("UPDATE knowledge_version SET expiry_json=NULL")
        self.conn.execute(
            "UPDATE knowledge SET state='retracted',retracted_at=20.0,"
            "retracted_reason='Withdraw.'"
        )
        self.assertEqual(
            query.search(self.conn, "Python", cwd=cwd, env={})["knowledge"], []
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM knowledge_fts WHERE knowledge_id=?",
                (ident,),
            )[0],
            1,
        )

    def test_revision_preview_and_event_pagination(self) -> None:
        receipt = self.save(self.result([self.note()]))
        ident = receipt.applied[0].knowledge_id
        self.context = self.start(2, 2)
        self.save(
            self.result(
                [
                    self.note(
                        action="replace",
                        knowledge_id=ident,
                        expected_revision=1,
                        content="Python again.",
                    )
                ]
            )
        )
        cwd = self.row("SELECT cwd FROM event LIMIT 1")[0]
        first = query.search(self.conn, "Python", cwd=cwd, env={}, limit=1)
        self.assertEqual(first["knowledge"][0]["revision"], 2)
        second = query.search(
            self.conn, "Python", cwd=cwd, env={}, limit=1, page=2
        )
        self.assertEqual(second["knowledge"], [])
        self.assertFalse(second["has_more"])
        code, old = self.call("know", "show", ident, "--revision", "1")
        self.assertEqual((code, old["revision"]), (0, 1))
        code, current = self.call("know", "show", ident)
        self.assertEqual((code, current["revision"]), (0, 2))

    def test_task_guidance_does_not_escape_task_scope(self) -> None:
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 14.0
            )
        )
        self.scope = Scope(project_id=self.scope.project_id, task="parser")
        self.context = self.start()
        ident = self.save(self.result([self.note()])).applied[0].knowledge_id
        for flags, expected in (
            ((), []),
            (("--task", "writer"), []),
            (("--task", "parser"), [ident]),
            (("--all-projects", "--task", "parser"), [ident]),
        ):
            code, out = self.call("know", "list", *flags)
            self.assertEqual(code, 0)
            self.assertEqual(
                [item["id"] for item in out["knowledge"]], expected
            )
        code, out = self.call("know", "context", "--task", "parser")
        self.assertEqual((code, out["notes"][0]["id"]), (0, ident))
        code, out = self.call("know", "context")
        self.assertEqual((code, out["notes"]), (0, []))

    def test_default_project_read_does_not_union_global_guidance(self) -> None:
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 14.0
            )
        )
        project = self.scope.project_id
        self.scope = Scope(project_id=None, task=None)
        self.conn.execute(
            "UPDATE scope SET key='global',kind='global' WHERE key=?",
            (project,),
        )
        self.conn.execute(
            "UPDATE review_evidence SET scope_json=?",
            (self.scope.model_dump_json(),),
        )
        self.policy = self.policy.model_copy(update={"allow_global": True})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (self.policy.model_dump_json(), self.digest(self.policy)),
        )
        self.context = self.start()
        ident = self.save(self.result([self.note()])).applied[0].knowledge_id
        code, out = self.call("know", "list")
        self.assertEqual((code, out["knowledge"]), (0, []))
        code, out = self.call("know", "list", "--all-projects")
        self.assertEqual((code, out["knowledge"][0]["id"]), (0, ident))
