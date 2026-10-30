"""Complete scan coverage, cooperative deadlines and no partial acceptance."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from unittest import mock

from install import host_renewal_scan
from install.context import StepFailedError
from install.host_renewal_preview import preview, scan_state
from muninn import review_recovery, store_copy
from muninn.review_pause_state import Witness
from muninn.review_progress import digest
from tests.host_renewal_support import HostFixture, states
from tests.installer_support import ROOT


class HostRenewalScanTests(HostFixture):
    """Exercise complete real SQLite metadata scans and all deadline phases."""

    def test_control_is_separate_from_full_work_and_progress(self) -> None:
        first = preview(self.ctx, self.repo, self.sha)
        self.sql(
            "UPDATE review_control SET generation=generation+1,admission=0"
        )
        second = preview(self.ctx, self.repo, self.sha)
        self.assertEqual(first.state.work_digest, second.state.work_digest)
        self.assertEqual(
            first.state.progress_digest, second.state.progress_digest
        )
        self.assertNotEqual(first.digest, second.digest)

    def test_same_digest_forged_policy_snapshot_refuses(self) -> None:
        self.request(raw="{}")
        before = states(self.home)
        with self.assertRaises(StepFailedError):
            preview(self.ctx, self.repo, self.sha)
        self.assertEqual(states(self.home), before)

    def test_metadata_scan_is_independent_of_prompt_cap(self) -> None:
        self.sql(
            "INSERT INTO review_issue "
            "VALUES('large','config','startup',?,'safe',1.0)",
            ("metadata" * self.old.max_input_bytes,),
        )
        before = states(self.home)
        with mock.patch(
            "install.host_renewal_scan.time.monotonic", return_value=0.0
        ):
            plan = preview(self.ctx, self.repo, self.sha)
        self.assertTrue(plan.digest)
        self.assertEqual(states(self.home), before)

    def test_scan_uses_at_most_twenty_two_complete_state_streams(self) -> None:
        self.request()
        statements: list[str] = []
        with closing(sqlite3.connect(self.path)) as conn:
            conn.set_trace_callback(statements.append)
            scan_state(
                conn, self.path, candidate_digest=digest(self.candidate)
            )
            self.assertFalse(conn.in_transaction)
            self.assertEqual(
                conn.execute("PRAGMA temp_store").fetchone()[0], 2
            )
        streams = [
            sql
            for sql in statements
            if sql.startswith(
                ("SELECT * FROM", "SELECT DISTINCT policy_digest")
            )
        ]
        self.assertEqual(len(streams), 22)
        self.assertTrue(
            all("LIMIT" not in sql and "OFFSET" not in sql for sql in streams)
        )
        self.assertEqual(
            sum(
                "DISTINCT policy_digest,policy_json" in sql for sql in streams
            ),
            1,
        )

    def test_deadline_interrupts_sql_and_removes_handler(self) -> None:
        self.sql(
            "INSERT INTO review_issue "
            "VALUES('large','config','startup',?,'safe',1.0)",
            ("metadata" * 20000,),
        )
        clock = [0.0]
        original = host_renewal_scan._Scan.interrupt

        def expire(current: host_renewal_scan._Scan) -> int:
            clock[0] = 31.0
            return original(current)

        before = states(self.home)
        with (
            closing(sqlite3.connect(self.path)) as conn,
            mock.patch(
                "install.host_renewal_scan.time.monotonic",
                side_effect=lambda: clock[0],
            ),
            mock.patch.object(host_renewal_scan._Scan, "interrupt", expire),
        ):
            with self.assertRaisesRegex(StepFailedError, "host_renewal_limit"):
                scan_state(
                    conn, self.path, candidate_digest=digest(self.candidate)
                )
            self.assertEqual(
                conn.execute(
                    "WITH RECURSIVE values_to_sum(value) AS (VALUES(0) "
                    "UNION ALL SELECT value+1 FROM values_to_sum "
                    "WHERE value<9999) SELECT sum(value) FROM values_to_sum"
                ).fetchone()[0],
                49995000,
            )
        self.assertEqual(clock[0], 31.0)
        self.assertEqual(states(self.home), before)

    def expire_at(self, boundary: str) -> None:
        """Refuse a row or final-result overrun without changing the store."""
        clock = [0.0]
        original_row = host_renewal_scan._row
        original_result = host_renewal_scan._Scan.result

        def row(value: dict[str, object]) -> bytes:
            framed = original_row(value)
            if boundary == "row":
                clock[0] = 31.0
            return framed

        def result(
            current: host_renewal_scan._Scan, witness: Witness
        ) -> object:
            value = original_result(current, witness)
            clock[0] = 31.0
            return value

        with (
            mock.patch(
                "install.host_renewal_scan.time.monotonic",
                side_effect=lambda: clock[0],
            ),
            mock.patch.object(host_renewal_scan, "_row", row),
            mock.patch.object(host_renewal_scan._Scan, "result", result),
            self.assertRaisesRegex(StepFailedError, "host_renewal_limit"),
        ):
            preview(self.ctx, self.repo, self.sha)

    def test_deadline_during_row_and_after_last_row_refuses(self) -> None:
        before = states(self.home)
        for boundary in ("row", "result"):
            with self.subTest(boundary=boundary):
                self.expire_at(boundary)
            self.assertEqual(states(self.home), before)

    def test_every_inventoried_row_and_scalar_field_is_covered(self) -> None:
        self.seed_inventory()
        tables = (
            *store_copy.GOVERNED_TABLES,
            "source",
            "tombstone",
            "learning_setup",
            "review_issue",
        )
        changed = 0
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            before = scan_state(
                conn, self.path, candidate_digest=digest(self.candidate)
            )
            for table in tables:
                row = conn.execute(f'SELECT * FROM "{table}"').fetchone()
                self.assertIsNotNone(row, table)
                for column in dict(row):
                    old = row[column]
                    replacement = (
                        old + 1
                        if isinstance(old, (int, float))
                        else (
                            old + "changed"
                            if isinstance(old, str)
                            else "changed"
                        )
                    )
                    conn.execute("SAVEPOINT mutation")
                    try:
                        conn.execute(
                            f'UPDATE "{table}" SET "{column}"=?',
                            (replacement,),
                        )
                        try:
                            after = scan_state(
                                conn,
                                self.path,
                                candidate_digest=digest(self.candidate),
                            )
                        except StepFailedError:
                            continue
                        with self.subTest(table=table, column=column):
                            self.assertNotEqual(after.state, before.state)
                            changed += 1
                    except sqlite3.IntegrityError:
                        pass
                    finally:
                        conn.execute("ROLLBACK TO mutation")
                        conn.execute("RELEASE mutation")
            self.assertEqual(
                scan_state(
                    conn, self.path, candidate_digest=digest(self.candidate)
                ),
                before,
            )
        self.assertGreater(changed, 50)

    def test_exact_setup_notice_is_the_only_allowed_replacement(self) -> None:
        request = self.request()
        self.attempt(request)
        plan = preview(self.ctx, self.repo, self.sha)
        detail = review_recovery.explain(
            "process_identity_unverified", "recovery"
        )
        self.insert(
            "review_issue", {"id": "setup", **detail, "created_at": 0.0}
        )
        with closing(sqlite3.connect(self.path)) as conn:
            after = scan_state(
                conn,
                self.path,
                candidate_digest=digest(self.candidate),
                containment=plan.containment,
            )
        self.assertEqual(
            after.containment.invariant_digest,
            plan.containment.invariant_digest,
        )
        self.sql("UPDATE review_issue SET created_at=1.0 WHERE id='setup'")
        with self.assertRaises(StepFailedError):
            self.scan(plan.containment)

    def test_fixed_erasure_and_witness_changes_are_bound(self) -> None:
        initial = preview(self.ctx, self.repo, self.sha)
        key = self.ctx.data / "tombstone.key"
        key.write_bytes(b"k" * 32)
        key.chmod(0o600)
        changed = preview(self.ctx, self.repo, self.sha)
        self.assertNotEqual(
            initial.state.erasure_digest, changed.state.erasure_digest
        )
        self.assertNotEqual(initial.digest, changed.digest)
        journal = self.ctx.data / "tombstones.jsonl"
        journal.write_bytes(
            b'{"provider":"codex","level":"thread","thread_id":"synthetic","created_at":1.0}\n'
        )
        journal.chmod(0o600)
        self.assertNotEqual(
            preview(self.ctx, self.repo, self.sha).digest, changed.digest
        )
        for path, raw in (
            (key, b"bad"),
            (journal, b"torn"),
            (self.ctx.data / "review.supervisor.json", b"{}"),
        ):
            old = path.read_bytes()
            path.write_bytes(raw)
            before = states(self.home)
            with (
                self.subTest(path=path.name),
                self.assertRaises(StepFailedError),
            ):
                preview(self.ctx, self.repo, self.sha)
            self.assertEqual(states(self.home), before)
            path.write_bytes(old)

    def test_source_formats_and_unmarked_sources_refuse(self) -> None:
        for version in (1, 3, 0):
            self.sql(f"PRAGMA user_version={version}")
            before = states(self.home)
            with (
                self.subTest(version=version),
                self.assertRaises(StepFailedError),
            ):
                preview(self.ctx, self.repo, self.sha)
            self.assertEqual(states(self.home), before)
        self.sql("PRAGMA user_version=2")
        self.sql("DELETE FROM store_meta")
        before = states(self.home)
        with self.assertRaises(StepFailedError):
            preview(self.ctx, self.repo, self.sha)
        self.assertEqual(states(self.home), before)

    def test_real_frozen_marked_v1_sources_refuse(self) -> None:
        for shape in ("base", "state"):
            self.path.unlink()
            source = (
                ROOT / "tests/fixtures/installer" / f"marked-v1-{shape}.sql"
            )
            with closing(sqlite3.connect(self.path)) as conn:
                conn.executescript(source.read_text())
            self.path.chmod(0o600)
            before = states(self.home)
            with (
                self.subTest(shape=shape),
                self.assertRaisesRegex(
                    StepFailedError, "host_renewal_current_format_required"
                ),
            ):
                preview(self.ctx, self.repo, self.sha)
            self.assertEqual(states(self.home), before)

    def test_unresolved_calls_outside_projection_refuse(self) -> None:
        self.seed_inventory()
        preview(self.ctx, self.repo, self.sha)
        for state in ("reserved", "launched", "unknown"):
            self.sql("UPDATE review_call SET state=?", (state,))
            before = states(self.home)
            with (
                self.subTest(state=state),
                self.assertRaisesRegex(
                    StepFailedError, "host_renewal_containment_required"
                ),
            ):
                preview(self.ctx, self.repo, self.sha)
            self.assertEqual(states(self.home), before)

    def test_no_call_containment_projection_is_exact(self) -> None:
        request = self.request()
        attempt = self.attempt(request)
        plan = preview(self.ctx, self.repo, self.sha)
        self.assertEqual(plan.request_ids, (request,))
        self.assertEqual(plan.attempt_ids, (attempt,))
        job = plan.containment.jobs[0]
        self.assertEqual(
            (job.request.id, job.attempt.id, job.attempt_count),
            (request, attempt, 1),
        )
        self.sql(
            "UPDATE review_attempt SET ended_at=3.0,progress_at=3.0,"
            "outcome='abandoned'"
        )
        self.sql(
            "UPDATE review_request SET state='failed',"
            "current_attempt_id=NULL,retry_at=NULL"
        )
        after = self.scan(plan.containment)
        self.assertEqual(
            after.containment.invariant_digest,
            plan.containment.invariant_digest,
        )
        self.assertNotEqual(after.state.work_digest, plan.state.work_digest)
        self.sql("UPDATE review_request SET trigger='hook'")
        changed = self.scan(plan.containment)
        self.assertNotEqual(
            changed.containment.invariant_digest,
            plan.containment.invariant_digest,
        )

    def test_cancelled_fields_and_backward_attempt_clocks_are_exact(
        self,
    ) -> None:
        request = self.request()
        self.attempt(request)
        self.sql("UPDATE review_request SET state='cancelled'")
        plan = preview(self.ctx, self.repo, self.sha)
        self.sql(
            "UPDATE review_attempt SET ended_at=3.0,progress_at=3.0,"
            "outcome='abandoned'"
        )
        checked = self.scan(plan.containment)
        self.assertEqual(
            checked.containment.jobs[0].request,
            plan.containment.jobs[0].request,
        )
        self.sql("UPDATE review_attempt SET ended_at=1.0,progress_at=1.0")
        with self.assertRaises(StepFailedError):
            self.scan(plan.containment)
        self.sql("UPDATE review_attempt SET ended_at=3.0,progress_at=3.0")
        self.sql("UPDATE review_request SET current_attempt_id=NULL")
        with self.assertRaises(StepFailedError):
            self.scan(plan.containment)

    def test_keyed_table_intent_never_creates_missing_key(self) -> None:
        self.insert(
            "tombstone",
            {
                "provider": "codex",
                "level": "line",
                "thread_id": "synthetic",
                "line": 0,
                "line_sha256": "ev2:" + "a" * 64,
                "created_at": 1.0,
            },
        )
        before = states(self.home)
        with self.assertRaisesRegex(
            StepFailedError, "host_renewal_erasure_invalid"
        ):
            preview(self.ctx, self.repo, self.sha)
        self.assertEqual(states(self.home), before)

    def test_unfinished_attempt_requires_running_or_cancelled_request(
        self,
    ) -> None:
        request = self.request()
        self.attempt(request)
        for state in ("pending", "waiting", "completed", "failed"):
            self.sql("UPDATE review_request SET state=?", (state,))
            before = states(self.home)
            with self.subTest(state=state), self.assertRaises(StepFailedError):
                preview(self.ctx, self.repo, self.sha)
            self.assertEqual(states(self.home), before)
