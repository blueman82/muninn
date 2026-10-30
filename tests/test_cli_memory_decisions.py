"""Exercise actual conversational command parsing and stored displays."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import threading
from typing import Any
from unittest import mock

from muninn import (
    cli,
    cli_decision,
    decision_candidates,
    decision_requests,
    review_requests,
)
from muninn.decision_models import CandidateInput
from muninn.decision_requests import admit_application, request_application
from muninn.knowledge_erase import erase_review_support
from muninn.review_models import Receipt
from tests import policy_preview_v1_support as v1
from tests.decision_support import DecisionCase
from tests.test_cli_learning import CliLearningCase


class DecisionCliCase(DecisionCase, CliLearningCase):
    """Use indexed synthetic originals and real command output."""

    def stage_cli(self) -> dict[str, object]:
        """Stage a checked proposal through the actual command handler."""
        path = self.tmp / "candidate.json"
        path.write_text(
            CandidateInput(
                request_id=self.context.request_id,
                result=self.result([self.note()]),
                disposition=None,
            ).model_dump_json()
        )
        code, out = self.call(
            "know", "decision", "stage", "--input", str(path)
        )
        self.assertEqual(code, 0, out)
        return out


class DecisionCliTests(DecisionCliCase):
    """Keep inspection separate from consent and actual application."""

    def test_stage_show_status_and_unresolved_are_real_read_only_views(
        self,
    ) -> None:
        view = self.stage_cli()
        ident = str(view["id"])
        before = self.projection()
        code, display = self.call("know", "decision", "show", ident, "--human")
        self.assertEqual(code, 0, display)
        frame = json.loads(display["text"])
        self.assertEqual(frame["candidate"], view["candidate"])
        self.assertEqual(
            frame["owner_decision"]["approve"],
            f"Please approve memory {ident}.",
        )
        self.assertEqual(
            frame["owner_decision"]["reject"], f"Please reject memory {ident}."
        )
        code, status = self.call("know", "decision", "status", ident)
        self.assertEqual((code, status["state"]), (0, "waiting_verification"))
        self.assertEqual(self.call("know", "unresolved")[1]["unresolved"], [])
        self.assertEqual(self.projection(), before)

    def test_paused_view_uses_actual_policy_ceiling_and_strict_digest(
        self,
    ) -> None:
        ident = str(self.stage_cli()["id"])
        question = {
            "claim": "Choose",
            "question": "Which rule?",
            "affected": [],
            "evidence_refs": [],
        }
        uid = self.save(self.result([], [question])).unresolved_ids[0]
        commands = (
            ("decision", "show", ident),
            ("decision", "status", ident),
            ("unresolved",),
            ("unresolved", uid),
        )
        for admission in (1, 0):
            self.conn.execute(
                "UPDATE review_control SET admission=?", (admission,)
            )
            before = self.projection()
            for args in commands:
                self.assertEqual(self.call("know", *args)[0], 0)
            self.assertEqual(self.projection(), before)
        self.conn.execute(
            "UPDATE review_control SET policy_digest=?", ("f" * 64,)
        )
        for args in commands:
            self.assertEqual(
                self.call("know", *args)[1]["error"], "review_policy_required"
            )
        for value in ("{}", None):
            self.conn.execute(
                "UPDATE review_control SET policy_json=?", (value,)
            )
            before = self.projection()
            for args in commands:
                self.assertEqual(
                    self.call("know", *args)[1]["error"],
                    "review_policy_required",
                )
            self.assertEqual(self.projection(), before)

    def test_invalid_input_ids_and_missing_files_are_safe(self) -> None:
        for args in (
            ("decision", "show", "D_bad"),
            ("unresolved", "U_bad"),
            ("unresolved", "--limit", "0"),
            ("decision", "stage", "--input", str(self.tmp / "missing")),
        ):
            self.assertNotEqual(self.call("know", *args)[0], 0)
        path = self.tmp / "invalid.json"
        path.write_text('{"sentinel_private_field": "secret"}')
        self.assertEqual(
            self.call("know", "decision", "stage", "--input", str(path))[1][
                "error"
            ],
            "rejected_validation",
        )

    def test_whole_new_unicode_display_obeys_exact_current_byte_limit(
        self,
    ) -> None:
        view = self.stage(self.result([self.note(content="日本語 memory")]))
        code, shown = self.call("know", "decision", "show", view.id)
        self.assertEqual(code, 0, shown)
        size = len(shown["text"].encode("utf-8"))
        original = self.policy
        for ceiling, expected in ((size, 0), (size - 1, 2)):
            self.policy = original.model_copy(
                update={"context_bytes": ceiling}
            )
            self.conn.execute(
                "UPDATE review_control SET policy_json=?,policy_digest=?",
                (self.policy.model_dump_json(), self.digest(self.policy)),
            )
            code, result = self.call("know", "decision", "show", view.id)
            self.assertEqual(code, expected, result)
            if expected:
                self.assertEqual(result["error"], "decision_display_limit")
                self.assertNotIn("text", result)
            else:
                self.assertEqual(result["text"], shown["text"])

    def test_refused_acceptance_rolls_back_actual_proof_before_pending_code(
        self,
    ) -> None:
        view = self.stage()
        intent = self.intent(view.id)
        before = self.row("SELECT count(*) FROM review_evidence")[0]
        path = self.tmp / "intent.json"
        path.write_text(intent.model_dump_json())
        with mock.patch(
            "muninn.cli_decision.accept_decision",
            side_effect=ValueError("PRIVATE_UNKNOWN"),
        ):
            code, out = self.call(
                "know", "decision", "submit", "--input", str(path)
            )
        self.assertEqual(
            (code, out["state"], out["code"], out["intent_state"]),
            (0, "waiting_verification", "decision_invalid", "blocked"),
        )
        self.assertEqual(
            self.row("SELECT count(*) FROM review_evidence")[0], before
        )
        self.assertEqual(
            self.row(
                "SELECT count(*) FROM memory_candidate_support "
                "WHERE role!='original'"
            )[0],
            0,
        )
        self.assertNotIn(
            "PRIVATE_UNKNOWN", (self.home / "calls.jsonl").read_text()
        )

    def test_human_stderr_and_pretty_preserve_exact_text_and_safe_logs(
        self,
    ) -> None:
        ident = str(self.stage_cli()["id"])
        code, shown = self.call("know", "decision", "show", ident, "--human")
        self.assertEqual(code, 0, shown)
        self.assertEqual(self.last_stderr, shown["explanation"] + "\n")
        self.assertEqual(len(self.last_stdout.splitlines()), 1)
        code, pretty = self.call("--pretty", "know", "decision", "show", ident)
        self.assertEqual(code, 0, pretty)
        self.assertEqual(pretty["text"], shown["text"])
        self.assertGreater(len(self.last_stdout.splitlines()), 1)
        calls = (self.home / "calls.jsonl").read_text()
        self.assertNotIn("Use Python", calls)
        self.assertNotIn("Please approve", calls)
        self.assertNotIn('"knowledge_ids"', calls)

    def test_unknown_and_erased_decisions_never_reconstruct_payload(
        self,
    ) -> None:
        self.assertEqual(
            self.call("know", "decision", "status", "D_" + "f" * 32)[1][
                "error"
            ],
            "decision_unknown",
        )
        draft = self.stage()
        self.transaction(
            lambda: erase_review_support(
                self.conn, (self.evidence[0].id,), 30.0
            )
        )
        self.assertEqual(
            self.call("know", "decision", "show", draft.id)[1]["error"],
            "decision_erased",
        )
        for name in ("status", "apply"):
            code, view = self.call("know", "decision", name, draft.id)
            self.assertEqual(
                (code, view["state"], view["candidate"]), (0, "erased", None)
            )
            self.assertNotIn("Use Python", json.dumps(view))


class DecisionReadSnapshotTests(DecisionCliCase):
    """Keep complete CLI policy and lifecycle views in one read snapshot."""

    def test_all_explicit_read_callbacks_share_policy_scope_snapshot(
        self,
    ) -> None:
        ident = str(self.stage_cli()["id"])
        question = {
            "claim": "Choose",
            "question": "Which rule?",
            "affected": [],
            "evidence_refs": [],
        }
        uid = self.save(self.result([], [question])).unresolved_ids[0]
        observed: list[tuple[str, bool]] = []

        def observer(name: str) -> Any:
            original = getattr(cli_decision, name)

            def read(
                conn: sqlite3.Connection, *args: Any, **kwargs: Any
            ) -> Any:
                observed.append((name, conn.in_transaction))
                return original(conn, *args, **kwargs)

            return read

        names = (
            "saved_policy",
            "scopes",
            "show_candidate",
            "decision_view",
            "show_unresolved",
            "list_unresolved",
        )
        with contextlib.ExitStack() as stack:
            for name in names:
                stack.enter_context(
                    mock.patch.object(
                        cli_decision, name, side_effect=observer(name)
                    )
                )
            for admission in (1, 0):
                self.conn.execute(
                    "UPDATE review_control SET admission=?", (admission,)
                )
                before = self.projection()
                for args in (
                    ("decision", "show", ident),
                    ("decision", "status", ident),
                    ("unresolved",),
                    ("unresolved", uid),
                ):
                    self.assertEqual(self.call("know", *args)[0], 0)
                self.assertEqual(self.projection(), before)
        self.assertEqual({name for name, _ in observed}, set(names))
        self.assertTrue(all(active for _, active in observed), observed)

    def test_real_application_between_status_queries_never_mixes_receipt(
        self,
    ) -> None:
        ident = str(self.stage_cli()["id"])
        text = self.call("know", "decision", "show", ident)[1]["text"]
        self.approve(self.intent(ident, display=text))
        self.transaction(
            lambda: review_requests.cancel_request(
                self.conn, self.context.request_id, 20.0
            )
        )
        self.transaction(
            lambda: admit_application(self.conn, self.home, ident, 1, 21.0)
        )
        entered, resume = threading.Event(), threading.Event()
        snapshots: list[bool] = []
        answers: list[tuple[int, dict[str, Any]]] = []
        errors: list[BaseException] = []
        original_receipt = decision_candidates.stored_receipt
        original_step = decision_requests._step

        def receipt(conn: sqlite3.Connection, request: str) -> Receipt | None:
            if threading.current_thread() is reader:
                snapshots.append(conn.in_transaction)
                entered.set()
                if not resume.wait(3.0):
                    raise AssertionError("reader release timed out")
            return original_receipt(conn, request)

        def step(*args: Any, **kwargs: Any) -> Any:
            view = original_step(*args, **kwargs)
            if snapshots == [True]:
                resume.set()
            return view

        def read() -> None:
            try:
                answers.append(self.call("know", "decision", "status", ident))
            except BaseException as exc:
                errors.append(exc)

        reader = threading.Thread(target=read)
        with (
            mock.patch.object(
                decision_candidates, "stored_receipt", side_effect=receipt
            ),
            mock.patch.object(decision_requests, "_step", side_effect=step),
        ):
            reader.start()
            try:
                self.assertTrue(entered.wait(3.0))
                saved = request_application(self.home, ident, 25.0)
            finally:
                resume.set()
                reader.join(3.0)
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(saved.state, "saved")
        self.assertIsNotNone(saved.receipt)
        self.assertEqual(len(answers), 1)
        code, status = answers[0]
        self.assertEqual(code, 0, status)
        self.assertEqual(
            status["state"] == "saved", status["receipt"] is not None, status
        )
        self.assertEqual(
            (status["state"], status["receipt"]), ("approved_pending", None)
        )
        self.assertEqual(snapshots, [True])


class RuntimeDecisionFormatTests(v1.V1PreviewFixture):
    """Refuse old stores through the actual ordinary CLI runtime guard."""

    def test_old_marked_store_is_not_opened_by_decision_views(self) -> None:
        self.old("state")
        before = self.path.read_bytes()
        for args in (("unresolved",), ("decision", "status", "D_" + "f" * 32)):
            output = io.StringIO()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "HOME": str(self.home),
                        "MUNINN_HOME": str(self.ctx.data),
                    },
                ),
                contextlib.redirect_stdout(output),
            ):
                code = cli.main(("know", *args))
            self.assertEqual(code, 4)
            result = json.loads(output.getvalue())
            self.assertEqual(result["error"], "store_unavailable")
            self.assertEqual(self.path.read_bytes(), before)
