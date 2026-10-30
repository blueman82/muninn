"""Refuse unfinished old governed erasure before any migration artifact."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from unittest import mock

from install import cutover, decision_migration, recovery_guidance
from install.context import StepFailedError
from muninn import review_pause_state as state
from muninn import store, tombstones
from tests.policy_preview_v1_support import V1PreviewFixture


class MarkedV1ErasureReadinessTests(V1PreviewFixture):
    """Use frozen old DDL, actual locks and completed erasure witnesses."""

    def erased_source(self, *, clean: bool) -> None:
        """Persist old intent and database postconditions without new opens."""
        self.old("state")
        fd = state.acquire(self.ctx.data)
        try:
            state.seed_new(self.ctx.data, fd)
        finally:
            os.close(fd)
        row = tombstones.tombstone_row(
            "codex", "session", session_root="thread"
        )
        tombstones.append_tombstones(self.ctx.data, [row])
        with closing(sqlite3.connect(self.path)) as conn:
            columns = ",".join(row)
            marks = ",".join("?" for _ in row)
            conn.execute(
                f"INSERT INTO tombstone({columns}) VALUES({marks})",
                tuple(row.values()),
            )
            conn.execute(
                "UPDATE review_evidence SET excerpt=NULL,state='erased'"
            )
            if clean:
                conn.execute(
                    "UPDATE knowledge_version SET "
                    "content_json=NULL,reason=NULL,expiry_json=NULL,erased_at=4.0"
                )
                conn.execute(
                    "UPDATE knowledge_claim SET "
                    "text=NULL,dependency_keys_json='[]',state='erased'"
                )
                conn.execute(
                    "UPDATE knowledge SET state='erased',retracted_reason=NULL"
                )
                conn.execute(
                    "UPDATE unresolved SET item_json=NULL,state='erased'"
                )
                conn.execute(
                    "UPDATE review_request SET "
                    "state='cancelled',current_attempt_id=NULL,retry_at=NULL,cancel_code='evidence_erased'"
                )
            conn.commit()
        self.ctx.dry_run = False

    def assert_before_archive_refusal(self) -> None:
        """Check recovery and cutover refuse without source writes."""
        raw, rows = self.path.read_bytes(), self.rows()
        witness = (self.ctx.data / state.WITNESS).read_bytes()
        entries = {item.name for item in self.ctx.data.iterdir()}
        self.ctx.rdir.mkdir(parents=True, exist_ok=True)
        with (
            mock.patch.object(
                decision_migration.platform_rebuild,
                "retain_upgrade_store",
                side_effect=AssertionError("archive must not start"),
            ),
            mock.patch.object(
                decision_migration,
                "_candidate",
                side_effect=AssertionError("candidate must not start"),
            ),
        ):
            for operation in (
                lambda: recovery_guidance.recover_pause(self.ctx, self.rec),
                lambda: cutover.prepare_store(self.ctx, self.rec),
            ):
                with self.assertRaisesRegex(
                    StepFailedError, "^marked_v1_recovery_required$"
                ):
                    operation()
                self.assertEqual(self.path.read_bytes(), raw)
                self.assertEqual(self.rows(), rows)
                self.assertEqual(
                    (self.ctx.data / state.WITNESS).read_bytes(), witness
                )
                self.assertEqual(
                    {item.name for item in self.ctx.data.iterdir()}, entries
                )
                self.assertNotIn("store", self.rec)

    def test_scrubbed_database_with_retained_attempt_output_refuses(
        self,
    ) -> None:
        self.erased_source(clean=True)
        output = (
            self.ctx.data
            / "review-work"
            / ("A_" + "1" * 32)
            / "pending-output.json"
        )
        output.parent.parent.mkdir(mode=0o700)
        output.parent.mkdir(mode=0o700)
        output.write_bytes(b"unfinished governed private output")
        self.assert_before_archive_refusal()
        self.assertEqual(
            output.read_bytes(), b"unfinished governed private output"
        )

    def test_outside_window_admitted_dependents_require_old_recovery(
        self,
    ) -> None:
        self.erased_source(clean=False)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "UPDATE review_request SET "
                "provider='claude',thread_id='other',session_root='other'"
            )
            conn.commit()
        self.assert_before_archive_refusal()

    def test_already_completed_old_erasure_still_migrates(self) -> None:
        self.erased_source(clean=True)
        recovery_guidance.recover_pause(self.ctx, self.rec)
        self.ctx.rdir.mkdir(parents=True, exist_ok=True)
        raw = self.path.read_bytes()
        cutover.prepare_store(self.ctx, self.rec)
        with closing(store.connect_ro(self.path)) as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 2
            )
            self.assertEqual(
                conn.execute(
                    "SELECT content_json,reason,erased_at FROM "
                    "knowledge_version"
                ).fetchone()[:],
                (None, None, 4.0),
            )
        self.assertEqual(
            decision_migration.Path(self.rec["store"]["archive"]).read_bytes(),
            raw,
        )

    def test_remaining_derived_index_refuses_before_archive(self) -> None:
        self.erased_source(clean=True)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "INSERT INTO knowledge_fts "
                "VALUES(?,1,'retained derived body')",
                ("K_" + "1" * 32,),
            )
            conn.commit()
        self.assert_before_archive_refusal()

    def test_attempt_symlink_refuses_without_following_or_cleanup(
        self,
    ) -> None:
        self.erased_source(clean=True)
        outside = self.home / "outside-work"
        outside.mkdir(mode=0o700)
        sentinel = outside / "keep.json"
        sentinel.write_bytes(b"independent output")
        parent = self.ctx.data / "review-work"
        parent.mkdir(mode=0o700)
        (parent / ("A_" + "1" * 32)).symlink_to(
            outside, target_is_directory=True
        )
        self.assert_before_archive_refusal()
        self.assertEqual(sentinel.read_bytes(), b"independent output")
