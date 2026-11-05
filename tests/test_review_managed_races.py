"""Check positive pre-exec refusal without duplicating a native execution."""

from __future__ import annotations

import struct
import time
from pathlib import Path
from unittest import TestCase, mock

from muninn import (
    review_budget,
    review_worker,
)
from muninn import review_process_protocol as protocol
from muninn import (
    review_round_launch as round_launch,
)
from muninn import (
    review_runtime_records as records,
)
from muninn import review_supervisor_result as supervisor_result
from muninn.review_launch_spec import LaunchSpec
from muninn.review_models import ProcessIdentity, ReviewContext
from muninn.review_round_state import Round
from muninn.review_runtime_models import (
    ActivationRuntime,
    ManagedCallRecord,
    ManagedCapability,
    RuntimeIdentity,
)
from tests.managed_codex_support import identity
from tests.test_review_managed_records import ManagedRecordTests


class ManagedRaceTests(TestCase):
    """Fake only native creation and pipe I/O at the owned wrapper boundary."""

    def worker(self) -> review_worker._Worker:
        """Create finite wrapper state without changing actual process FDs."""
        with (
            mock.patch("os.set_blocking"),
            mock.patch.object(
                review_worker,
                "identify",
                return_value=ProcessIdentity(
                    pid=100,
                    created="fixture",
                    pgid=100,
                    job_marker=None,
                ),
            ),
        ):
            worker = review_worker._Worker(time.monotonic() + 10, 8192, 4096)
        worker.outgoing.clear()
        return worker

    def test_owned_enoent_emits_positive_refusal(self) -> None:
        observed = identity(Path("/fixture/manager"))
        worker = self.worker()
        worker.incoming.extend(
            protocol.activation(
                (observed.requested.executable, "exec"),
                Path("/fixture/cwd"),
                {},
                b"prompt",
                8192,
                runtime=ActivationRuntime(execution=observed),
            )
        )
        with (
            mock.patch("os.read", side_effect=BlockingIOError),
            mock.patch(
                "muninn.review_managed_files.matches", return_value=True
            ),
            mock.patch("subprocess.Popen", side_effect=FileNotFoundError),
        ):
            worker._launch()
        self.assertIsNone(worker.cli)
        self.assertEqual(worker.outgoing[:1], b"P")
        size = struct.unpack("!I", worker.outgoing[1:5])[0]
        notice = protocol.PrestartRefusalNotice.model_validate_json(
            bytes(worker.outgoing[5 : 5 + size])
        )
        self.assertEqual(notice.code, "exec_enoent")

    def test_permission_error_is_not_positive_updater_race(self) -> None:
        observed = identity(Path("/fixture/manager"))
        worker = self.worker()
        worker.incoming.extend(
            protocol.activation(
                (observed.requested.executable, "exec"),
                Path("/fixture/cwd"),
                {},
                b"prompt",
                8192,
                runtime=ActivationRuntime(execution=observed),
            )
        )
        with (
            mock.patch("os.read", side_effect=BlockingIOError),
            mock.patch(
                "muninn.review_managed_files.matches", return_value=True
            ),
            mock.patch("subprocess.Popen", side_effect=PermissionError),
            self.assertRaises(PermissionError),
        ):
            worker._launch()
        self.assertEqual(worker.outgoing, b"")

    def test_legacy_activation_bytes_have_no_managed_defaults(self) -> None:
        raw = protocol.activation(
            ("/exact/client",), Path("/cwd"), {}, b"x", 10
        )
        header = struct.unpack("!I", raw[8:12])[0]
        self.assertEqual(
            raw[12 : 12 + header],
            b'{"argv":["/exact/client"],"cwd":"/cwd","env":{}}',
        )


