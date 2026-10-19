"""Real recall rendering and the review CLI use final, linked evidence."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
from datetime import UTC, datetime
from typing import Any
from unittest import mock

from muninn import cli, hook, hook_recall, obs, query, recall_audit, store
from muninn import cli_recall_review as review_command
from muninn.cli_recall_review import _assessment
from tests.hook_support import AKIA, RecallCase


class RecallReviewTests(RecallCase):
    """User-visible assessment hints and request linkage in a temp HOME."""

    def reviewed(self, *args: str) -> tuple[int, dict[str, Any]]:
        """Run the review command and parse its JSON response."""
        return self.command("recall-review", *args)

    def command(self, *args: str) -> tuple[int, dict[str, Any]]:
        """Run a public read command against the disposable provider tree."""
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.env),
            contextlib.redirect_stdout(output),
        ):
            code = cli.main(list(args))
        return code, json.loads(output.getvalue())

    def test_real_hook_records_only_sources_in_final_context(self) -> None:
        for index in range(3):
            self.talk(
                f"old{index}",
                "alphaterm betaterm gammaterm " + "padding " * 50,
            )
        self.noise(self.repo)
        trace: dict[str, Any] = {}
        out = hook.prompt_submit(
            {
                "cwd": self.scope.project_id,
                "session_id": "current",
                "prompt": "alphaterm betaterm gammaterm",
            },
            "codex",
            self.env,
            trace=trace,
        )
        text = self.body(out)
        self.assertIn("Memory R1: useful/noise/uncertain", text)
        self.assertLessEqual(len(text.encode()), hook.CODEX_RECALL_LIMIT)
        report = recall_audit.review(self.home, None, ack=False)
        sources = report["deliveries"][0]["sources"]
        self.assertEqual(len(sources), len(trace["delivered_ids"]))
        for source in sources:
            self.assertIn(source["ref"], text)
        self.assertLess(len(sources), len(trace["returned_ids"]))

    def test_off_and_empty_hooks_do_not_count(self) -> None:
        self.noise(self.repo)
        self.assertEqual(self.ask(), {})
        self.assertFalse((self.home / recall_audit.NAME).exists())
        self.talk("a", "alphaterm betaterm gammaterm")
        (self.home / "recall.off").touch()
        self.assertEqual(self.ask(), {})
        self.assertFalse((self.home / recall_audit.NAME).exists())

    def test_review_resolves_request_and_model_assessment(self) -> None:
        now = datetime.now(UTC).isoformat()
        number = self.talk("current", "alphaterm betaterm gammaterm", ts=now)
        row = self.conn.execute(
            "SELECT * FROM event WHERE id=?", (number,)
        ).fetchone()
        self.talk("old", "alphaterm betaterm gammaterm evidence")
        self.noise(self.repo)
        request_ref = self.ref(number)
        session = self.conn.execute(
            "SELECT thread_id FROM source WHERE id=?", (row["source_id"],)
        ).fetchone()[0]
        hook.prompt_submit(
            {
                "cwd": self.scope.project_id,
                "session_id": session,
                "prompt": row["text"],
            },
            "codex",
            self.env,
        )
        assessment = self.transcripts.add_event(
            row["source_id"],
            self.repo,
            "Memory R1: noise — unrelated evidence",
            kind="reply",
            role="assistant",
            ts=now,
        )
        code, report = self.reviewed()
        self.assertEqual(code, 0)
        delivery = report["deliveries"][0]
        self.assertEqual(delivery["request_ref"], request_ref)
        self.assertEqual(delivery["assessment"]["rating"], "noise")
        self.assertEqual(delivery["assessment"]["ref"], self.ref(assessment))
        self.assertIn("self-report", report["review_prompt"])
        self.assertIn("know show K_ID --revision N", report["review_prompt"])
        self.assertIn("preview hash", report["review_prompt"])

    def test_review_marks_missing_requests_unresolved(self) -> None:
        self.talk("old", "alphaterm betaterm gammaterm evidence")
        self.noise(self.repo)
        self.ask()
        code, report = self.reviewed()
        self.assertEqual(code, 0)
        self.assertEqual(
            report["deliveries"][0]["request_status"], "unresolved"
        )
        self.assertEqual(
            report["deliveries"][0]["assessment"]["rating"], "unassessed"
        )
        self.assertEqual(self.reviewed("--ack")[0], 2)
        self.assertEqual(self.reviewed("--batch", "1", "--ack")[0], 2)

    def test_due_reminder_fits_both_provider_caps(self) -> None:
        self.talk("old", "alphaterm betaterm gammaterm evidence")
        self.noise(self.repo)
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                with mock.patch.object(
                    recall_audit,
                    "counts",
                    return_value=(49 if provider == "claude" else 99, 0),
                ):
                    out = hook.prompt_submit(
                        {
                            "cwd": self.scope.project_id,
                            "session_id": "now",
                            "prompt": "alphaterm betaterm gammaterm",
                        },
                        provider,
                        self.env,
                    )
                text = self.body(out)
                self.assertIn("Review due", text)
                self.assertIn("recall review due", str(out["systemMessage"]))
                self.assertIn(
                    "recall-review --batch 1", str(out["systemMessage"])
                )
                self.assertLessEqual(
                    len(text.encode()), 900 if provider == "codex" else 1500
                )

    def test_echoed_options_are_not_a_usefulness_rating(self) -> None:
        from_source = self.transcripts.add_source("rating", session="rating")
        number = self.transcripts.add_event(from_source, self.repo, "request")
        row = self.conn.execute(
            "SELECT * FROM event WHERE id=?", (number,)
        ).fetchone()
        self.transcripts.add_event(
            from_source,
            self.repo,
            "Memory R1: useful/noise/uncertain — reason",
            kind="reply",
            role="assistant",
        )
        with contextlib.closing(
            store.connect_ro(store.db_path(self.home))
        ) as conn:
            self.assertEqual(_assessment(conn, row, 1)["rating"], "unassessed")

    def test_legacy_request_and_reason_previews_are_redacted(self) -> None:
        prompt = f"alphaterm betaterm gammaterm {AKIA}"
        now = datetime.now(UTC).isoformat()
        number = self.talk("legacy", prompt, ts=now)
        source = self.conn.execute(
            "SELECT source_id FROM event WHERE id=?", (number,)
        ).fetchone()[0]
        self.talk("older", "alphaterm betaterm gammaterm evidence")
        self.noise(self.repo)
        self.ask(prompt, provider="codex", session_id="legacy")
        self.transcripts.add_event(
            source,
            self.repo,
            f"Memory R1: noise — {AKIA}",
            kind="reply",
            role="assistant",
            ts=now,
        )
        code, report = self.reviewed()
        self.assertEqual(code, 0)
        self.assertNotIn(AKIA, json.dumps(report))
        delivery = report["deliveries"][0]
        self.assertIn("[redacted:secret]", delivery["request_preview"])
        self.assertIn(
            "[redacted:secret]", delivery["assessment"]["reason_preview"]
        )

    def test_statement_lock_after_open_returns_json_error(self) -> None:
        self.talk("old", "alphaterm betaterm gammaterm evidence")
        self.noise(self.repo)
        self.ask()
        request = review_command._request

        def locked(
            conn: sqlite3.Connection, delivery: dict[str, Any]
        ) -> sqlite3.Row | None:
            conn.execute("PRAGMA busy_timeout=1")
            self.conn.execute("BEGIN EXCLUSIVE")
            try:
                return request(conn, delivery)
            finally:
                self.conn.execute("ROLLBACK")

        with mock.patch.object(review_command, "_request", side_effect=locked):
            code, report = self.reviewed()
        self.assertEqual(code, 4)
        self.assertEqual(report["error"], "store_unavailable")
        self.assertIn("SQLITE_BUSY", report["detail"])

    def test_normalized_delivery_keeps_revision_evidence_and_safe_ids(
        self,
    ) -> None:
        ident = self.add(content="alphaterm betaterm gammaterm old")
        content = "alphaterm betaterm gammaterm café " + "é" * 200
        self.replace_text(ident, content)
        trace: dict[str, Any] = {}
        out = hook.prompt_submit(
            self.payload(prompt="alphaterm betaterm gammaterm"),
            "claude",
            self.env,
            trace=trace,
        )
        text = self.body(out)
        self.assertLessEqual(len(text.encode()), hook.RECALL_LIMIT)
        self.assertIn(f"{ident} revision 2", text)
        self.assertEqual(trace["delivered_knowledge_ids"], [ident])
        report = recall_audit.review(self.home, None, ack=False)
        (saved,) = report["deliveries"][0]["knowledge"]
        with contextlib.closing(
            store.connect_ro(store.db_path(self.home))
        ) as conn:
            (preview,) = query.search(
                conn,
                "alphaterm betaterm gammaterm",
                cwd=str(self.scope.project_id),
                env={},
            )["knowledge"]
        self.assertEqual(saved["id"], ident)
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(saved["evidence_refs"], preview["evidence_refs"])
        self.assertEqual(
            saved["sha256"],
            hashlib.sha256(preview["text"].encode()).hexdigest(),
        )
        self.add(
            action="replace",
            knowledge_id=ident,
            expected_revision=2,
            content="Later guidance with different support.",
            evidence_refs=[self.evidence[1].id],
        )
        code, reviewed = self.reviewed()
        self.assertEqual(code, 0)
        delivery = reviewed["deliveries"][0]
        self.assertEqual(delivery["sources"], [])
        (recorded,) = delivery["knowledge"]
        self.assertEqual(recorded, saved)
        code, version = self.command(
            "know",
            "show",
            recorded["id"],
            "--revision",
            str(recorded["revision"]),
        )
        self.assertEqual((code, version["revision"]), (0, 2))
        (source,) = recorded["evidence_sources"]
        self.assertEqual(source["evidence_id"], recorded["evidence_refs"][0])
        code, opened = self.command("open", source["ref"], "--context", "3")
        self.assertEqual((code, opened["hash_ok"]), (0, True))
        self.assertEqual(opened["text"], "Use Python for this project.")
        self.assertEqual(
            opened["provenance"]["line_sha256"], source["line_sha256"]
        )
        self.assertNotIn(str(self.path), json.dumps(reviewed))
        self.assertNotIn(opened["text"], json.dumps(reviewed))
        self.assertNotIn(content, json.dumps(report))
        self.assertNotIn(
            content.encode(), (self.home / recall_audit.NAME).read_bytes()
        )
        obs.log_call(self.home, {"cmd": "hook prompt", **trace})
        log_text = (self.home / "calls.jsonl").read_text()
        logged = json.loads(log_text.splitlines()[-1])
        self.assertEqual(logged["knowledge_ids"], [ident])
        self.assertEqual(logged["delivered_knowledge_ids"], [ident])
        self.assertNotIn("delivered_knowledge", logged)
        self.assertNotIn("evidence_sources", log_text)

    def test_recorded_locator_does_not_assert_original_availability(
        self,
    ) -> None:
        self.add(content="alphaterm betaterm gammaterm")
        self.ask()
        _, before = self.reviewed()
        saved = before["deliveries"][0]["knowledge"][0]
        (source,) = saved["evidence_sources"]
        original = self.path.read_bytes()
        self.path.write_bytes(original.replace(b"Use Python", b"Use Golang"))
        code, changed = self.command("open", source["ref"])
        self.assertEqual((code, changed["hash_ok"]), (0, False))
        self.assertEqual(
            changed["provenance"]["line_sha256"], source["line_sha256"]
        )
        self.path.unlink()
        code, missing = self.command("open", source["ref"])
        self.assertEqual(code, 0)
        self.assertIsNone(missing["hash_ok"])
        _, after = self.reviewed()
        self.assertEqual(after["deliveries"][0]["knowledge"][0], saved)
        self.assertIn("changed hashes stay unresolved", after["review_prompt"])

    def test_unbound_evidence_never_falls_back_to_current_revision(
        self,
    ) -> None:
        ident = self.add(content="alphaterm betaterm gammaterm")
        found = query.search(
            self.conn,
            "alphaterm betaterm gammaterm",
            cwd=str(self.scope.project_id),
            env={},
        )
        self.replace_text(ident, "New revision with the same evidence.")
        self.conn.execute(
            "DELETE FROM knowledge_support WHERE knowledge_id=? "
            "AND revision=1",
            (ident,),
        )
        # A stale page cannot borrow the later revision's support mapping.
        with mock.patch.object(
            hook_recall, "_search_page", return_value=found
        ):
            self.ask()
        _, report = self.reviewed()
        (saved,) = report["deliveries"][0]["knowledge"]
        self.assertEqual((saved["id"], saved["revision"]), (ident, 1))
        self.assertEqual(
            saved["evidence_sources"],
            [
                {
                    "evidence_id": self.evidence[0].id,
                    "ref": None,
                    "line_sha256": None,
                }
            ],
        )
        self.assertIn("missing mappings", report["review_prompt"])

    def test_only_delivered_knowledge_records_source_mapping(self) -> None:
        for index in range(3):
            self.add(
                content=f"alphaterm betaterm gammaterm {index} " + "x" * 280
            )
        trace: dict[str, Any] = {}
        out = hook.prompt_submit(
            self.payload(prompt="alphaterm betaterm gammaterm"),
            "codex",
            self.env,
            trace=trace,
        )
        text = self.body(out)
        _, report = self.reviewed()
        entries = report["deliveries"][0]["knowledge"]
        self.assertLess(len(entries), len(trace["knowledge_ids"]))
        self.assertEqual(
            [e["id"] for e in entries], trace["delivered_knowledge_ids"]
        )
        self.assertLessEqual(len(text.encode()), hook.CODEX_RECALL_LIMIT)
        for entry in entries:
            self.assertIn(entry["id"], text)
            self.assertEqual(
                len(entry["evidence_sources"]), len(entry["evidence_refs"])
            )
