"""Drive real owned wrappers with synthetic local clients and temp stores."""

from __future__ import annotations

import json
import os
import time
from unittest import mock, skipIf

from muninn import review_supervisor, review_supervisor_result
from muninn.review_models import ReviewContext
from muninn.review_process import WorkerReady
from muninn.review_round_state import Round
from tests.managed_supervisor_base import ManagedSupervisorBase


@skipIf(os.name == "nt", "Darwin managed route uses POSIX test stand-ins")
class ManagedSupervisorTests(ManagedSupervisorBase):
    """Exercise preparation with fake clients and temporary stores."""

    def test_two_round_update_preserves_policy_attempt_and_each_execution(
        self,
    ) -> None:
        self.install("0.166.0", two_rounds=True)
        original = review_supervisor_result._advance

        def advance(pending: Round, context: ReviewContext) -> None:
            original(pending, context)
            self.install("0.167.0", two_rounds=True)

        with mock.patch(
            "muninn.review_supervisor_result._advance", side_effect=advance
        ):
            self.finish()
        records = self.call_records()
        self.assertEqual(len(records), 2)
        self.assertEqual(
            [record.candidates[-1].requested.version for record in records],
            ["0.166.0", "0.167.0"],
        )
        self.assertEqual(
            [record.launched_identity for record in records],
            [record.candidates[-1].digest for record in records],
        )
        self.assertEqual(
            self.marker.read_text().splitlines(), ["0.166.0", "0.167.0"]
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT policy_digest FROM review_control"
            ).fetchone()[0],
            self.digest(self.policy),
        )
        self.assertTrue(
            all(
                value is None
                for value in json.loads(
                    self.conn.execute(
                        "SELECT observed_json FROM review_call LIMIT 1"
                    ).fetchone()[0]
                ).values()
            )
        )

    def test_update_at_readiness_appends_candidate_without_extra_charge(
        self,
    ) -> None:
        original = self.supervisor._persist
        changed = False

        def ready(event: review_supervisor.ProcessEvent, now: float) -> None:
            nonlocal changed
            original(event, now)
            if isinstance(event, WorkerReady) and not changed:
                changed = True
                self.install("0.167.0")

        with mock.patch.object(self.supervisor, "_persist", side_effect=ready):
            self.finish()
        record = self.call_records()[0]
        self.assertEqual(
            [item.requested.version for item in record.candidates],
            ["0.166.0", "0.167.0"],
        )
        self.assertEqual(record.refresh_count, 1)
        self.assertEqual(self.marker.read_text().splitlines(), ["0.167.0"])
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )

    def test_update_after_offer_gets_one_acknowledged_wrapper_replacement(
        self,
    ) -> None:
        original = self.supervisor.monitor.activate
        changed = False

        def activate(attempt: str, index: int, frame: bytes) -> None:
            nonlocal changed
            if not changed:
                changed = True
                self.install("0.167.0")
            original(attempt, index, frame)

        with mock.patch.object(
            self.supervisor.monitor, "activate", side_effect=activate
        ):
            self.finish()
        record = self.call_records()[0]
        self.assertEqual(
            [event.kind for event in record.events],
            ["offer", "prestart_refusal", "offer"],
        )
        self.assertEqual(record.refresh_count, 1)
        self.assertEqual(self.marker.read_text().splitlines(), ["0.167.0"])
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )

    def test_second_offer_race_stops_before_any_native_client_or_new_attempt(
        self,
    ) -> None:
        original = self.supervisor.monitor.activate
        offers = 0

        def activate(attempt: str, index: int, frame: bytes) -> None:
            nonlocal offers
            offers += 1
            self.install("0.167.0" if offers == 1 else "0.168.0")
            original(attempt, index, frame)

        with mock.patch.object(
            self.supervisor.monitor, "activate", side_effect=activate
        ):
            self.finish()
        self.assertFalse(self.marker.exists())
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT error_code FROM review_attempt"
            ).fetchone()[0],
            "runtime_race_exhausted",
        )
        self.assertIsNone(self.call_records()[0].launched_identity)

    def test_stop_freezes_an_owned_launch_notice_before_containment(
        self,
    ) -> None:
        original = self.supervisor.monitor.activate
        activated = False

        def activate(attempt: str, index: int, frame: bytes) -> None:
            nonlocal activated
            original(attempt, index, frame)
            activated = True

        deadline = time.monotonic() + 5.0
        with mock.patch.object(
            self.supervisor.monitor, "activate", side_effect=activate
        ):
            while not activated and time.monotonic() < deadline:
                self.supervisor.tick(20.0)
                time.sleep(0.01)
        self.assertTrue(activated)
        notice_seen = False
        while time.monotonic() < deadline:
            with self.supervisor.monitor.lock:
                notice_seen = any(
                    slot.launched is not None
                    for slot in self.supervisor.monitor.slots.values()
                )
            if notice_seen:
                break
            time.sleep(0.01)
        self.assertTrue(notice_seen)
        self.supervisor.stop(21.0)
        record = self.call_records()[0]
        self.assertEqual(
            record.launched_identity, record.candidates[-1].digest
        )
