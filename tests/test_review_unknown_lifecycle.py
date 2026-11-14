"""Keep accepted uncertainty through public commands and store maintenance."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import sqlite3
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

from install import installer
from muninn import (
    cli,
    knowledge_erase,
    review_recovery,
    review_state,
    review_unknown_binding,
    review_unknown_state,
    store,
    store_copy,
)
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from muninn.review_supervisor import Supervisor
from tests.installer_support import World, git
from tests.unknown_recovery_support import UnknownRecoveryCase


class UnknownLifecycleTests(UnknownRecoveryCase):
    """Exercise the public surface and retained governed metadata."""

    def test_public_cli_returns_the_persisted_acknowledgement(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(
                [
                    "learn",
                    "recover-unknown",
                    "--attempt",
                    self.context.attempt_id,
                    "--round",
                    "1",
                    "--accept-uncertainty",
                ]
            )
        self.assertEqual(code, 0)
        value = json.loads(output.getvalue())
        self.assertTrue(value["accept_uncertainty"])
        self.assertRegex(value["acknowledgement_id"], r"^UA_[0-9a-f]{32}$")
        stored = review_unknown_state.read(
            self.fixture.conn, self.context.attempt_id, 1
        )
        assert stored is not None
        self.assertEqual(
            value["acknowledgement_id"], stored.acknowledgement_id
        )

    def test_copy_preserves_exact_acknowledgement_and_unknown_call(
        self,
    ) -> None:
        acknowledgement = self.recover()
        with closing(
            sqlite3.connect(":memory:", isolation_level=None)
        ) as target:
            target.row_factory = sqlite3.Row
            store.initialize_store(target)
            counts = store_copy.copy_governed_state(self.fixture.conn, target)
            self.assertEqual(counts["review_unknown_ack"], 1)
            self.assertEqual(
                review_unknown_state.read(target, self.context.attempt_id, 1),
                acknowledgement,
            )
            self.assertEqual(
                tuple(target.execute("SELECT * FROM review_call").fetchone()),
                tuple(
                    self.fixture.conn.execute(
                        "SELECT * FROM review_call"
                    ).fetchone()
                ),
            )

    def test_erasure_retains_metadata_without_granting_a_stale_exemption(
        self,
    ) -> None:
        acknowledgement = self.recover()
        plan = knowledge_erase.support_closure(
            self.fixture.conn, (), request_ids=(self.context.request_id,)
        )
        self.fixture.transaction(
            lambda: knowledge_erase.erase_support_closure(
                self.fixture.conn, plan, 21.0
            )
        )
        self.assertEqual(
            review_unknown_state.read(
                self.fixture.conn, self.context.attempt_id, 1
            ),
            acknowledgement,
        )
        self.assertFalse(
            review_unknown_binding.matches(self.fixture.conn, acknowledgement)
        )
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT state FROM review_call"
            ).fetchone()[0],
            "unknown",
        )

    def test_startup_preserves_open_admission_after_historical_guard_notice(
        self,
    ) -> None:
        self.fixture.transaction(
            lambda: review_recovery.record(
                self.fixture.conn,
                "setup",
                "process_identity_unverified",
                "recovery",
                0.0,
            )
        )
        self.recover()
        supervisor = Supervisor(self.fixture.home, self.fixture.policy)
        supervisor.start(22.0)
        self.addCleanup(supervisor.stop, 24.0)
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            1,
        )
        self.assertIsNone(
            self.fixture.conn.execute(
                "SELECT 1 FROM review_issue WHERE id='setup'"
            ).fetchone()
        )
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT state FROM review_request"
            ).fetchone()[0],
            "failed",
        )

    def test_upgrade_preserves_closed_admission_and_unknown_history(
        self,
    ) -> None:
        world = World(self)
        installer.install(world.ctx(fresh=True), world.repo, world.sha)
        root = Path(__file__).resolve().parents[1]
        for name in (
            "review_runtime_models.py",
            "review_codex_policy.py",
            "review_models.py",
        ):
            relative = Path("muninn") / name
            (world.repo / relative).write_bytes((root / relative).read_bytes())
        (world.repo / "bin/note").write_text("next\n")
        git(world.repo, "add", "-A")
        git(world.repo, "commit", "-qm", "upgrade")
        sha = git(world.repo, "rev-parse", "HEAD").decode().strip()
        ctx = dataclasses.replace(
            world.ctx(upgrade=True), ts="20261114T020000Z"
        )
        ctx.data = self.fixture.home
        ctx.learning_prompt = mock.Mock(return_value="no")
        before = {
            name: [
                tuple(row)
                for row in self.fixture.conn.execute(f"SELECT * FROM {name}")
            ]
            for name in ("review_call", "review_attempt", "review_request")
        }
        policy = tuple(
            self.fixture.conn.execute(
                "SELECT policy_json,policy_digest FROM review_control"
            ).fetchone()
        )

        def heartbeat() -> None:
            path = ctx.data / "status.json"
            path.write_text("{}")
            os.utime(path, (world.fake.now(), world.fake.now()))

        with mock.patch.object(
            world.fake, "_heartbeat", side_effect=heartbeat
        ):
            result = installer.install(ctx, world.repo, sha)
        self.assertEqual(result["sha"], sha)
        self.assertFalse(ctx.enable_learning)
        self.assertEqual(
            self.fixture.conn.execute(
                "SELECT admission FROM review_control"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            tuple(
                self.fixture.conn.execute(
                    "SELECT policy_json,policy_digest FROM review_control"
                ).fetchone()
            ),
            policy,
        )
        for name, rows in before.items():
            self.assertEqual(
                [
                    tuple(row)
                    for row in self.fixture.conn.execute(
                        f"SELECT * FROM {name}"
                    )
                ],
                rows,
            )
        self.assertTrue(
            review_state.available(self.fixture.conn, "review_unknown_ack")
        )

    def test_rebuild_preserves_acknowledgement_and_unknown_charge(
        self,
    ) -> None:
        acknowledgement = self.recover()
        before = tuple(
            self.fixture.conn.execute("SELECT * FROM review_call").fetchone()
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["rebuild"])
        self.assertEqual(code, 0, output.getvalue())
        with closing(
            store.connect_ro(store.db_path(self.fixture.home))
        ) as conn:
            self.assertEqual(
                review_unknown_state.read(conn, self.context.attempt_id, 1),
                acknowledgement,
            )
            self.assertEqual(
                tuple(conn.execute("SELECT * FROM review_call").fetchone()),
                before,
            )
            self.assertTrue(
                review_unknown_binding.matches(conn, acknowledgement)
            )

    def test_auxiliary_records_are_immutable_and_cascade_with_target(
        self,
    ) -> None:
        acknowledgement = self.recover()
        with self.assertRaises(sqlite3.IntegrityError):
            self.fixture.conn.execute(
                "UPDATE review_unknown_ack SET acknowledged_at=22.0"
            )
        self.fixture.conn.execute(
            "DELETE FROM review_call WHERE attempt_id=?",
            (self.context.attempt_id,),
        )
        self.assertIsNone(
            review_unknown_state.read(
                self.fixture.conn, self.context.attempt_id, 1
            )
        )
        self.assertIsNotNone(acknowledgement.acknowledgement_id)

    def test_later_authorized_policy_keeps_past_acceptance_without_reopening(
        self,
    ) -> None:
        acknowledgement = self.recover()
        previous = self.fixture.policy
        later = previous.model_copy(
            update={"max_input_bytes": previous.max_input_bytes - 1}
        )
        later = ReviewPolicy.model_validate_json(canonical_json(later))
        self.fixture.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?,"
            "admission=0,generation=generation+1",
            (canonical_json(later), digest(later)),
        )
        before = tuple(
            self.fixture.conn.execute(
                "SELECT * FROM review_control"
            ).fetchone()
        )
        self.assertTrue(
            review_unknown_binding.matches(self.fixture.conn, acknowledgement)
        )
        self.assertEqual(self.recover(), acknowledgement)
        self.assertEqual(
            tuple(
                self.fixture.conn.execute(
                    "SELECT * FROM review_control"
                ).fetchone()
            ),
            before,
        )
