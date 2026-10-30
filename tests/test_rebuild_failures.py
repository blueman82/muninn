"""Conservative rebuild failure, publication and retained archive behavior."""

from __future__ import annotations

import contextlib
import dataclasses
import os
import sqlite3
from collections.abc import Callable, Generator
from pathlib import Path
from typing import BinaryIO
from unittest import mock

from muninn import cli_rebuild, erase_residue, platform_rebuild, store
from muninn import review_pause as q
from tests.cli_support import CliCase
from tests.knowledge_retirement_support import native_note
from tests.review_pause_support import initialize
from tests.store_support import assert_private, public_read
from tests.test_ingest import TID


class RebuildFailureTests(CliCase):
    """Use a controlled current source and preserve every artifact."""

    def setUp(self) -> None:
        super().setUp()
        self.conn.close()
        initialize(self.home)
        self.session(TID, "Please retain this instruction.", "kept")
        self.assertEqual(self.muninn("ingest")[0], 0)
        native_note(self.home, self.roots, "retained zebra")
        self.db = store.db_path(self.home)
        self.unowned = self.home / "muninn.sqlite-unknown"
        self.unowned.write_bytes(b"owner")

    def asides(self) -> list[Path]:
        """Enumerate recovery residue through the public scan."""
        return [
            Path(name)
            for name in erase_residue.aside_files(self.home)
            if Path(name).name.startswith(store.RECOVERY_PREFIX)
        ]

    def admission(self, path: Path) -> int:
        """Inspect a closed database without altering recovery state."""
        with contextlib.closing(store.connect_ro(path)) as conn:
            return conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0]

    def test_archive_collision_refuses_without_overwrite_or_retry(
        self,
    ) -> None:
        archive = self.home / f"{store.RECOVERY_PREFIX}collision"
        archive.write_bytes(b"earlier")
        archive.chmod(0o600)
        with mock.patch.object(platform_rebuild.uuid, "uuid4") as ident:
            ident.return_value.hex = "collision"
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (4, "store_unavailable"))
        self.assertEqual(ident.call_count, 1)
        self.assertEqual(archive.read_bytes(), b"earlier")
        self.assertIsNone(out["old_kept_as"])
        self.assertFalse((self.home / cli_rebuild.REBUILD).exists())
        self.assertEqual(self.admission(self.db), 0)
        self.assertTrue(out["recovery_required"])

    def test_public_source_is_refused_without_tightening(self) -> None:
        before = self.db.read_bytes()
        with public_read(self, self.db):
            code, out, _ = self.muninn("rebuild")
            self.assertEqual((code, out["reason"]), (2, "source_changed"))
            self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.asides(), [])
        self.assertFalse(out["publication_possible"])

    @contextlib.contextmanager
    def refuse_publication(self, *, after: bool) -> Generator[None]:
        """Fail around a real publication with no automatic restore avenue."""
        publish = platform_rebuild.publish_replacement
        attempts: list[Path] = []

        def fail(home: Path, db: Path, new: Path) -> None:
            attempts.append(new)
            if after:
                publish(home, db, new)
            raise PermissionError("synthetic publication failure")

        with mock.patch.object(platform_rebuild, "publish_replacement", fail):
            yield
        self.assertEqual(attempts, [self.home / cli_rebuild.REBUILD])

    def assert_recovery(
        self, out: dict[str, object], *, published: bool
    ) -> None:
        """Check actual retained content and uncertainty after failure."""
        self.assertTrue(out["recovery_required"])
        self.assertTrue(out["publication_possible"])
        self.assertNotIn("old_restored", out)
        self.assertEqual(self.unowned.read_bytes(), b"owner")
        self.assertEqual(len(self.asides()), 1)
        archive = self.asides()[0]
        self.assertEqual(out["old_kept_as"], archive.name)
        assert_private(self, archive)
        self.assertEqual(self.admission(archive), 0)
        self.assertEqual(self.admission(self.db), int(published))
        self.assertEqual(
            (self.home / cli_rebuild.REBUILD).exists(), not published
        )
        if not published:
            self.assertEqual(self.db.read_bytes(), archive.read_bytes())
        with contextlib.closing(store.connect_ro(self.db)) as conn:
            self.assertEqual(
                conn.execute("PRAGMA quick_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM knowledge_fts "
                    "WHERE knowledge_fts MATCH 'zebra'"
                ).fetchone()[0],
                1,
            )

    def test_failed_replace_preserves_old_and_complete_candidate(self) -> None:
        with self.refuse_publication(after=False):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (4, "replace_failed"))
        self.assert_recovery(out, published=False)
        before = {
            p: p.read_bytes()
            for p in (*self.asides(), self.db, self.home / cli_rebuild.REBUILD)
        }
        code, again, _ = self.muninn("rebuild")
        self.assertEqual(
            (code, again["reason"]), (2, "rebuild_candidate_exists")
        )
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_replace_then_raise_never_restores_or_reenables_live(self) -> None:
        before = self.db.stat().st_ino
        with self.refuse_publication(after=True):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (4, "replace_failed"))
        self.assertNotEqual(self.db.stat().st_ino, before)
        self.assert_recovery(out, published=True)
        archive = self.asides()[0]
        raw = archive.read_bytes()
        code, again, _ = self.muninn("rebuild")
        self.assertEqual(code, 0, again)
        self.assertEqual(archive.read_bytes(), raw)
        self.assertEqual(len(self.asides()), 2)

    def test_answer_reports_retained_archive_without_content(self) -> None:
        code, out, _ = self.muninn("rebuild")
        self.assertEqual(code, 0, out)
        self.assertIn("archive", out["warning"])
        self.assertNotIn("Please retain", str(out))
        self.assertNotIn("retained zebra", str(out))
        self.assertEqual(out["old_kept_as"], self.asides()[0].name)
        self.assertEqual(self.admission(self.db), 1)

    def test_failed_validation_retains_fenced_live_and_archive(self) -> None:
        with mock.patch.object(
            cli_rebuild,
            "_validate",
            side_effect=ValueError("quick_check_failed"),
        ):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["reason"]), (2, "quick_check_failed"))
        self.assertFalse(out["publication_possible"])
        self.assertTrue(out["recovery_required"])
        self.assertEqual(self.admission(self.db), 0)
        self.assertEqual(self.db.read_bytes(), self.asides()[0].read_bytes())
        self.assertTrue((self.home / cli_rebuild.REBUILD).is_file())

    def test_hot_journal_refuses_before_archive_or_candidate(self) -> None:
        before = self.db.read_bytes()
        journal = self.home / "muninn.sqlite-journal"
        journal.write_bytes(b"\0" * 512)
        code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["reason"]), (2, "source_changed"))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(journal.read_bytes(), b"\0" * 512)
        self.assertEqual(self.asides(), [])
        self.assertFalse((self.home / cli_rebuild.REBUILD).exists())

    def test_partial_archive_failure_preserves_enumerable_residue(
        self,
    ) -> None:
        def partial(source: BinaryIO, target: BinaryIO) -> None:
            target.write(b"partial")
            target.flush()
            raise OSError("synthetic copy interruption")

        with mock.patch.object(
            platform_rebuild.shutil, "copyfileobj", partial
        ):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (4, "store_unavailable"))
        self.assertIsNone(out["old_kept_as"])
        self.assertTrue(out["recovery_required"])
        self.assertEqual([p.read_bytes() for p in self.asides()], [b"partial"])
        self.assertEqual(self.admission(self.db), 0)
        self.assertFalse((self.home / cli_rebuild.REBUILD).exists())

    def test_own_open_ctime_drift_preserves_immutable_original(self) -> None:
        verify = cli_rebuild._verify_after_own_open
        originals: list[dict[str, object]] = []
        drift: list[bool] = []

        def observed(
            home: Path, original: cli_rebuild.fingerprint.Fingerprint
        ) -> None:
            before = dataclasses.asdict(original)
            if os.name != "nt":
                path = store.db_path(home)
                raw, prior = path.read_bytes(), path.stat()
                descriptor = os.open(
                    path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                )
                try:
                    os.fchmod(descriptor, 0o600)
                    after = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                self.assertNotEqual(after.st_ctime_ns, prior.st_ctime_ns)
                for field in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_uid",
                    "st_nlink",
                    "st_size",
                    "st_mtime_ns",
                ):
                    self.assertEqual(
                        getattr(after, field), getattr(prior, field), field
                    )
                self.assertEqual(path.read_bytes(), raw)
            current = cli_rebuild.fingerprint.capture(home)
            with (
                self.assertRaises(store.BusyError),
                store.writer_lock(home, wait_s=0),
            ):
                self.fail("final comparison has no writer")
            drift.append(
                current.database.ctime_ns != original.database.ctime_ns
            )
            verify(home, original)
            self.assertEqual(dataclasses.asdict(original), before)
            originals.append(before)

        with mock.patch.object(
            cli_rebuild, "_verify_after_own_open", observed
        ):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(originals), 2)
        self.assertTrue(all(item == originals[0] for item in originals))
        if os.name != "nt":
            self.assertEqual(drift, [True, True])

    def test_own_replay_or_other_post_open_changes_never_publish(self) -> None:
        for kind in (
            "row",
            "identity",
            "size",
            "mtime",
            "key",
            "intent",
            "journal",
            "wal",
            "shm",
        ):
            with self.subTest(kind=kind):
                home = self.tmp / ("final-" + kind)
                with contextlib.closing(store.connect_rw(store.db_path(home))):
                    pass
                initialize(home)
                verify, calls = q.verify_current, 0

                def changed(
                    owner: q.MaintenanceOwner,
                    conn: sqlite3.Connection,
                    kind: str = kind,
                    home: Path = home,
                    verify: Callable[
                        [q.MaintenanceOwner, sqlite3.Connection], None
                    ] = verify,
                ) -> None:
                    nonlocal calls
                    verify(owner, conn)
                    calls += 1
                    if calls != 2:
                        return
                    path = store.db_path(home)
                    if kind == "row":
                        conn.execute(
                            "INSERT INTO scope VALUES(99,'new','new','dir')"
                        )
                    elif kind == "identity":
                        # Inject after SQLite releases its Windows delete lock.
                        conn.close()
                        raw = path.read_bytes()
                        path.rename(home / "prior.sqlite")
                        path.write_bytes(raw)
                        path.chmod(0o600)
                    elif kind == "size":
                        with path.open("ab") as handle:
                            handle.write(b"changed")
                    elif kind == "mtime":
                        info = path.stat()
                        os.utime(
                            path,
                            ns=(info.st_atime_ns, info.st_mtime_ns + 1000000),
                        )
                    else:
                        name = {
                            "key": "tombstone.key",
                            "intent": "tombstones.jsonl",
                        }.get(kind, "muninn.sqlite-" + kind)
                        (home / name).write_bytes(b"new private state")
                        (home / name).chmod(0o600)

                with (
                    mock.patch.object(
                        q, "verify_current", side_effect=changed
                    ),
                    mock.patch.object(
                        platform_rebuild, "publish_replacement"
                    ) as publish,
                ):
                    code, out, _ = self.muninn(
                        "rebuild", env={"MUNINN_HOME": str(home)}
                    )
                self.assertEqual((code, out["reason"]), (2, "source_changed"))
                self.assertTrue(out["recovery_required"])
                self.assertFalse(out["publication_possible"])
                self.assertEqual(calls, 2)
                publish.assert_not_called()
                self.assertEqual(
                    len(list(home.glob(store.RECOVERY_PREFIX + "*"))), 1
                )

    def test_final_archive_identity_change_refuses_publication(self) -> None:
        validate = cli_rebuild._validate

        def replace(conn: sqlite3.Connection, control: sqlite3.Row) -> None:
            validate(conn, control)
            archive = self.asides()[0]
            raw = archive.read_bytes()
            archive.rename(archive.with_name("held-archive.sqlite"))
            archive.write_bytes(raw)
            archive.chmod(0o600)

        with mock.patch.object(cli_rebuild, "_validate", side_effect=replace):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["reason"]), (2, "source_changed"))
        self.assertFalse(out["publication_possible"])
        self.assertEqual(self.admission(self.db), 0)
