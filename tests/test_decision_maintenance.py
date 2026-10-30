"""Preserve governed erasure through actual copy and rebuild publication."""

from __future__ import annotations

import json
from contextlib import closing

from install import snapshot
from install.context import Ctx, StepFailedError
from muninn import cli_rebuild, erase, store, store_copy, tombstones
from muninn.decision_candidates import candidate_view, show_candidate
from tests.decision_support import DecisionCase
from tests.policy_preview_support import no_command
from tests.review_pause_support import initialize


class DecisionMaintenanceTests(DecisionCase):
    """Keep bodies absent and historical proof identity after maintenance."""

    def test_owner_erasure_survives_copy_rebuild_and_rollback(self) -> None:
        draft = self.stage()
        intent = self.intent(draft.id)
        proof = self.approve(intent)
        receipt = self.saved_fixture(draft.id)
        original = self.path.read_bytes()
        archive = self.home / (store.PRE_UPGRADE_PREFIX + "raw-history")
        archive.write_bytes(store.db_path(self.home).read_bytes())
        archive.chmod(0o600)
        out = erase.erase(
            self.conn,
            home=self.home,
            event_ref=f"codex:{proof.owner_window.source.thread_id}:"
            f"{intent.owner_locator.line}.1",
            env={"HOME": str(self.tmp)},
        )
        review = out["review"]
        assert isinstance(review, dict)
        self.assertEqual(review["retained_archives"], 1)
        self.assertEqual(self.path.read_bytes(), original)
        erased = candidate_view(self.conn, draft.id)
        self.assertEqual(
            (erased.state, erased.candidate, erased.receipt),
            ("erased", None, receipt),
        )
        with closing(store.connect_rw(self.home / "copied.sqlite")) as target:
            store_copy.copy_governed_state(self.conn, target)
            self.assertEqual(candidate_view(target, draft.id), erased)
            tombstones.reapply_tombstones(target, self.home)
            self.assertEqual(candidate_view(target, draft.id), erased)
            self.assertEqual(
                target.execute("PRAGMA foreign_key_check").fetchall(), []
            )
        self.conn.close()
        initialize(self.home)
        env = {
            "HOME": str(self.tmp),
            "MUNINN_ROOTS": json.dumps(
                {key: str(value) for key, value in self.roots.items()}
            ),
        }
        rebuilt = cli_rebuild._run(env, self.home, cli_rebuild._Status())
        self.assertTrue(rebuilt["rebuilt"])
        with closing(store.connect_ro(store.db_path(self.home))) as conn:
            self.assertEqual(candidate_view(conn, draft.id), erased)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM knowledge_version "
                    "WHERE content_json IS NOT NULL"
                ).fetchone()[0],
                0,
            )
            with self.assertRaisesRegex(ValueError, "decision_erased"):
                show_candidate(conn, draft.id, 65536)
        ctx = Ctx(self.tmp, no_command, "rollback-fixture", upgrade=True)
        ctx.data = self.home
        with self.assertRaisesRegex(
            StepFailedError, "archive_restore_requires_review"
        ):
            snapshot.undo_store(ctx, {"snapshot": {"name": archive.name}})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(archive.is_file())

    def test_copy_and_vacuum_keep_declared_identity_and_display(self) -> None:
        first = self.stage()
        self.approve(self.intent(first.id))
        second = self.stage(
            self.result([self.note(content="A second distinct draft")])
        )
        before = {
            ident: (candidate_view(self.conn, ident), self.display(ident))
            for ident in (first.id, second.id)
        }
        self.conn.execute("UPDATE memory_candidate SET rowid=rowid+100")
        with closing(store.connect_rw(self.home / "copy.sqlite")) as target:
            store_copy.copy_governed_state(self.conn, target)
            target.execute("VACUUM")
            self.assertEqual(
                target.execute(
                    "SELECT min(rowid) FROM memory_candidate"
                ).fetchone()[0],
                1,
            )
            for ident, (view, display) in before.items():
                self.assertEqual(candidate_view(target, ident), view)
                self.assertEqual(
                    show_candidate(target, ident, 65536).text, display
                )
