"""Recover indexed learning tails through the real supervisor tick."""

from __future__ import annotations

import json
from unittest import mock

from muninn import hook_learn, store
from tests.ingest_support import SEG, line, rollout
from tests.test_classify import codex_meta, user_msg
from tests.test_review_supervisor import SupervisorCase


class LearningCatchupRuntimeTests(SupervisorCase):
    """Use real temporary originals, policy and durable request state."""

    def setUp(self) -> None:
        super().setUp()
        self.records[0]["payload"]["session_id"] = self.need_source()[
            "session_root"
        ]
        self.path.write_bytes(b"".join(map(line, self.records)))
        self.bump(self.path)
        self.run_ingest()

    def through(self) -> int:
        """Read the greatest durably admitted physical boundary."""
        return self.conn.execute(
            "SELECT max(through_line) FROM review_request"
        ).fetchone()[0]

    def test_late_indexed_tail_is_admitted_without_another_hook(self) -> None:
        with mock.patch.object(self.supervisor, "_spawn"):
            self.supervisor.tick(13.0)
            self.append(self.path, [user_msg(4, "late permitted prompt")])
            self.assertEqual(self.through(), 3)
            self.run_ingest()
            self.supervisor.tick(19.0)
            self.assertEqual(self.through(), 4)
            self.supervisor.tick(25.0)
            self.assertEqual(
                self.conn.execute(
                    "SELECT count(*) FROM review_request "
                    "WHERE from_line=4 AND through_line=4"
                ).fetchone()[0],
                1,
            )

    def test_writer_contention_retries_the_missed_interval(self) -> None:
        self.append(self.path, [user_msg(4, "writer was busy")])
        self.run_ingest()
        with mock.patch.object(self.supervisor, "_spawn"):
            with store.writer_lock(self.home, wait_s=0):
                self.supervisor.tick(13.0)
                self.assertEqual(self.through(), 3)
            self.supervisor.tick(19.0)
            self.assertEqual(self.through(), 4)

    def test_session_end_before_first_index_is_eventually_recovered(
        self,
    ) -> None:
        path = self.write(
            rollout(SEG),
            [codex_meta("user", SEG), user_msg(1, "short native session")],
        )
        capture = hook_learn.capture_boundary(
            self.home,
            "codex",
            "session-end",
            json.dumps(
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": SEG,
                    "cwd": str(self.tmp),
                    "transcript_path": str(path),
                    "reason": "other",
                }
            ).encode(),
            self.policy,
        )
        self.assertEqual(capture.code, "source_unavailable")
        self.run_ingest()
        with mock.patch.object(self.supervisor, "_spawn"):
            self.supervisor.tick(13.0)
            self.supervisor.tick(19.0)
        self.assertEqual(
            self.conn.execute(
                "SELECT from_line,through_line FROM review_request "
                "WHERE thread_id=?",
                (SEG,),
            ).fetchone()[:],
            (1, 2),
        )
