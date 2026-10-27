"""Preview refusal reasons retain safeguards and omit private error text."""

from __future__ import annotations

import io
import os
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from install import installer, policy_preview, review_policy
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import cli_maint, ingest, platform_lock, store
from tests.policy_preview_support import PreviewFixture, states
from tests.store_support import public_read


class PreviewDiagnosticTests(PreviewFixture):
    """Reproduce native contention and publication in private fixture state."""

    def test_missing_state_reports_missing_without_provisioning(self) -> None:
        self.lock.unlink()
        before = states(self.home)
        with self.assertRaisesRegex(
            StepFailedError,
            "^review_policy_preview_unavailable: state_missing$",
        ):
            review_policy.preflight(self.ctx, self.rec)
        self.assertEqual(states(self.home), before)

    def test_native_writer_contention_reports_busy_without_mutation(
        self,
    ) -> None:
        with store.writer_lock(self.ctx.data, wait_s=0):
            before = states(self.home)
            with self.assertRaisesRegex(
                StepFailedError,
                "^review_policy_preview_unavailable: writer_busy$",
            ):
                review_policy.preflight(self.ctx, self.rec)
            self.assertEqual(states(self.home), before)
        with policy_preview.connection(self.path):
            pass

    def test_unsafe_state_reports_privacy_without_repair(self) -> None:
        with public_read(self, self.lock):
            before = states(self.home)
            with self.assertRaisesRegex(
                StepFailedError,
                "^review_policy_preview_unavailable: unsafe_state$",
            ):
                review_policy.preflight(self.ctx, self.rec)
            self.assertEqual(states(self.home), before)

    def test_heartbeat_publication_discards_stable_database_read(self) -> None:
        database = self.path.read_bytes()
        lock = policy_preview._metadata(self.lock.stat())
        with (
            self.assertRaisesRegex(
                StepFailedError,
                "^review_policy_preview_unavailable: directory_changed$",
            ),
            policy_preview.connection(self.path) as conn,
        ):
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
            cli_maint.heartbeat(
                self.ctx.data, ingest.PassStats(), {"HOME": str(self.home)}
            )
        self.assertEqual(self.path.read_bytes(), database)
        self.assertEqual(policy_preview._metadata(self.lock.stat()), lock)
        self.assertTrue((self.ctx.data / "status.json").is_file())
        with policy_preview.connection(self.path):
            pass

    def test_competing_writer_open_changes_lock_before_lock_refusal(
        self,
    ) -> None:
        if os.name == "nt":
            self.skipTest("POSIX fchmod metadata; Windows has native ACLs")
        before = policy_preview._metadata(self.lock.stat())
        database = self.path.read_bytes()
        with (
            self.assertRaisesRegex(
                StepFailedError,
                "^review_policy_preview_unavailable: lock_changed$",
            ),
            policy_preview.connection(self.path),
        ):
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(self.ctx.data, wait_s=0),
            ):
                self.fail("competing writer acquired preview ownership")
            self.assertNotEqual(
                policy_preview._metadata(self.lock.stat()), before
            )
        self.assertEqual(self.path.read_bytes(), database)
        with store.writer_lock(self.ctx.data, wait_s=0):
            pass

    def test_sidecars_keep_reason_and_all_bytes(self) -> None:
        sidecar = Path(f"{self.path}-wal")
        sidecar.write_bytes(b"private fixture state")
        sidecar.chmod(0o600)
        before = states(self.home)
        with self.assertRaisesRegex(
            StepFailedError,
            "^review_policy_preview_unavailable: sidecar_present$",
        ):
            review_policy.preflight(self.ctx, self.rec)
        self.assertEqual(states(self.home), before)

    def test_database_metadata_change_has_distinct_reason(self) -> None:
        with (
            self.assertRaisesRegex(
                StepFailedError,
                "^review_policy_preview_unavailable: database_changed$",
            ),
            policy_preview.connection(self.path),
        ):
            found = self.path.stat()
            os.utime(
                self.path,
                ns=(found.st_atime_ns, found.st_mtime_ns + 1_000_000_000),
            )

    def test_header_refusal_never_prints_header_bytes(self) -> None:
        self.path.write_bytes(b"private fixture malformed header")
        before = states(self.home)
        with (
            self.assertRaisesRegex(
                StepFailedError,
                "^review_policy_preview_unavailable: unsupported_header$",
            ),
            policy_preview.connection(self.path),
        ):
            self.fail("malformed image admitted")
        self.assertEqual(states(self.home), before)

    def test_native_and_sqlite_errors_have_finite_public_reasons(self) -> None:
        for target, reason, error in (
            (
                platform_lock,
                "io_error",
                OSError("private path and native details"),
            ),
            (
                sqlite3,
                "sqlite_error",
                sqlite3.OperationalError("private SQL and policy details"),
            ),
        ):
            name = "try_lock" if target is platform_lock else "connect"
            with (
                self.subTest(reason=reason),
                mock.patch.object(target, name, side_effect=error),
                self.assertRaisesRegex(
                    StepFailedError,
                    f"^review_policy_preview_unavailable: {reason}$",
                ) as caught,
            ):
                review_policy.preflight(self.ctx, self.rec)
            self.assertIs(caught.exception.__cause__, error)
            with policy_preview.connection(self.path):
                pass

    def test_installer_main_prints_sanitized_refusal_and_fails(self) -> None:
        def install(ctx: Ctx, repo: Path, sha: str) -> Record:
            with policy_preview.connection(self.path):
                self.fail("busy writer admitted")
            return {}

        out = io.StringIO()
        with (
            store.writer_lock(self.ctx.data, wait_s=0),
            mock.patch.object(installer, "install", side_effect=install),
            redirect_stdout(out),
        ):
            code = installer.main(
                [
                    "--repo",
                    str(self.home),
                    "--sha",
                    "a" * 40,
                    "--upgrade",
                    "--dry-run",
                    "--home",
                    str(self.home),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn(
            "FAILED: review_policy_preview_unavailable: writer_busy\n",
            out.getvalue(),
        )
        self.assertNotIn(str(self.path), out.getvalue())

    def test_unrelated_blocking_io_is_not_reported_as_writer_contention(
        self,
    ) -> None:
        with (
            self.assertRaisesRegex(
                StepFailedError,
                "^review_policy_preview_unavailable: io_error$",
            ),
            policy_preview.connection(self.path),
        ):
            raise BlockingIOError("private unrelated read failure")
