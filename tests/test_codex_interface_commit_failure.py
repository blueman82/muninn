"""Keep committed rules recoverable across handled receipt write failures."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from unittest import mock

from install import codex_interface, record
from install.codex_interface_preview import preview
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn.review_progress import digest
from tests.codex_interface_support import InterfaceApplyFixture


class InterfaceCommitFailureTests(InterfaceApplyFixture):
    """Retain exact owned authority while cleanup handles ordinary errors."""

    def test_handled_commit_receipt_failure_resumes_owned_paused_grant(
        self,
    ) -> None:
        self.managed_release()
        request = self.request()
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        original = codex_interface.phase

        def fail_receipt(
            ctx: Ctx, rec: Record, name: str, **changes: object
        ) -> codex_interface.InterfaceReceipt:
            if name == "grant_committed":
                raise OSError("synthetic committed receipt write failure")
            return original(ctx, rec, name, **changes)

        with (
            mock.patch.object(
                codex_interface, "phase", side_effect=fail_receipt
            ),
            self.assertRaises(OSError),
        ):
            self.apply(plan)
        with closing(sqlite3.connect(self.path)) as conn:
            paused = conn.execute(
                "SELECT policy_digest,admission,generation FROM review_control"
            ).fetchone()
            original_request = conn.execute(
                "SELECT * FROM review_request WHERE id=?", (request,)
            ).fetchone()
        self.assertEqual(paused[:2], (digest(plan.candidate), 0))
        self.ctx.learning_prompt = lambda _: self.fail(
            "owned commit recovery requested another grant"
        )
        try:
            result = self.apply(plan)
        except StepFailedError as exc:
            self.fail(f"owned committed grant could not resume: {exc}")
        self.assertEqual(result["codex_interface"]["phase"], "complete")
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT policy_digest,admission,generation FROM review_"
                    "control"
                ).fetchone(),
                paused,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT * FROM review_request WHERE id=?", (request,)
                ).fetchone(),
                original_request,
            )

    def test_receipt_failure_cannot_promote_a_changed_successor(self) -> None:
        self.managed_release()
        self.request()
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        original = codex_interface.phase
        revoked: list[tuple[object, ...]] = []

        def revoke(
            ctx: Ctx, rec: Record, name: str, **changes: object
        ) -> codex_interface.InterfaceReceipt:
            if name == "grant_committed":
                self.sql(
                    "UPDATE review_control SET admission=0,generation="
                    "generation+1"
                )
                with closing(sqlite3.connect(self.path)) as conn:
                    revoked.append(
                        tuple(
                            conn.execute(
                                "SELECT * FROM review_control"
                            ).fetchone()
                        )
                    )
                raise OSError("synthetic concurrent revocation")
            return original(ctx, rec, name, **changes)

        with (
            mock.patch.object(codex_interface, "phase", side_effect=revoke),
            self.assertRaises(OSError),
        ):
            self.apply(plan)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT * FROM review_control").fetchone(),
                revoked[0],
            )
        self.ctx.learning_prompt = lambda _: self.fail(
            "a foreign successor requested a new grant"
        )
        with self.assertRaisesRegex(
            StepFailedError, "codex_interface_state_changed"
        ):
            self.apply(plan)

    def failed_recovery_write(
        self, *, saved: bool, error: type[BaseException]
    ) -> None:
        """Exercise receipt failure before or after its durable publication."""
        self.managed_release()
        self.request()
        plan = preview(self.ctx)
        self.ctx.learning_prompt = lambda _: "yes"
        original_phase, original_save = codex_interface.phase, record.save
        committed: list[tuple[object, ...]] = []

        def fail_phase(
            ctx: Ctx, rec: Record, name: str, **changes: object
        ) -> codex_interface.InterfaceReceipt:
            if name == "grant_committed":
                with closing(sqlite3.connect(self.path)) as conn:
                    committed.append(
                        tuple(
                            conn.execute(
                                "SELECT * FROM review_control"
                            ).fetchone()
                        )
                    )
                raise OSError("synthetic normal receipt failure")
            return original_phase(ctx, rec, name, **changes)

        def fail_save(ctx: Ctx, rec: Record) -> None:
            if (
                rec.get("codex_interface", {}).get("phase")
                == "grant_committed"
            ):
                if saved:
                    original_save(ctx, rec)
                raise error("synthetic recovery receipt failure")
            original_save(ctx, rec)

        with (
            mock.patch.object(
                codex_interface, "phase", side_effect=fail_phase
            ),
            mock.patch.object(record, "save", side_effect=fail_save),
            self.assertRaises(error),
        ):
            self.apply(plan)
        actual = record.load_record(self.ctx.rdir / "rollback-record.json")
        self.assertEqual(
            actual["codex_interface"]["phase"],
            "grant_committed" if saved else "commit_pending",
        )
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                tuple(conn.execute("SELECT * FROM review_control").fetchone()),
                committed[0],
            )
        self.assertIsNone(self.world.fake.loaded)
        self.ctx.learning_prompt = lambda _: self.fail(
            "receipt-write recovery requested another grant"
        )
        result = self.apply(plan)
        self.assertEqual(result["codex_interface"]["phase"], "complete")

    def test_failed_recovery_receipt_write_keeps_exact_successor(self) -> None:
        self.failed_recovery_write(saved=False, error=OSError)

    def test_uncertain_recovery_write_reads_the_durable_committed_phase(
        self,
    ) -> None:
        self.failed_recovery_write(saved=True, error=OSError)

    def test_interrupted_recovery_write_keeps_the_exact_successor(
        self,
    ) -> None:
        self.failed_recovery_write(saved=False, error=KeyboardInterrupt)
