"""Qualify raw source archives and safe marked-one migration boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import cutover, recovery_guidance, snapshot
from install import decision_migration as migration
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import platform_rebuild, store, store_copy, tombstones
from muninn import review_pause as q
from muninn import review_pause_state as state
from tests.policy_preview_support import states
from tests.policy_preview_v1_support import V1PreviewFixture


class DecisionMigrationTests(V1PreviewFixture):
    """Use real owner/writer scopes without native service effects."""

    def seed(self) -> None:
        """Certify the fixture's never-exposed contained source before use."""
        fd = state.acquire(self.ctx.data)
        try:
            state.seed_new(self.ctx.data, fd)
        finally:
            os.close(fd)

    def migrate(self) -> None:
        """Drive actual upgrade source discrimination and publication."""
        self.ctx.dry_run = False
        self.ctx.rdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with cutover.prepared_store(self.ctx, self.rec):
            pass

    def test_migrate_both_marked_v1_shapes_preserves_identity(self) -> None:
        for shape in ("base", "state"):
            with self.subTest(shape=shape):
                self.doCleanups()
                self.setUp()
                self.old(shape)
                self.seed()
                raw, rows = self.path.read_bytes(), self.rows()
                self.migrate()
                with closing(store.connect_ro(self.path)) as conn:
                    self.assertEqual(store_copy.validate_source(conn), 2)
                    self.assertEqual(
                        conn.execute(
                            "SELECT candidate_id,decision_revision "
                            "FROM review_request"
                        ).fetchall()[0][:],
                        (None, None),
                    )
                    self.assertEqual(
                        tuple(
                            conn.execute(
                                "SELECT id,duplicate_key,created_at "
                                "FROM review_request"
                            ).fetchone()
                        ),
                        ("R_" + "1" * 32, "old-key", 1.5),
                    )
                archive = self.ctx.data / self.rec["store"]["archive"]
                self.assertEqual(archive.read_bytes(), raw)
                with closing(sqlite3.connect(archive)) as source:
                    self.assertEqual(tuple(source.iterdump()), rows)
                    self.assertEqual(
                        source.execute("PRAGMA user_version").fetchone()[0], 1
                    )

    def test_unknown_shape_fails_closed(self) -> None:
        self.old("state")
        self.seed()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("CREATE TABLE unexpected(value)")
            conn.commit()
        files, rows = states(self.home), self.rows()
        with self.assertRaises(StepFailedError):
            self.migrate()
        self.assertEqual(self.rows(), rows)
        self.assertEqual(
            self.path.read_bytes(),
            files[str(self.path.relative_to(self.home))][1],
        )
        self.assertNotIn("store", self.rec)

    def test_marked_one_recovery_requires_complete_witness_and_erasure(
        self,
    ) -> None:
        for condition in (
            "healthy",
            "missing",
            "interrupted",
            "baseline",
            "pending",
        ):
            with self.subTest(condition=condition):
                self.doCleanups()
                self.setUp()
                self.old("state")
                if condition != "missing":
                    self.seed()
                if condition == "interrupted":
                    witness = self.ctx.data / state.WITNESS
                    raw = witness.read_text().replace('"complete"', '"active"')
                    witness.write_text(raw)
                    witness.chmod(0o600)
                elif condition == "baseline":
                    (self.ctx.data / state.BASELINE).write_text("unverified")
                    (self.ctx.data / state.BASELINE).chmod(0o600)
                elif condition == "pending":
                    tombstones.append_tombstones(
                        self.ctx.data,
                        [
                            tombstones.tombstone_row(
                                "codex", "session", session_root="thread"
                            )
                        ],
                    )
                self.ctx.dry_run = False
                before, rows = self.path.read_bytes(), self.rows()
                messages: list[str] = []
                with (
                    mock.patch.object(self.ctx, "say", messages.append),
                    mock.patch.object(
                        recovery_guidance.cli_recovery,
                        "recover",
                        side_effect=AssertionError(
                            "current recovery must not open v1"
                        ),
                    ),
                ):
                    if condition == "healthy":
                        recovery_guidance.recover_pause(self.ctx, self.rec)
                    else:
                        with self.assertRaisesRegex(
                            StepFailedError, "marked_v1_recovery_required"
                        ):
                            recovery_guidance.recover_pause(self.ctx, self.rec)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.rows(), rows)
                self.assertEqual(
                    len(messages), 0 if condition == "healthy" else 3
                )
                if messages:
                    self.assertIn("marked-v1 pause or erasure", messages[0])
                    self.assertIn(
                        "verified v1 installed muninn recover", messages[1]
                    )
                    self.assertIn("Keep the old release", messages[2])
                    self.assertIn("all data unchanged", messages[2])

    def test_failure_after_publication_retains_actual_current_and_raw_archive(
        self,
    ) -> None:
        self.old("state")
        self.seed()
        raw = self.path.read_bytes()
        publish = platform_rebuild.publish_replacement

        def lost_response(home: Path, db: Path, new: Path) -> None:
            publish(home, db, new)
            raise OSError("lost publication response")

        with (
            mock.patch.object(
                platform_rebuild, "publish_replacement", lost_response
            ),
            self.assertRaises(OSError),
        ):
            self.migrate()
        with closing(store.connect_ro(self.path)) as conn:
            self.assertEqual(store_copy.validate_source(conn), 2)
        self.assertEqual(Path(self.rec["store"]["archive"]).read_bytes(), raw)
        self.assertEqual(self.rec["store"]["phase"], "migration_possible")
        self.rec["snapshot"] = {
            "name": Path(self.rec["store"]["archive"]).name
        }
        with self.assertRaisesRegex(
            StepFailedError, "archive_restore_requires_review"
        ):
            snapshot.undo_store(self.ctx, self.rec)

    def test_old_runtime_refuses_v2(self) -> None:
        self.old("state")
        target = self.ctx.data / "populated-two.sqlite"
        with (
            closing(
                sqlite3.connect(self.path, isolation_level=None)
            ) as source,
            closing(store.connect_rw(target)) as conn,
        ):
            store_copy.copy_governed_state(source, conn)
            rows = tuple(conn.iterdump())
        frozen = Path(__file__).parent / "fixtures/installer/marked-v1-runtime"
        manifest = json.loads((frozen / "manifest.json").read_text())
        self.assertEqual(
            manifest["commit"], "63e9a62ab4a546456ac59f6a2d324b842ac1027e"
        )
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary).resolve()
            for name, info in manifest["files"].items():
                raw = (frozen / (name + ".txt")).read_bytes()
                self.assertEqual(len(raw), info["bytes"])
                self.assertEqual(
                    hashlib.sha256(raw).hexdigest(), info["sha256"]
                )
                self.assertEqual(
                    hashlib.sha1(
                        b"blob " + str(len(raw)).encode() + b"\0" + raw
                    ).hexdigest(),
                    info["blob"],
                )
                destination = package / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
            runner = package / "probe.py"
            runner.write_text("""import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from muninn import store
old_file = Path(__file__).parent / "muninn/store.py"
assert Path(store.__file__).resolve() == old_file
assert store.SCHEMA_VERSION == 1
opener = getattr(store, sys.argv[1])
old_error = opener.__globals__["StoreUnavailableError"]
assert old_error is store.StoreUnavailableError
try:
    connection = opener(Path(sys.argv[2]))
except store.StoreUnavailableError as error:
    assert str(error) == "cutover_required", str(error)
    print("actual-old-runtime-refused:" + sys.argv[1])
else:
    connection.close()
    raise AssertionError("old runtime accepted populated v2")
""")
            environment = {
                key: value
                for key, value in os.environ.items()
                if key not in ("PYTHONPATH", "PYTHONHOME")
            }
            environment.update(
                HOME=str(package), MUNINN_HOME=str(package / "home")
            )
            for operation in ("connect_ro", "connect_rw"):
                raw = target.read_bytes()
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(runner),
                        operation,
                        str(target),
                    ],
                    cwd=package,
                    env=environment,
                    capture_output=True,
                    check=False,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertIn(
                    "actual-old-runtime-refused:" + operation,
                    result.stdout.decode(),
                )
                self.assertEqual(target.read_bytes(), raw)
                with closing(sqlite3.connect(target)) as conn:
                    self.assertEqual(tuple(conn.iterdump()), rows)

    def test_failures_before_and_after_publication_preserve_actual_data(
        self,
    ) -> None:
        for phase in (
            "archive",
            "copy",
            "verify_published",
            "record_published",
        ):
            with self.subTest(phase=phase):
                self.doCleanups()
                self.setUp()
                self.old("state")
                self.seed()
                original = self.path.read_bytes()
                saved = migration.record.save
                verified = migration.q.verify_current

                def save(
                    ctx: Ctx,
                    rec: Record,
                    saved: Callable[[Ctx, Record], None] = saved,
                ) -> None:
                    saved(ctx, rec)
                    if rec.get("store", {}).get("phase") == "current_prepared":
                        raise OSError("lost durable record response")

                def verify(
                    owner: q.MaintenanceOwner,
                    conn: sqlite3.Connection,
                    verified: Callable[
                        [q.MaintenanceOwner, sqlite3.Connection], None
                    ] = verified,
                ) -> None:
                    verified(owner, conn)
                    raise OSError("postpublication verify response lost")

                target, name, replacement = (
                    (
                        platform_rebuild,
                        "retain_upgrade_store",
                        mock.Mock(side_effect=OSError("archive")),
                    )
                    if phase == "archive"
                    else (
                        (
                            store_copy,
                            "copy_governed_state",
                            mock.Mock(side_effect=OSError("copy")),
                        )
                        if phase == "copy"
                        else (
                            (migration.q, "verify_current", verify)
                            if phase == "verify_published"
                            else (migration.record, "save", save)
                        )
                    )
                )
                with (
                    mock.patch.object(target, name, replacement),
                    self.assertRaises(OSError),
                ):
                    self.migrate()
                with closing(sqlite3.connect(self.path)) as conn:
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(
                    version, 1 if phase in ("archive", "copy") else 2
                )
                if version == 1:
                    self.assertEqual(self.path.read_bytes(), original)
                if phase != "archive":
                    self.assertEqual(
                        Path(self.rec["store"]["archive"]).read_bytes(),
                        original,
                    )
                if phase == "record_published":
                    durable = json.loads(
                        (self.ctx.rdir / "rollback-record.json").read_text()
                    )
                    self.assertEqual(
                        durable["store"]["phase"], "current_prepared"
                    )

    def test_newer_source_or_tombstone_refuses_stale_publication(self) -> None:
        for changed in ("source", "journal"):
            with self.subTest(changed=changed):
                self.doCleanups()
                self.setUp()
                self.old("state")
                self.seed()
                copy = store_copy.copy_governed_state

                def intervene(
                    source: sqlite3.Connection,
                    target: sqlite3.Connection,
                    copy: Callable[
                        [sqlite3.Connection, sqlite3.Connection],
                        dict[str, int],
                    ] = copy,
                    changed: str = changed,
                ) -> dict[str, int]:
                    result = copy(source, target)
                    if changed == "journal":
                        tombstones.append_tombstones(
                            self.ctx.data,
                            [
                                tombstones.tombstone_row(
                                    "codex", "session", session_root="thread"
                                )
                            ],
                        )
                    else:
                        with closing(sqlite3.connect(self.path)) as writer:
                            writer.execute(
                                "INSERT INTO scope(key,label,kind) "
                                "VALUES('newer','newer','dir')"
                            )
                            writer.commit()
                    return result

                with (
                    mock.patch.object(
                        store_copy, "copy_governed_state", intervene
                    ),
                    self.assertRaisesRegex(ValueError, "source_changed"),
                ):
                    self.migrate()
                with closing(sqlite3.connect(self.path)) as conn:
                    self.assertEqual(
                        conn.execute("PRAGMA user_version").fetchone()[0], 1
                    )
                    if changed == "source":
                        self.assertEqual(
                            conn.execute(
                                "SELECT label FROM scope WHERE key='newer'"
                            ).fetchone()[0],
                            "newer",
                        )
                self.assertEqual(
                    self.rec["store"]["phase"], "marked_v1_archived"
                )
                self.assertTrue(Path(self.rec["store"]["archive"]).is_file())
