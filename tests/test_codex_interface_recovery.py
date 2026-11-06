"""Check SQL commit classification without live installer or service use."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from unittest import mock

from install import (
    codex_interface,
    legacy_state,
    policy_records,
    steps_release,
)
from install.codex_interface_preview import preview
from install.context import StepFailedError
from muninn.review_json import canonical_json
from muninn.review_progress import digest
from tests.codex_interface_support import InterfaceApplyFixture
from tools.codex_interface_artifacts import plan_artifacts
from tools.codex_policy_artifacts import publish_files


class InterfaceCommitTests(InterfaceApplyFixture):
    """Retain approved intent and old queue across predecessor/successor."""

    def intent(
        self,
    ) -> tuple[dict[str, object], codex_interface.InterfaceReceipt]:
        """Prepare a temp operation with explicit synthetic yes consent."""
        self.managed_release()
        plan = preview(self.ctx)
        self.ctx.dry_run = False
        self.ctx.codex_interface = plan.digest
        self.ctx.interface_plan = plan
        self.ctx.learning_prompt = lambda _: "yes"
        self.ctx.rdir.mkdir(mode=0o700)
        self.operation = self.enterContext(legacy_state.operation(self.ctx))
        self.enterContext(codex_interface.owned(self.ctx, self.operation))
        rec: dict[str, object] = {
            "repo": str(self.repo),
            "sha": self.sha,
            "ts": self.ctx.ts,
            "python": {"path": str(self.executable)},
        }
        codex_interface.run(self.ctx, rec, plan)
        with mock.patch(
            "install.steps_release.prepare_runtime",
            return_value=self.executable,
        ):
            steps_release.prepare_release(self.ctx, rec)
            steps_release.pin(self.ctx, rec)
        value = codex_interface.receipt(self.ctx, rec)
        bundle = plan_artifacts(plan, value.approval, self.ctx.ts)
        publish_files(self.ctx.data, bundle.files)
        predecessor = plan.state.control
        successor = predecessor | {
            "policy_json": canonical_json(plan.candidate),
            "policy_digest": digest(plan.candidate),
            "generation": predecessor["generation"] + 1,
        }
        value = codex_interface.phase(
            self.ctx,
            rec,
            "commit_pending",
            contained=plan.state,
            predecessor=predecessor,
            successor=successor,
        )
        return rec, value

    def test_commit_keeps_old_rows_and_crash_recognizes_exact_successor(
        self,
    ) -> None:
        request = self.request()
        rec, value = self.intent()
        with closing(sqlite3.connect(self.path, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            with mock.patch(
                "muninn.review_managed_files.matches", return_value=True
            ):
                codex_interface.commit_transaction(self.ctx, rec, conn, value)
            self.assertEqual(
                codex_interface.recognize(conn, value), "successor"
            )
            self.assertEqual(
                conn.execute(
                    "SELECT policy_digest FROM review_request WHERE id=?",
                    (request,),
                ).fetchone()[0],
                digest(self.old),
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM review_request").fetchone()[
                    0
                ],
                1,
            )
            conn.execute("UPDATE review_control SET generation=generation+1")
            with self.assertRaisesRegex(
                StepFailedError, "codex_interface_state_changed"
            ):
                codex_interface.recognize(conn, value)

    def test_added_old_request_refuses_whole_complete_set_before_commit(
        self,
    ) -> None:
        self.request()
        rec, value = self.intent()
        self.request(number=2)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            with self.assertRaisesRegex(
                StepFailedError, "codex_interface_state_changed"
            ):
                codex_interface.commit_transaction(self.ctx, rec, conn, value)
            self.assertEqual(
                conn.execute(
                    "SELECT policy_digest FROM review_control"
                ).fetchone()[0],
                digest(self.old),
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM review_request").fetchone()[
                    0
                ],
                2,
            )

    def test_receipt_rejects_partial_or_widened_commit_phase(self) -> None:
        _, value = self.intent()
        raw = value.model_dump(mode="json")
        for change in (
            {"contained": None},
            {"predecessor": None},
            {"successor": None},
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(
                    StepFailedError, "codex_interface_record_invalid"
                ),
            ):
                policy_records.codex_interface(raw | change)
        assert value.successor is not None
        with self.assertRaisesRegex(
            StepFailedError, "codex_interface_record_invalid"
        ):
            policy_records.codex_interface(
                raw
                | {
                    "successor": value.successor
                    | {"admission": 1 - value.successor["admission"]}
                }
            )

    def test_whole_receipt_envelope_refuses_before_unreadable_save(
        self,
    ) -> None:
        rec, _ = self.intent()
        with self.assertRaisesRegex(StepFailedError, "codex_interface_limit"):
            policy_records.validate(
                rec | {"opaque_preimage": "x" * (4 * 1024 * 1024)}
            )
