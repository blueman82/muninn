"""Dry-run policy validation retains actual refusals without state writes."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import policy_preview, review_policy, review_policy_default
from install.context import StepFailedError
from muninn import platform_lock, store
from tests.policy_preview_support import PreviewFixture, states
from tests.store_support import public_read


class PolicyPreviewTests(PreviewFixture):
    """Run the real policy queries over private native temporary stores."""

    def test_missing_lock_refuses_without_creating_it(self) -> None:
        self.lock.unlink()
        before = states(self.home)
        with self.assertRaises(StepFailedError):
            review_policy.preflight(self.ctx, self.rec)
        self.assertEqual(states(self.home), before)

    def test_compatible_policy_preview_changes_no_bytes_or_metadata(
        self,
    ) -> None:
        before = states(self.home)
        review_policy.preflight(self.ctx, self.rec)
        self.assertEqual(states(self.home), before)

    def test_default_discovery_also_requires_existing_lock(self) -> None:
        self.lock.unlink()
        before = states(self.home)
        with self.assertRaises(StepFailedError):
            review_policy_default._configured(self.ctx)
        self.assertEqual(states(self.home), before)

    def test_default_discovery_reads_actual_compatible_control(self) -> None:
        before = states(self.home)
        self.assertEqual(
            review_policy_default._configured(self.ctx), (True, None)
        )
        self.assertEqual(states(self.home), before)

    def test_incompatible_policy_retains_original_refusal(self) -> None:
        selected = self.policy.model_copy(
            update={"policy_id": "another-policy"}
        )
        before = states(self.home)
        with self.assertRaisesRegex(
            StepFailedError, "^review_policy_transition_required$"
        ):
            review_policy.preflight(self.ctx, self.record_for(selected))
        self.assertEqual(states(self.home), before)

    def test_invalid_control_digest_retains_original_refusal(self) -> None:
        with (
            store.writer_lock(self.ctx.data),
            closing(store.connect_rw(self.path)) as conn,
        ):
            conn.execute(
                "UPDATE review_control SET policy_digest=?", ("0" * 64,)
            )
        before = states(self.home)
        with self.assertRaisesRegex(
            StepFailedError, "^review_policy_state_invalid$"
        ):
            review_policy.preflight(self.ctx, self.rec)
        self.assertEqual(states(self.home), before)

    def test_legacy_schema_three_is_observed_without_mutation(self) -> None:
        with (
            store.writer_lock(self.ctx.data),
            closing(sqlite3.connect(self.path)) as conn,
        ):
            conn.execute("PRAGMA user_version=3")
        before = states(self.home)
        review_policy.preflight(self.ctx, self.rec)
        self.assertEqual(
            review_policy_default._configured(self.ctx), (False, None)
        )
        self.assertEqual(states(self.home), before)

    def test_same_process_separate_opens_contend_in_both_directions(
        self,
    ) -> None:
        before = states(self.home)
        fd = os.open(self.lock, os.O_RDWR)
        try:
            platform_lock.try_lock(fd)
            with (
                self.assertRaisesRegex(StepFailedError, "preview_unavailable"),
                policy_preview.connection(self.path),
            ):
                self.fail("held writer admitted")
        finally:
            os.close(fd)
        self.assertEqual(states(self.home), before)
        before = states(self.home)
        with policy_preview.connection(self.path):
            fd = os.open(self.lock, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    platform_lock.try_lock(fd)
            finally:
                os.close(fd)
        self.assertEqual(states(self.home), before)

    def test_public_lock_is_refused_without_permission_repair(self) -> None:
        with public_read(self, self.lock):
            before = states(self.home)
            with self.assertRaisesRegex(
                StepFailedError, "preview_unavailable"
            ):
                review_policy.preflight(self.ctx, self.rec)
            self.assertEqual(states(self.home), before)

    def test_public_database_or_directory_is_never_repaired(self) -> None:
        for path in (self.path, self.ctx.data):
            with (
                self.subTest(path=path),
                public_read(self, path, directory=path == self.ctx.data),
            ):
                before = states(self.home)
                with self.assertRaisesRegex(
                    StepFailedError, "preview_unavailable"
                ):
                    review_policy.preflight(self.ctx, self.rec)
                self.assertEqual(states(self.home), before)

    def test_missing_data_directory_is_not_created(self) -> None:
        self.ctx.data = self.home / "missing-data"
        before = states(self.home)
        with self.assertRaisesRegex(StepFailedError, "preview_unavailable"):
            review_policy.preflight(self.ctx, self.rec)
        self.assertEqual(states(self.home), before)

    def test_foreign_posix_owner_is_refused_without_repair(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX uid control; Windows uses native ACL checks")
        original = Path.lstat

        def lstat(path: Path) -> os.stat_result:
            found = original(path)
            if path != self.lock:
                return found
            fields = list(found)
            fields[4] = found.st_uid + 1
            return os.stat_result(fields)

        before = states(self.home)
        with (
            mock.patch.object(Path, "lstat", lstat),
            self.assertRaisesRegex(StepFailedError, "preview_unavailable"),
        ):
            review_policy.preflight(self.ctx, self.rec)
        self.assertEqual(states(self.home), before)

    def test_native_lock_error_keeps_cause_and_closes_its_descriptor(
        self,
    ) -> None:
        error = OSError("native lock unavailable")
        before = states(self.home)
        with (
            mock.patch.object(platform_lock, "try_lock", side_effect=error),
            self.assertRaisesRegex(
                StepFailedError, "preview_unavailable"
            ) as caught,
        ):
            review_policy.preflight(self.ctx, self.rec)
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(states(self.home), before)
        fd = os.open(self.lock, os.O_RDWR)
        try:
            platform_lock.try_lock(fd)
        finally:
            os.close(fd)

    def test_nonregular_or_linked_locks_are_refused_without_cleanup(
        self,
    ) -> None:
        self.lock.unlink()
        target = self.ctx.data / "foreign-lock"
        target.write_bytes(b"retained")
        target.chmod(0o600)
        for kind in ("directory", "symlink", "hardlink"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    self.lock.mkdir(mode=0o700)
                elif kind == "symlink":
                    self.lock.symlink_to(target)
                else:
                    self.lock.hardlink_to(target)
                before = states(self.home)
                with (
                    self.assertRaisesRegex(
                        StepFailedError, "preview_unavailable"
                    ),
                    policy_preview.connection(self.path),
                ):
                    self.fail("unsafe lock admitted")
                self.assertEqual(states(self.home), before)
                if kind == "directory":
                    self.lock.rmdir()
                else:
                    self.lock.unlink()

    def test_sidecars_and_dangling_sidecars_refuse_without_effects(
        self,
    ) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            for content in (b"", b"pending recovery", None):
                with self.subTest(suffix=suffix, content=content):
                    sidecar = Path(f"{self.path}{suffix}")
                    if content is None:
                        sidecar.symlink_to(self.ctx.data / "missing")
                    else:
                        sidecar.write_bytes(content)
                        sidecar.chmod(0o600)
                    before = states(self.home)
                    with self.assertRaisesRegex(
                        StepFailedError, "preview_unavailable"
                    ):
                        review_policy.preflight(self.ctx, self.rec)
                    self.assertEqual(states(self.home), before)
                    sidecar.unlink()

    def test_uncheckpointed_wal_is_not_read_as_stale_main_file(self) -> None:
        with closing(sqlite3.connect(self.path)) as writer:
            self.assertEqual(
                writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal"
            )
            writer.execute(
                "UPDATE review_control SET policy_digest=?", ("0" * 64,)
            )
            writer.commit()
            self.assertGreater(Path(f"{self.path}-wal").stat().st_size, 0)
            before = states(self.home)
            with self.assertRaisesRegex(
                StepFailedError, "preview_unavailable"
            ):
                review_policy.preflight(self.ctx, self.rec)
            self.assertEqual(states(self.home), before)

    def test_unsupported_or_short_header_never_creates_sidecars(self) -> None:
        original = self.path.read_bytes()
        for raw in (b"short", original[:18] + b"\x02\x02" + original[20:]):
            with self.subTest(header=raw[:20]):
                self.path.write_bytes(raw)
                before = states(self.home)
                with (
                    self.assertRaisesRegex(
                        StepFailedError, "preview_unavailable"
                    ),
                    policy_preview.connection(self.path),
                ):
                    self.fail("unsupported header admitted")
                self.assertEqual(states(self.home), before)

    def test_changed_database_or_sidecar_discards_the_read(self) -> None:
        for change in ("metadata", "content", "sidecar"):
            with self.subTest(change=change):
                sidecar = Path(f"{self.path}-wal")
                with (
                    self.assertRaisesRegex(
                        StepFailedError, "preview_unavailable"
                    ),
                    policy_preview.connection(self.path) as conn,
                ):
                    self.assertEqual(
                        conn.execute(
                            "SELECT count(*) FROM review_control"
                        ).fetchone()[0],
                        1,
                    )
                    if change == "metadata":
                        found = self.path.stat()
                        os.utime(
                            self.path,
                            ns=(
                                found.st_atime_ns,
                                found.st_mtime_ns + 1_000_000_000,
                            ),
                        )
                    elif change == "content":
                        with self.path.open("ab") as stream:
                            stream.write(b"changed")
                    else:
                        sidecar.write_bytes(b"foreign state")
                if sidecar.exists():
                    self.assertEqual(sidecar.read_bytes(), b"foreign state")
                    sidecar.unlink()
        fd = os.open(self.lock, os.O_RDWR)
        try:
            platform_lock.try_lock(fd)
        finally:
            os.close(fd)

    def test_replaced_lock_or_database_is_never_accepted_or_cleaned(
        self,
    ) -> None:
        for path in (self.lock, self.path):
            with self.subTest(path=path):
                original = path.read_bytes()
                replacement = self.ctx.data / "replacement"
                replacement.write_bytes(original)
                replacement.chmod(0o600)
                previous = path.stat()
                replaced = False
                with (
                    self.assertRaisesRegex(
                        StepFailedError, "preview_unavailable"
                    ),
                    policy_preview.connection(self.path),
                ):
                    replacement.replace(path)
                    replaced = True
                self.assertEqual(path.read_bytes(), original)
                current = path.stat()
                if replaced:
                    self.assertNotEqual(
                        (previous.st_dev, previous.st_ino),
                        (current.st_dev, current.st_ino),
                    )
                    self.assertFalse(replacement.exists())
                else:
                    self.assertTrue(replacement.exists())
                    replacement.unlink()

    def test_sidecar_appearing_during_open_prevents_query_admission(
        self,
    ) -> None:
        original_connect = sqlite3.connect
        sidecar = Path(f"{self.path}-wal")

        def connect(database: str, *, uri: bool) -> sqlite3.Connection:
            sidecar.write_bytes(b"new foreign state")
            return original_connect(database, uri=uri)

        with (
            mock.patch.object(sqlite3, "connect", side_effect=connect),
            self.assertRaisesRegex(StepFailedError, "preview_unavailable"),
            policy_preview.connection(self.path),
        ):
            self.fail("changed source reached caller queries")
        self.assertEqual(sidecar.read_bytes(), b"new foreign state")

    def test_cancellation_keeps_primary_error_and_releases_lock(self) -> None:
        error = KeyboardInterrupt("stop preview")
        with (
            self.assertRaises(KeyboardInterrupt) as caught,
            policy_preview.connection(self.path),
        ):
            raise error
        self.assertIs(caught.exception, error)
        fd = os.open(self.lock, os.O_RDWR)
        try:
            platform_lock.try_lock(fd)
        finally:
            os.close(fd)

    def test_cleanup_error_does_not_replace_cancellation(self) -> None:
        original_close = os.close
        error = KeyboardInterrupt("original cancellation")

        def close(fd: int) -> None:
            original_close(fd)
            raise OSError("descriptor already released")

        with (
            mock.patch.object(os, "close", side_effect=close),
            self.assertRaises(KeyboardInterrupt) as caught,
            policy_preview.connection(self.path),
        ):
            raise error
        self.assertIs(caught.exception, error)
        fd = os.open(self.lock, os.O_RDWR)
        try:
            platform_lock.try_lock(fd)
        finally:
            os.close(fd)

    def test_normal_install_still_uses_original_mutating_lock_setup(
        self,
    ) -> None:
        self.ctx.dry_run = False
        self.lock.unlink()
        review_policy.preflight(self.ctx, self.rec)
        self.assertTrue(self.lock.is_file())