class RoundRefreshTests(ManagedRecordTests):
    """Carry one refresh across reservation and worker readiness."""

    def capability(self) -> ManagedCapability:
        """Supply fixture authority; launch discovery is faked separately."""
        return ManagedCapability(
            kind="codex-managed-capability-v1",
            requested=self.selected,
            proof_path=self.home / "fixture-proof.json",
            schema_digest="a" * 64,
            config_digest="b" * 64,
        )

    def spec(
        self, observed: RuntimeIdentity, context: ReviewContext
    ) -> LaunchSpec:
        """Describe a nonexecuted launch with its own runtime facts."""
        return LaunchSpec(
            ("/fake/wrapper",),
            self.home,
            {},
            (observed.requested.executable, "exec"),
            self.home,
            {},
            b"{}",
            context.deadline,
            execution=observed,
        )

    def test_late_update_uses_one_reservation_and_one_total_refresh(
        self,
    ) -> None:
        context = self.context().model_copy(
            update={"deadline": time.monotonic() + 100}
        )
        pending = Round(context, {}, {})
        current = [identity(self.selected.manager_root)]

        def prepare(
            context: ReviewContext, capability: object, prompt: bytes
        ) -> LaunchSpec:
            return self.spec(current[0], context)

        with (
            mock.patch.object(
                round_launch, "prepare_launch", side_effect=prepare
            ) as discovery,
            mock.patch(
                "muninn.review_managed_files.matches",
                side_effect=lambda observed: observed == current[0],
            ),
        ):
            first = round_launch.prepare(pending, self.capability())
            self.transaction(
                lambda: review_budget.reserve_call(
                    self.conn, context, 13.0, execution=first.execution
                )
            )
            current[0] = identity(self.selected.manager_root, "0.167.0")
            second = round_launch.prepare(pending, self.capability())
            self.assertEqual(
                round_launch.prepare(pending, self.capability()), second
            )
            self.transaction(
                lambda: records.offer_execution(
                    self.conn, context, current[0], 14.0
                )
            )
            current[0] = identity(self.selected.manager_root, "0.168.0")
            with self.assertRaisesRegex(ValueError, "runtime_race_exhausted"):
                round_launch.prepare(pending, self.capability())
            self.assertEqual(discovery.call_count, 2)
        row = self.conn.execute("SELECT * FROM review_call").fetchone()
        record = ManagedCallRecord.model_validate_json(row["requested_json"])
        self.assertEqual(
            [item.requested.version for item in record.candidates],
            ["0.166.0", "0.167.0"],
        )
        self.assertEqual(record.refresh_count, 1)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )

    def test_completed_round_gets_distinct_record_and_new_call_refresh_budget(
        self,
    ) -> None:
        context = self.context()
        pending = Round(context, {}, {}, refresh_count=1)
        first, second = identity(self.selected.manager_root), identity(
            self.selected.manager_root, "0.167.0"
        )
        self.transaction(
            lambda: review_budget.reserve_call(
                self.conn, context, 13.0, execution=first
            )
        )
        self.transaction(
            lambda: records.offer_execution(self.conn, context, first, 14.0)
        )
        self.transaction(
            lambda: records.freeze_launched_execution(
                self.conn, context, first.digest, 15.0
            )
        )
        self.conn.execute(
            "UPDATE review_call SET state='completed',launched_at=15.0,ended_"
            "at=16.0"
        )
        original = self.conn.execute(
            "SELECT requested_json FROM review_call"
        ).fetchone()[0]
        supervisor_result._advance(pending, context)
        self.assertEqual(pending.refresh_count, 0)
        self.transaction(
            lambda: review_budget.reserve_call(
                self.conn, pending.context, 17.0, execution=second
            )
        )
        self.transaction(
            lambda: records.offer_execution(
                self.conn, pending.context, second, 18.0
            )
        )
        self.transaction(
            lambda: records.freeze_launched_execution(
                self.conn, pending.context, second.digest, 19.0
            )
        )
        rows = self.conn.execute(
            "SELECT requested_json,policy_digest,budget_account_id FROM revie"
            "w_call ORDER BY round_index"
        ).fetchall()
        self.assertEqual(rows[0][0], original)
        self.assertEqual(
            ManagedCallRecord.model_validate_json(
                rows[1][0]
            ).launched_identity,
            second.digest,
        )
        self.assertEqual(rows[0][1:], rows[1][1:])
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt"
            ).fetchone()[0],
            1,
        )

    def test_setup_recheck_cannot_extend_original_managed_deadline(
        self,
    ) -> None:
        context = self.context()
        pending = Round(context, {}, {})
        with mock.patch("time.monotonic", return_value=1000.0):
            round_launch.resume_setup(pending)
        self.assertEqual(pending.context.deadline, context.deadline)

    def test_failed_fresh_negotiation_consumes_one_persisted_refresh(
        self,
    ) -> None:
        context = self.context().model_copy(
            update={"deadline": time.monotonic() + 100}
        )
        pending = Round(context, {}, {})
        observed = identity(self.selected.manager_root)
        with mock.patch.object(
            round_launch,
            "prepare_launch",
            return_value=self.spec(observed, context),
        ):
            round_launch.prepare(pending, self.capability())
        self.transaction(
            lambda: review_budget.reserve_call(
                self.conn, context, 13.0, execution=observed
            )
        )
        with (
            mock.patch(
                "muninn.review_managed_files.matches", return_value=False
            ),
            mock.patch(
                "muninn.review_managed_files.selection_changed",
                return_value=True,
                create=True,
            ),
            mock.patch.object(
                round_launch,
                "prepare_launch",
                side_effect=ValueError("runtime_probe_unavailable"),
            ) as discovery,
        ):
            with self.assertRaisesRegex(
                ValueError, "runtime_probe_unavailable"
            ):
                round_launch.prepare(
                    pending, self.capability(), home=self.home, now=14.0
                )
            self.assertEqual(pending.refresh_count, 1)
            record = ManagedCallRecord.model_validate_json(
                self.conn.execute(
                    "SELECT requested_json FROM review_call"
                ).fetchone()[0]
            )
            self.assertEqual(record.refresh_count, 1)
            self.assertEqual(record.candidates, (observed,))
            with self.assertRaisesRegex(ValueError, "runtime_race_exhausted"):
                round_launch.prepare(
                    pending, self.capability(), home=self.home, now=15.0
                )
            self.assertEqual(discovery.call_count, 1)

    def test_nonselector_mutation_does_not_authorize_fresh_resolution(
        self,
    ) -> None:
        context = self.context().model_copy(
            update={"deadline": time.monotonic() + 100}
        )
        first, other = identity(self.selected.manager_root), identity(
            self.selected.manager_root, "0.167.0"
        )
        pending = Round(context, {}, {}, launch=self.spec(first, context))
        with (
            mock.patch(
                "muninn.review_managed_files.matches", return_value=False
            ),
            mock.patch(
                "muninn.review_managed_files.selection_changed",
                return_value=False,
                create=True,
            ),
            mock.patch.object(
                round_launch,
                "prepare_launch",
                return_value=self.spec(other, context),
            ) as discovery,
        ):
            with self.assertRaisesRegex(
                ValueError, "runtime_selection_unsafe"
            ):
                round_launch.prepare(pending, self.capability())
            discovery.assert_not_called()

    def test_reused_execution_gets_the_current_evidence_prompt(self) -> None:
        context = self.context()
        observed = identity(self.selected.manager_root)
        pending = Round(
            context,
            {},
            {},
            launch=self.spec(observed, context),
            prepared_prompt=b"old",
            prompt=b"new",
        )
        with (
            mock.patch(
                "muninn.review_managed_files.matches", return_value=True
            ),
            mock.patch(
                "muninn.review_codex_launch.native_prompt",
                return_value=b"current evidence",
            ) as prompt,
            mock.patch.object(round_launch, "prepare_launch") as discovery,
        ):
            result = round_launch.prepare(pending, self.capability())
        self.assertEqual(result.prompt, b"current evidence")
        prompt.assert_called_once_with(context, b"new")
        discovery.assert_not_called()
