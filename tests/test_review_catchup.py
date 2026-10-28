"""Bound automatic admission and preserve denied historical intervals."""

from __future__ import annotations

import json
import sqlite3
from unittest import mock

from muninn import (
    review_catchup,
    review_progress,
    review_read_budget,
    review_requests,
    scope,
)
from muninn.review_json import canonical_json
from muninn.review_read_budget import ReadBudget
from tests.ingest_support import TID, line, rollout
from tests.test_classify import codex_meta, turn_context, user_msg
from tests.test_review_requests import RequestCase


class ReviewCatchupTests(RequestCase):
    """Exercise durable admission against disposable real originals."""

    def setUp(self) -> None:
        super().setUp()
        self.policy = self.policy.model_copy(update={"max_input_bytes": 65536})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.after_id = 0

    def step(self) -> None:
        """Run one bounded pass with its own caller-owned transaction."""
        self.after_id = self.transaction(
            lambda: review_catchup.catch_up(
                self.conn, self.policy, 12.0, self.after_id
            )
        )

    def windows(self) -> list[tuple[int, int]]:
        """Read admitted boundaries without interpreting progress as work."""
        return [
            tuple(row)
            for row in self.conn.execute(
                "SELECT from_line,through_line FROM review_request "
                "ORDER BY from_line,through_line"
            )
        ]

    def test_short_preindex_session_recovers_one_interval_per_pass(
        self,
    ) -> None:
        self.assertEqual(self.windows(), [])
        self.step()
        self.assertEqual(self.windows(), [(1, 2)])
        self.after_id = 0
        self.step()
        self.assertEqual(self.windows(), [(1, 2), (3, 3)])
        self.after_id = 0
        self.step()
        self.assertEqual(self.windows(), [(1, 2), (3, 3)])

    def test_indexed_preactivation_history_stays_excluded(self) -> None:
        self.transaction(
            lambda: review_progress.activate_sources(self.conn, 10.0)
        )
        self.step()
        self.assertEqual(self.windows(), [])
        self.append(self.path, [user_msg(4, "new permitted tail")])
        self.run_ingest()
        self.after_id = 0
        self.step()
        self.assertEqual(self.windows(), [(4, 4)])

    def test_old_history_discovered_after_activation_stays_denied(
        self,
    ) -> None:
        self.conn.execute(
            "UPDATE review_control SET activated_at=1800000000.0"
        )
        self.conn.execute("UPDATE source SET first_seen=1800000001.0")
        self.step()
        self.assertEqual(self.windows(), [])

    def test_disabled_or_changed_policy_admits_nothing(self) -> None:
        self.conn.execute("UPDATE review_control SET admission=0")
        self.step()
        self.assertEqual(self.windows(), [])
        changed = self.policy.model_copy(update={"policy_id": "changed"})
        self.conn.execute(
            "UPDATE review_control SET admission=1,policy_json=?,"
            "policy_digest=?",
            (canonical_json(changed), self.digest(changed)),
        )
        self.step()
        self.assertEqual(self.windows(), [])

    def test_missing_native_identity_and_fork_remain_denied(self) -> None:
        for change in (
            {"session_id": None},
            {"thread_source": "guardian_review"},
            {"source": {"subagent": {"other": "worker"}}},
            {"forked_from_id": "parent"},
        ):
            with self.subTest(change=change):
                self.records[0] = codex_meta("user", TID)
                self.records[0]["payload"].update(change)
                self.path.write_bytes(b"".join(map(line, self.records)))
                self.bump(self.path)
                self.run_ingest()
                self.after_id = 0
                self.step()
                self.assertEqual(self.windows(), [])

    def test_cancelled_window_is_not_revived_after_restart(self) -> None:
        request = self.enqueue(self.window(1, 3))
        self.transaction(
            lambda: review_requests.cancel_request(self.conn, request.id, 11.0)
        )
        self.append(self.path, [user_msg(4, "after cancelled interval")])
        self.run_ingest()
        self.step()
        self.assertEqual(self.windows(), [(1, 3), (4, 4)])
        self.after_id = 0
        self.step()
        self.assertEqual(self.windows(), [(1, 3), (4, 4)])

    def test_partial_tail_waits_for_complete_indexing(self) -> None:
        self.transaction(
            lambda: review_progress.activate_sources(self.conn, 10.0)
        )
        self.append(self.path, [], line(user_msg(4, "partial"))[:-1])
        self.run_ingest()
        self.step()
        self.assertEqual(self.windows(), [])
        self.append(self.path, [], b"\n")
        self.run_ingest()
        self.after_id = 0
        self.step()
        self.assertEqual(self.windows(), [(4, 4)])

    def test_read_refusal_does_not_starve_a_later_source(self) -> None:
        self.write(
            rollout("other"),
            [codex_meta("user", "other"), user_msg(1, "permitted")],
        )
        self.run_ingest()
        original = review_catchup._native

        def refuse_first(
            conn: sqlite3.Connection,
            source: sqlite3.Row,
            budget: ReadBudget,
        ) -> bool:
            if source["thread_id"] == TID:
                raise ValueError("evidence_limit")
            return original(conn, source, budget)

        with mock.patch.object(
            review_catchup, "_native", side_effect=refuse_first
        ):
            self.step()
            self.assertEqual(self.windows(), [])
            records = (self.home / "calls.jsonl").read_text().splitlines()
            self.assertEqual(
                json.loads(records[-1])["error_code"], "evidence_limit"
            )
            self.step()
        self.assertEqual(self.windows(), [(1, 2)])

    def test_erased_window_stays_denied_but_later_interval_advances(
        self,
    ) -> None:
        self.conn.execute(
            "INSERT INTO tombstone(provider,level,thread_id,line,"
            "line_sha256,created_at) VALUES('codex','line',?,2,?,11.0)",
            (TID, self.window(2, 2).boundary_hash),
        )
        self.step()
        self.assertEqual(self.windows(), [])
        self.conn.execute(
            "DELETE FROM event WHERE source_id=? AND line=2",
            (self.need_source()["id"],),
        )
        self.step()
        self.assertEqual(self.windows(), [(3, 3)])
        self.assertEqual(
            self.conn.execute(
                "SELECT completed_line FROM review_source"
            ).fetchone()[0],
            0,
        )

    def test_oversized_source_does_not_consume_the_next_sources_grant(
        self,
    ) -> None:
        self.append(self.path, [{"type": "noop"}] * 500)
        self.write(
            rollout("other"),
            [codex_meta("user", "other"), user_msg(1, "permitted")],
        )
        self.run_ingest()
        self.policy = self.policy.model_copy(update={"max_input_bytes": 4000})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.step()
        self.assertEqual(self.windows(), [])
        self.step()
        self.assertEqual(self.windows(), [(1, 2)])

    def test_recall_modes_do_not_control_learning_admission(self) -> None:
        for mode in ("off", "advisory", "on"):
            with self.subTest(mode=mode):
                (self.home / f"recall.{mode}").touch()
                self.step()
        self.assertEqual(self.windows(), [(1, 2), (3, 3)])

    def test_large_remaining_source_returns_its_first_candidate_promptly(
        self,
    ) -> None:
        self.append(
            self.path,
            [user_msg(number, "later") for number in range(4, 504)],
        )
        self.run_ingest()
        with mock.patch.object(review_read_budget, "MAX_SQL_STEPS", 3000):
            self.step()
            self.step()
        self.assertEqual(self.windows(), [(1, 2), (3, 3)])

    def test_sql_exhaustion_preserves_transaction_and_pending_work(
        self,
    ) -> None:
        with mock.patch.object(review_read_budget, "MAX_SQL_STEPS", 1):
            self.step()
        self.assertEqual(self.windows(), [])
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_source").fetchone()[
                0
            ],
            0,
        )
        self.step()
        self.assertEqual(self.windows(), [(1, 2)])

    def test_erasing_an_admitted_boundary_does_not_block_later_work(
        self,
    ) -> None:
        self.step()
        self.conn.execute(
            "INSERT INTO tombstone(provider,level,thread_id,line,"
            "line_sha256,created_at) VALUES('codex','line',?,2,?,11.0)",
            (TID, self.window(2, 2).boundary_hash),
        )
        self.append(self.path, [user_msg(4, "after erasure")])
        self.run_ingest()
        self.step()
        self.step()
        self.assertEqual(self.windows(), [(1, 2), (3, 3), (4, 4)])

    def test_erased_cursor_hash_still_rejects_rewritten_original(self) -> None:
        self.step()
        self.conn.execute(
            "INSERT INTO tombstone(provider,level,thread_id,line,"
            "line_sha256,created_at) VALUES('codex','line',?,2,?,11.0)",
            (TID, self.window(2, 2).boundary_hash),
        )
        self.records[1] = user_msg(1, "rewritten erased original")
        self.records.append(user_msg(4, "later prompt"))
        self.path.write_bytes(b"".join(map(line, self.records)))
        self.bump(self.path)
        self.run_ingest()
        self.step()
        self.assertEqual(self.windows(), [(1, 2)])

    def test_missing_activation_cannot_admit_history(self) -> None:
        self.conn.execute("UPDATE review_control SET activated_at=NULL")
        self.step()
        self.assertEqual(self.windows(), [])

    def test_normal_exclusions_do_not_report_capture_failures(self) -> None:
        self.conn.execute(
            "UPDATE review_control SET activated_at=1800000000.0"
        )
        self.conn.execute("UPDATE source SET first_seen=1800000001.0")
        self.step()
        self.assertFalse((self.home / "calls.jsonl").exists())

    def test_idle_history_does_not_delay_a_fresh_session_for_hundreds_of_turns(
        self,
    ) -> None:
        source = dict(self.need_source())
        del source["id"]
        for number in range(260):
            source["thread_id"] = f"history-{number}"
            source["path"] = f"history-{number}.jsonl"
            self.conn.execute(
                f"INSERT INTO source({','.join(source)}) "
                f"VALUES({','.join('?' for _ in source)})",
                tuple(source.values()),
            )
        self.transaction(
            lambda: review_progress.activate_sources(self.conn, 10.0)
        )
        self.write(
            rollout("fresh"),
            [codex_meta("user", "fresh"), user_msg(1, "fresh")],
        )
        self.run_ingest()
        self.step()
        self.assertEqual(self.windows(), [])
        self.step()
        self.assertEqual(self.windows(), [(1, 2)])

    def test_observed_noop_tail_stays_in_the_next_admitted_window(
        self,
    ) -> None:
        self.step()
        self.step()
        self.append(self.path, [{"type": "noop"}])
        self.run_ingest()
        self.step()
        self.append(self.path, [user_msg(5, "after noop")])
        self.run_ingest()
        self.step()
        self.assertEqual(self.windows(), [(1, 2), (3, 3), (4, 5)])
        for row in self.conn.execute("SELECT id FROM review_request"):
            self.receipt(row[0])
        completed = self.transaction(
            lambda: review_progress.advance_completed(
                self.conn, self.window().source, 15.0
            )
        )
        self.assertEqual(completed, 5)

    def test_legacy_observation_cursor_does_not_drop_a_noop_tail(
        self,
    ) -> None:
        self.step()
        self.step()
        self.append(self.path, [{"type": "noop"}])
        self.run_ingest()
        self.conn.execute("UPDATE review_source SET examined_line=4")
        self.after_id = 0
        self.append(self.path, [user_msg(5, "after legacy observation")])
        self.run_ingest()
        self.step()
        self.assertEqual(self.windows(), [(1, 2), (3, 3), (4, 5)])

    def test_scope_refusal_survives_restart_and_a_later_scope_grant(
        self,
    ) -> None:
        self.policy = self.policy.model_copy(update={"allow_global": False})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.transaction(
            lambda: review_progress.activate_sources(self.conn, 10.0)
        )
        global_id = scope.global_scope_id(self.conn)
        self.conn.execute(
            "INSERT INTO scope_path(cwd,scope_id,method) VALUES('',?,'alias')",
            (global_id,),
        )
        path = self.write(
            rollout("scoped"),
            [
                codex_meta("user", "scoped", cwd=None),
                user_msg(1, "denied global prompt"),
            ],
        )
        self.run_ingest()
        self.step()
        self.assertEqual(self.windows(), [])
        self.append(
            path,
            [
                turn_context(3, str(self.tmp)),
                {"type": "noop"},
                user_msg(5, "later permitted"),
            ],
        )
        self.run_ingest()
        self.policy = self.policy.model_copy(update={"allow_global": True})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        self.after_id = 0
        self.step()
        self.assertEqual(self.windows(), [(3, 5)])
