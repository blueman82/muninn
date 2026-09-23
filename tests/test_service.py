"""End-to-end tests for continuous dual-corpus provenance context."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Callable, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from brain import Brain
from context import build_index
from normalizers import (
    parse_source,
    parse_source_incremental,
    source_fingerprint,
)
from service import MAX_CLIENTS, MAX_REQUEST_BYTES, RequestHandler, serve
from service import request as service_request

ROOT = Path(__file__).parents[1]
CONTEXT = ROOT / "scripts" / "context.py"
CODEX_HOOK = ROOT / "hooks" / "codex.py"
README = ROOT / "README.md"

CRASH_APPEND_POINTS = (
    "after_event_insert",
    "after_fts_insert",
    "after_assertion_supersede",
    "after_assertion_insert",
    "after_source_state",
    "after_meta_counts",
    "before_commit",
)
CRASH_REPLACE_POINTS = (
    "after_delete_assertions",
    "after_delete_fts",
    "after_delete_events",
)
CRASH_RENAME_POINTS = (
    "after_rename_events",
    "after_rename_source_state",
    "before_rename_commit",
)


def run(
    *arguments: str, socket_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a public command with an optional service socket environment."""
    environment = os.environ | (
        {"PROVENANCE_CONTEXT_SOCKET": str(socket_path)} if socket_path else {}
    )
    return subprocess.run(
        [sys.executable, str(CONTEXT), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def e6_record(provider: str, content: str) -> str:
    """Build one complete provider-specific assistant record."""
    if provider == "codex":
        return json.dumps(
            {
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": content,
                    "cwd": "/repo",
                }
            }
        )
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": content}],
            },
            "cwd": "/repo",
        }
    )


class ServiceTest(unittest.TestCase):
    """Exercise append freshness, recovery, and client safety over UDS."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.codex = base / "codex"
        self.claude = base / "claude"
        self.codex.mkdir()
        self.claude.mkdir()
        self.database = base / "context.sqlite"
        self.state = base / "state"
        self.socket = base / "brain.sock"
        self.process = self.start()

    def tearDown(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            self.process.wait(timeout=5)
        if self.process.stderr:
            self.process.stderr.close()
        self.temporary.cleanup()

    def start(
        self,
        environment: dict[str, str] | None = None,
        wait_for_socket: bool = True,
    ) -> subprocess.Popen[str]:
        """Start the documented daemon command and wait for its socket."""
        process = subprocess.Popen(
            [
                sys.executable,
                str(CONTEXT),
                "serve",
                "--codex-root",
                str(self.codex),
                "--claude-root",
                str(self.claude),
                "--db",
                str(self.database),
                "--state-dir",
                str(self.state),
                "--socket",
                str(self.socket),
                "--interval",
                "0.05",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ | (environment or {}),
        )
        if not wait_for_socket:
            return process
        for _ in range(100):
            if self.socket.exists():
                return process
            if process.poll() is not None:
                error = process.stderr
                raise AssertionError(
                    error.read() if error else "daemon failed"
                )
            time.sleep(0.02)
        process.kill()
        raise AssertionError("daemon did not create its socket")

    def recall(self, prompt: str) -> dict[str, object]:
        """Recall through the documented client surface."""
        result = run(
            "recall",
            "--socket",
            str(self.socket),
            "--prompt",
            prompt,
            "--repo",
            "/repo",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def wait_for(self, prompt: str) -> dict[str, object]:
        """Poll recall until the appended source appears."""
        until = time.monotonic() + 5
        while time.monotonic() < until:
            packet = self.recall(prompt)
            if packet["evidence"]:
                return packet
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {prompt}")

    def status(self) -> dict[str, object]:
        """Read public daemon status and require a valid response."""
        result = run("status", "--socket", str(self.socket))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def wait_for_status(
        self, predicate: Callable[[dict[str, object]], bool]
    ) -> dict[str, object]:
        """Wait for a status predicate to observe reconciliation."""
        until = time.monotonic() + 5
        while time.monotonic() < until:
            status = self.status()
            if predicate(status):
                return status
            time.sleep(0.05)
        raise AssertionError("timed out waiting for status")

    def publish_count(self) -> int:
        """Count snapshot publications without client audit entries."""
        return sum(
            json.loads(line)["event"] == "publish"
            for line in (self.state / "audit.jsonl").read_text().splitlines()
        )

    def test_dual_append_recall_is_cited_and_sanitized(self) -> None:
        """Index complete Codex and Claude records without manual sync."""
        (self.codex / "one.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": "2026-09-23T00:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "codex-needle ready",
                        "cwd": "/repo",
                    },
                }
            )
            + "\n"
        )
        (self.claude / "two.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-09-23T00:00:01Z",
                    "cwd": "/repo",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "claude-needle ready"},
                            {"type": "thinking", "thinking": "hidden"},
                            {"type": "tool_use", "name": "Bash"},
                        ],
                    },
                }
            )
            + "\n"
        )
        codex = self.wait_for("codex-needle")
        claude = self.wait_for("claude-needle")
        codex_evidence = codex["evidence"]
        claude_evidence = claude["evidence"]
        self.assertIsInstance(codex_evidence, list)
        self.assertIsInstance(claude_evidence, list)
        codex_items = cast(list[object], codex_evidence)
        claude_items = cast(list[object], claude_evidence)
        codex_item = cast(dict[str, object], codex_items[0])
        claude_item = cast(dict[str, object], claude_items[0])
        codex_source = cast(dict[str, object], codex_item["source"])
        claude_source = cast(dict[str, object], claude_item["source"])
        self.assertEqual(codex_source["provider"], "codex")
        self.assertEqual(claude_source["provider"], "claude")
        self.assertNotIn("hidden", json.dumps(claude))
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)
        storage = cast(dict[str, object], self.status()["storage"])
        self.assertEqual(storage["journal_mode"], "wal")
        self.assertIsInstance(storage["wal_bytes"], int)
        self.assertIsInstance(storage["checkpoint"], list)

    def test_normalizer_ignores_tool_scope_for_direct_messages(self) -> None:
        """Do not inherit a tool item's repository scope into evidence."""
        source = self.codex / "scope.jsonl"
        source.write_text(
            json.dumps(
                {
                    "payload": {
                        "items": [
                            {
                                "type": "function_call_output",
                                "cwd": "/repo-a",
                            },
                            {
                                "type": "message",
                                "role": "assistant",
                                "cwd": "/repo-b",
                                "content": "scope-needle",
                            },
                        ]
                    }
                }
            )
            + "\n"
        )
        events, error, pending = parse_source("codex", self.codex, source)
        self.assertIsNone(error)
        self.assertFalse(pending)
        self.assertEqual(events[0]["cwd"], "/repo-b")

    def test_restart_partial_tail_and_unavailable_client_are_safe(
        self,
    ) -> None:
        """Reconcile missed writes and retry partial data safely."""
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=5)
        if self.process.stderr:
            self.process.stderr.close()
        source = self.codex / "recovery.jsonl"
        complete = {
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "restart-needle ready",
                "cwd": "/repo",
            }
        }
        source.write_text(json.dumps(complete) + "\n")
        self.process = self.start()
        self.wait_for("restart-needle")
        partial = json.dumps(
            {
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "tail-needle ready",
                    "cwd": "/repo",
                }
            }
        )
        with source.open("a") as stream:
            stream.write(partial[:-1])
        pending = self.wait_for_status(
            lambda status: status["pending_sources"] == 1
        )
        audit_count = self.publish_count()
        time.sleep(0.15)
        stable = self.status()
        self.assertEqual(stable["generation"], pending["generation"])
        self.assertEqual(self.publish_count(), audit_count)
        self.assertNotIn("tail-needle", json.dumps(self.recall("tail-needle")))
        with source.open("a") as stream:
            stream.write(partial[-1:] + "\n")
        self.wait_for("tail-needle")
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=5)
        unavailable = run(
            "recall",
            "--socket",
            str(self.socket),
            "--prompt",
            "restart-needle",
        )
        self.assertEqual(unavailable.returncode, 0)
        self.assertFalse(json.loads(unavailable.stdout)["available"])
        hook = subprocess.run(
            [sys.executable, str(CODEX_HOOK)],
            input=json.dumps(
                {"hook_event_name": "UserPromptSubmit", "prompt": "needle"}
            ),
            env=os.environ | {"PROVENANCE_CONTEXT_SOCKET": str(self.socket)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertEqual(json.loads(hook.stdout), {})

    def test_active_tail_excludes_its_source_without_fencing_healthy_recall(
        self,
    ) -> None:
        """Serve healthy evidence while a separate source has a write tail."""
        tail = self.codex / "active-tail.jsonl"
        healthy = self.claude / "healthy.jsonl"
        tail.write_text(e6_record("codex", "tail-prior"))
        healthy.write_text(e6_record("claude", "healthy-during-tail"))
        self.wait_for("tail-prior")
        self.wait_for("healthy-during-tail")
        with tail.open("a") as stream:
            stream.write(e6_record("codex", "tail-incomplete")[:-1])
        status = self.wait_for_status(
            lambda packet: packet["pending_sources"] == 1
            and bool(packet["available"])
        )
        self.assertFalse(status["last_error"])
        self.assertTrue(self.recall("healthy-during-tail")["evidence"])
        self.assertEqual(self.recall("tail-prior")["evidence"], [])
        self.assertEqual(self.recall("tail-incomplete")["evidence"], [])
        self.assertEqual(
            run("doctor", "--socket", str(self.socket)).returncode, 1
        )

    def test_status_truncates_many_redacted_sources(self) -> None:
        """Keep status and doctor bounded for a large healthy corpus."""
        for index in range(80):
            (self.codex / f"many-{index}.jsonl").write_text(
                e6_record("codex", f"many-source-{index}")
            )
        self.wait_for("many-source-79")
        status = run("status", "--socket", str(self.socket))
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertLess(len(status.stdout.encode()), MAX_REQUEST_BYTES)
        packet = json.loads(status.stdout)
        self.assertEqual(packet["source_count"], 80)
        self.assertTrue(packet["sources_truncated"])
        self.assertLess(len(packet["sources"]), packet["source_count"])
        self.assertEqual(
            run("doctor", "--socket", str(self.socket)).returncode, 0
        )

    def test_session_start_reads_many_error_statuses_within_8k(self) -> None:
        """Keep hook status usable when malformed sources fill diagnostics."""
        for index in range(40):
            (self.codex / f"broken-{index}.jsonl").write_text("{malformed}\n")
        status = self.wait_for_status(
            lambda packet: packet["error_sources"] == 40
            and bool(packet["available"])
        )
        self.assertLess(len(json.dumps(status).encode()), MAX_REQUEST_BYTES)
        until = time.monotonic() + 5
        hook = None
        while time.monotonic() < until:
            hook = subprocess.run(
                [sys.executable, str(CODEX_HOOK)],
                input=json.dumps({"hook_event_name": "SessionStart"}),
                env=os.environ
                | {"PROVENANCE_CONTEXT_SOCKET": str(self.socket)},
                capture_output=True,
                text=True,
                check=False,
            )
            if "Provenance index: available." in hook.stdout:
                break
        self.assertIsNotNone(hook)
        assert hook is not None
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertIn("Provenance index: available.", hook.stdout)

    def test_malformed_source_quarantines_stale_evidence_and_recovers(
        self,
    ) -> None:
        """Never inject a source after a complete malformed JSON line."""
        source = self.codex / "broken.jsonl"
        valid = {
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "quarantine-needle ready",
                "cwd": "/repo",
            }
        }
        source.write_text(json.dumps(valid) + "\n")
        self.wait_for("quarantine-needle")
        healthy = {
            "codex": self.codex / "healthy-codex.jsonl",
            "claude": self.claude / "healthy-claude.jsonl",
        }
        for provider, path in healthy.items():
            path.write_text(e6_record(provider, f"healthy{provider}error"))
            self.wait_for(f"healthy{provider}error")

        with source.open("a") as stream:
            stream.write("{malformed}\n")
        degraded = self.wait_for_status(
            lambda status: bool(status["last_error"])
            and bool(status["available"])
        )
        self.assertEqual(degraded["pending_sources"], 1)
        self.assertEqual(degraded["error_sources"], 1)
        self.assertTrue(degraded["available"])
        self.assertEqual(self.recall("quarantine-needle")["evidence"], [])
        for provider in healthy:
            self.assertTrue(
                self.wait_for(f"healthy{provider}error")["evidence"]
            )
        doctor = run("doctor", "--socket", str(self.socket))
        self.assertEqual(doctor.returncode, 1)
        source.write_text(json.dumps(valid) + "\n")
        self.wait_for("quarantine-needle")
        recovered = self.wait_for_status(
            lambda status: not status["last_error"]
        )
        self.assertTrue(recovered["available"])

    def test_repaired_sources_accept_terminal_json_replacements(self) -> None:
        """Recover both providers when a replacement omits its final LF."""
        sources = {
            "codex": self.codex / "e6-codex.jsonl",
            "claude": self.claude / "e6-claude.jsonl",
        }
        for source in sources.values():
            source.write_text("{malformed}\n")
        self.wait_for_status(
            lambda status: status["pending_sources"] == 2
            and bool(status["last_error"])
        )
        for provider, source in sources.items():
            source.write_text(
                e6_record(provider, f"{provider}-e6-repair-" + "x" * 100)
                + "\n"
            )
        for provider in sources:
            self.wait_for(f"{provider}-e6-repair")
        for source in sources.values():
            with source.open("a") as stream:
                stream.write("{")
        self.wait_for_status(
            lambda status: status["pending_sources"] == 2
            and not status["last_error"]
        )
        for provider, source in sources.items():
            source.write_text(
                e6_record(provider, f"{provider}-e6-repair-" + "x" * 100)
                + "\n"
            )
        for provider, source in sources.items():
            source.write_text(
                e6_record(provider, f"{provider}-e6-replacement")
            )
        for provider in sources:
            self.wait_for(f"{provider}-e6-replacement")
            self.assertNotIn(
                f"{provider}-e6-repair",
                json.dumps(self.recall(f"{provider}-e6-repair")),
            )
        status = self.wait_for_status(
            lambda packet: bool(packet["available"])
            and packet["pending_sources"] == 0
        )
        self.assertFalse(status["last_error"])

    def test_doctor_and_stalled_client_contract(self) -> None:
        """Doctor is fail-closed and a stalled peer cannot block the daemon."""
        self.wait_for_status(lambda packet: packet.get("available") is True)
        self.assertEqual(
            run("doctor", "--socket", str(self.socket)).returncode, 0
        )
        unavailable = run(
            "doctor", "--socket", str(self.socket.parent / "none")
        )
        self.assertEqual(unavailable.returncode, 1)
        stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stalled.connect(str(self.socket))
        stalled.sendall(b'{"op":"status"')
        started = time.monotonic()
        healthy = self.wait_for_status(
            lambda packet: packet.get("available") is True
        )
        stalled.close()
        self.assertTrue(healthy["available"])
        self.assertLess(time.monotonic() - started, 1)

    def test_quoted_secret_and_launchd_setup_are_safe(self) -> None:
        """Suppress quoted credentials and document launchd state setup."""
        source = self.codex / "quoted.jsonl"
        source.write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": '{"password":"quoted-secret-value"}',
                        "cwd": "/repo",
                    }
                }
            )
            + "\n"
        )
        time.sleep(0.15)
        self.assertEqual(self.recall("quoted-secret-value")["evidence"], [])
        hook = subprocess.run(
            [sys.executable, str(CODEX_HOOK)],
            input=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "quoted-secret-value",
                    "cwd": "/repo",
                }
            ),
            env=os.environ | {"PROVENANCE_CONTEXT_SOCKET": str(self.socket)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotIn("quoted-secret-value", hook.stdout)
        instructions = README.read_text()
        self.assertIn(
            'mkdir -p "$HOME/.local/share/provenance-context"', instructions
        )
        self.assertIn(
            'chmod 700 "$HOME/.local/share/provenance-context"', instructions
        )

    def test_nested_source_quarantines_only_its_own_evidence_until_repair(
        self,
    ) -> None:
        """A deep source line leaves healthy evidence available."""
        good = self.codex / "good.jsonl"
        good.write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "other-needle",
                        "cwd": "/repo",
                    }
                }
            )
            + "\n"
        )
        self.wait_for("other-needle")
        nested: object = {}
        for _ in range(1100):
            nested = {"next": nested}
        bad = self.codex / "deep.jsonl"
        bad.write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "deep",
                        "deep": nested,
                    }
                }
            )
            + "\n"
        )
        self.wait_for_status(
            lambda status: bool(status["last_error"])
            and bool(status["available"])
        )
        self.assertTrue(self.wait_for("other-needle")["evidence"])
        self.assertEqual(
            run("doctor", "--socket", str(self.socket)).returncode, 1
        )
        bad.write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "repaired-deep",
                        "cwd": "/repo",
                    }
                }
            )
            + "\n"
        )
        self.wait_for("repaired-deep")

    def test_recall_fence_withholds_old_snapshot_during_reconcile(
        self,
    ) -> None:
        """The admission fence returns empty rather than old evidence."""
        source = self.codex / "fence.jsonl"
        source.write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "fence-needle",
                        "cwd": "/repo",
                    }
                }
            )
            + "\n"
        )
        self.wait_for("fence-needle")
        database = Path(self.temporary.name) / "fence-context.sqlite"
        state = Path(self.temporary.name) / "fence-state"
        brain = Brain(self.codex, self.claude, database, state)
        brain.reconcile()
        brain.reconciling = True
        self.assertEqual(
            brain.recall({"prompt": "fence-needle", "repo": "/repo"})[
                "evidence"
            ],
            [],
        )
        brain.reconciling = False
        self.assertTrue(
            brain.recall({"prompt": "fence-needle", "repo": "/repo"})[
                "evidence"
            ]
        )

    def test_recall_never_mixes_snapshots_during_reconciliation(self) -> None:
        """A recall admitted during replacement returns no stale packet."""
        source = self.codex / "atomic.jsonl"
        source.write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "atomic-needle",
                        "cwd": "/repo",
                    }
                }
            )
            + "\n"
        )
        database = Path(self.temporary.name) / "atomic-context.sqlite"
        state = Path(self.temporary.name) / "atomic-state"
        brain = Brain(self.codex, self.claude, database, state)
        brain.reconcile()
        self.assertTrue(
            brain.recall({"prompt": "atomic-needle", "repo": "/repo"})[
                "evidence"
            ]
        )
        source.write_text("{malformed}\n")
        entered = threading.Event()
        release = threading.Event()
        from scan import parse_source_incremental as real_parse_source

        def delayed_parse(
            provider: str,
            root: Path,
            path: Path,
            offset: int,
            line: int,
        ) -> tuple[
            list[dict[str, str | int | None]], str | None, bool, int, int
        ]:
            entered.set()
            self.assertTrue(release.wait(2))
            return real_parse_source(provider, root, path, offset, line)

        with patch("scan.parse_source_incremental", side_effect=delayed_parse):
            worker = threading.Thread(target=brain.reconcile)
            worker.start()
            self.assertTrue(entered.wait(2))
            self.assertEqual(
                brain.recall({"prompt": "atomic-needle", "repo": "/repo"})[
                    "evidence"
                ],
                [],
            )
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(
            brain.recall({"prompt": "atomic-needle", "repo": "/repo"})[
                "evidence"
            ],
            [],
        )

    def test_uds_serves_fenced_requests_during_background_reconcile(
        self,
    ) -> None:
        """Keep UDS admission responsive while the sole writer is slow."""
        base = Path(self.temporary.name) / "background"
        codex, claude = base / "codex", base / "claude"
        codex.mkdir(parents=True)
        claude.mkdir()
        database, state = base / "context.sqlite", base / "state"
        Brain(codex, claude, database, state).reconcile()
        (codex / "slow.jsonl").write_text(e6_record("codex", "slow-needle"))
        socket_path = base / "brain.sock"
        entered, release, stopping = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def delayed_parse(
            provider: str,
            root: Path,
            path: Path,
            offset: int,
            line: int,
        ) -> tuple[
            list[dict[str, str | int | None]], str | None, bool, int, int
        ]:
            entered.set()
            release.wait(2)
            return parse_source_incremental(provider, root, path, offset, line)

        with patch("scan.parse_source_incremental", side_effect=delayed_parse):
            worker = threading.Thread(
                target=serve,
                args=(
                    codex,
                    claude,
                    database,
                    state,
                    socket_path,
                    60.0,
                    stopping,
                ),
            )
            worker.start()
            self.assertTrue(entered.wait(2))
            audit_path = state / "audit.jsonl"
            audit_before = (
                audit_path.stat().st_size if audit_path.exists() else 0
            )
            status = service_request(socket_path, {"op": "status"})
            statuses = [
                service_request(socket_path, {"op": "status"})
                for _ in range(20)
            ]
            audit_after = (
                audit_path.stat().st_size if audit_path.exists() else 0
            )
            brain = RequestHandler.brain
            brain.pending = 1
            self.assertTrue(brain.lock.acquire(blocking=False))
            try:
                status_started = time.monotonic()
                fence_status = service_request(socket_path, {"op": "status"})
                status_latency = time.monotonic() - status_started
                started = time.monotonic()
                recall = service_request(
                    socket_path,
                    {"op": "recall", "prompt": "slow-needle"},
                )
                recall_latency = time.monotonic() - started
                fence_audit = (
                    audit_path.stat().st_size if audit_path.exists() else 0
                )
            finally:
                brain.lock.release()
            release.set()
            stopping.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(status["state"], "reconciling")
        self.assertTrue(
            all(packet["state"] == "reconciling" for packet in statuses)
        )
        self.assertEqual(audit_after, audit_before)
        self.assertEqual(fence_audit, audit_before)
        self.assertFalse(recall["available"])
        self.assertEqual(recall["unavailable_reason"], "reconciling")
        self.assertEqual(fence_status["state"], "reconciling")
        self.assertLess(status_latency, 0.2)
        self.assertLess(recall_latency, 0.2)
        self.assertEqual(recall["unavailable_reason"], "reconciling")

    def test_client_saturation_and_audit_status_are_bounded(self) -> None:
        """Slow peers are capped and audit data is visible without content."""
        peers = [
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            for _ in range(MAX_CLIENTS)
        ]
        try:
            for peer in peers:
                peer.connect(str(self.socket))
            time.sleep(0.05)
            saturated = run("status", "--socket", str(self.socket))
            self.assertEqual(saturated.returncode, 0)
            self.assertFalse(json.loads(saturated.stdout)["available"])
            for peer in peers:
                peer.close()
            time.sleep(0.3)
            status = self.wait_for_status(
                lambda packet: isinstance(
                    packet.get("requests_saturated"), int
                )
                and cast(int, packet["requests_saturated"]) >= 1
            )
            state = json.loads((self.state / "state.json").read_text())
            for key in (
                "audit_bytes",
                "audit_rotations",
                "audit_dropped",
                "requests_rejected",
                "requests_saturated",
            ):
                self.assertEqual(status[key], state[key])
            self.assertGreaterEqual(cast(int, status["requests_rejected"]), 1)
            self.assertGreaterEqual(cast(int, status["audit_bytes"]), 1)
        finally:
            for peer in peers:
                peer.close()

    def test_audit_rotation_and_drop_are_reflected_in_state(self) -> None:
        """Rotation and oversized events retain only redacted counters."""
        brain = Brain(self.codex, self.claude, self.database, self.state)
        with patch("runtime.MAX_AUDIT_BYTES", 200):
            for number in range(4):
                brain.audit("test", str(number), count=number)
        with patch("runtime.MAX_AUDIT_RECORD_BYTES", 50):
            brain.audit("test", "oversized", count="x" * 100)
        brain._write_state()
        status = brain.status()
        state = json.loads((self.state / "state.json").read_text())
        self.assertGreaterEqual(cast(int, status["audit_rotations"]), 1)
        self.assertGreaterEqual(cast(int, status["audit_dropped"]), 1)
        self.assertLessEqual(cast(int, status["audit_bytes"]), 200)
        for key in ("audit_bytes", "audit_rotations", "audit_dropped"):
            self.assertEqual(status[key], state[key])

    def test_audit_and_rejection_counters_survive_a_restart(self) -> None:
        """Retain operational counters across a fresh Brain instance."""
        brain = Brain(self.codex, self.claude, self.database, self.state)
        with patch("runtime.MAX_AUDIT_RECORD_BYTES", 50):
            brain.audit("test", "oversized", count="x" * 100)
        brain.reject_request(saturated=True)
        brain._write_state()
        restarted = Brain(self.codex, self.claude, self.database, self.state)
        metrics = restarted._metrics()
        self.assertGreaterEqual(metrics["audit_dropped"], 1)
        self.assertGreaterEqual(metrics["requests_rejected"], 1)
        self.assertGreaterEqual(metrics["requests_saturated"], 1)

    def test_saturation_rejections_coalesce_before_audit_and_state_write(
        self,
    ) -> None:
        """Keep full client admission from creating synchronous I/O storms."""
        brain = Brain(self.codex, self.claude, self.database, self.state)
        with (
            patch.object(brain, "audit") as audit,
            patch("brain.write_json") as write_state,
        ):
            for _ in range(100):
                brain.reject_request(saturated=True)
            audit.assert_not_called()
            write_state.assert_not_called()
            brain._write_state()
        audit.assert_called_once()
        self.assertEqual(audit.call_args.kwargs["rejected"], 100)
        self.assertEqual(audit.call_args.kwargs["saturated"], 100)
        write_state.assert_called_once()

    def test_append_uses_cursor_and_failpoint_rolls_back_atomically(
        self,
    ) -> None:
        """Append commits once and a failed transaction advances no cursor."""
        source = self.codex / "cursor.jsonl"
        first = {
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "cursor-first",
                "cwd": "/repo",
            }
        }
        second = {
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "cursor-second",
                "cwd": "/repo",
            }
        }
        source.write_text(json.dumps(first) + "\n")
        database = Path(self.temporary.name) / "cursor.sqlite"
        state = Path(self.temporary.name) / "cursor-state"
        brain = Brain(self.codex, self.claude, database, state)
        brain.reconcile()
        with source.open("a") as stream:
            stream.write(json.dumps(second) + "\n")
        with patch(
            "scan.parse_source_incremental", wraps=parse_source_incremental
        ) as parser:
            brain.reconcile()
        self.assertGreater(cast(int, parser.call_args.args[3]), 0)
        self.assertTrue(
            brain.recall({"prompt": "cursor-second", "repo": "/repo"})[
                "evidence"
            ]
        )
        third = {
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "cursor-third",
                "cwd": "/repo",
            }
        }
        with source.open("a") as stream:
            stream.write(json.dumps(third) + "\n")
        with patch.dict(
            os.environ,
            {"PROVENANCE_CONTEXT_TEST_FAILPOINT": "before_commit"},
        ):
            brain.reconcile()
        self.assertEqual(
            brain.recall({"prompt": "cursor-third", "repo": "/repo"})[
                "evidence"
            ],
            [],
        )
        brain.reconcile()
        packet = brain.recall({"prompt": "cursor-third", "repo": "/repo"})
        self.assertEqual(len(cast(list[object], packet["evidence"])), 1)

    def test_named_failpoints_rollback_and_restart_exactly_once(self) -> None:
        """Rollback each event boundary before a restarted writer recovers."""
        append_points = (
            "after_event_insert",
            "after_fts_insert",
            "after_assertion_supersede",
            "after_assertion_insert",
            "after_source_state",
            "after_meta_counts",
            "before_commit",
        )
        replace_points = (
            "after_delete_assertions",
            "after_delete_fts",
            "after_delete_events",
        )
        for point in append_points + replace_points:
            source = self.codex / f"failpoint-{point}.jsonl"
            padding = "x" * 100 if point in replace_points else ""
            baseline = self._codex_record(
                f"I prefer baseline-{point}{padding}"
            )
            source.write_text(baseline)
            database = Path(self.temporary.name) / f"{point}.sqlite"
            state = Path(self.temporary.name) / f"{point}-state"
            brain = Brain(self.codex, self.claude, database, state)
            brain.reconcile()
            before = self._store_state(database, source.name)
            replacement = self._codex_record(f"I prefer recovered-{point}")
            if point in replace_points:
                self.assertLess(len(replacement), len(baseline))
                source.write_text(replacement)
            else:
                with source.open("a") as stream:
                    stream.write(replacement)
            with patch.dict(
                os.environ,
                {"PROVENANCE_CONTEXT_TEST_FAILPOINT": point},
            ):
                brain.reconcile()
            self.assertEqual(self._store_state(database, source.name), before)
            restarted = Brain(self.codex, self.claude, database, state)
            restarted.reconcile()
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM events WHERE text = ?",
                        (f"I prefer recovered-{point}",),
                    ).fetchone(),
                    (1,),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT cursor_bytes FROM sources "
                        "WHERE provider = 'codex' AND source_path = ?",
                        (source.name,),
                    ).fetchone(),
                    (source.stat().st_size,),
                )

    def test_crash_failpoints_restart_with_exact_once_public_evidence(
        self,
    ) -> None:
        """Crash every mutation boundary and recover cited markers once."""
        for point in CRASH_APPEND_POINTS:
            self._crash_case(point, "append")
        for point in CRASH_REPLACE_POINTS:
            self._crash_case(point, "replace")
        for point in CRASH_RENAME_POINTS:
            self._crash_case(point, "rename")

    def _crash_case(self, point: str, mode: str) -> None:
        """Exercise one deterministic crash boundary through the UDS daemon."""
        prior = f"prior-{point}"
        post = f"post-{point}"
        prior_source = self.codex / f"prior-{point}.jsonl"
        source = self.codex / f"crash-{point}-before.jsonl"
        prior_source.write_text(self._codex_record(prior))
        padding = "x" * 100 if mode == "replace" else ""
        baseline = self._codex_record(f"I prefer baseline-{point} {padding}")
        source.write_text(baseline)
        self.wait_for(prior)
        self.wait_for(f"baseline-{point}")
        self._stop_daemon()
        if mode == "replace":
            source.write_text(self._codex_record(f"I prefer {post}"))
        else:
            with source.open("a") as stream:
                stream.write(self._codex_record(f"I prefer {post}"))
            if mode == "rename":
                source.rename(self.codex / f"crash-{point}-after.jsonl")
        self._restart_after_crash(point)
        self._assert_exact_marker(prior)
        self._assert_exact_marker(post)

    def _restart_after_crash(self, point: str) -> None:
        """Restart normally after one deterministic test-only crash."""
        crashed = self.start(
            {
                "PROVENANCE_CONTEXT_TEST_CRASH_ARMED": "1",
                "PROVENANCE_CONTEXT_CRASH_FAILPOINT": point,
            },
            wait_for_socket=False,
        )
        self.assertEqual(crashed.wait(timeout=5), 86)
        if crashed.stderr:
            crashed.stderr.close()
        self.process = self.start()

    def _stop_daemon(self) -> None:
        """Stop the healthy daemon before mutating the crash fixture."""
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=5)
        if self.process.stderr:
            self.process.stderr.close()

    def _assert_exact_marker(self, marker: str) -> None:
        """Assert a public recall reports one source-cited marker."""
        packet = self.wait_for(marker)
        evidence = cast(list[dict[str, object]], packet["evidence"])
        self.assertEqual(
            sum(
                str(item.get("text", "")).endswith(marker) for item in evidence
            ),
            1,
            evidence,
        )

    def _codex_record(self, content: str) -> str:
        """Build one complete Codex user message for storage mutation tests."""
        return (
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": content,
                        "cwd": "/repo",
                    }
                }
            )
            + "\n"
        )

    def _store_state(
        self, database: Path, source_path: str
    ) -> tuple[object, ...]:
        """Read only cursor and derived counts needed to prove rollback."""
        with sqlite3.connect(database) as connection:
            cursor = connection.execute(
                "SELECT cursor_bytes, cursor_line FROM sources "
                "WHERE provider = 'codex' AND source_path = ?",
                (source_path,),
            ).fetchone()
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[
                    0
                ]
                for table in ("events", "event_fts", "assertions")
            )
        return cursor + counts if cursor else counts

    def test_rename_preserves_evidence_without_a_missing_tombstone(
        self,
    ) -> None:
        """Re-key a moved JSONL file by identity instead of fencing recall."""
        source = self.codex / "rename-before.jsonl"
        source.write_text(self._codex_record("rename-needle"))
        self.wait_for("rename-needle")
        source.rename(self.codex / "rename-after.jsonl")
        self.wait_for("rename-needle")
        status = self.wait_for_status(
            lambda packet: bool(packet["available"])
            and not any(
                item.get("status") == "missing"
                for item in cast(list[dict[str, object]], packet["sources"])
            )
        )
        self.assertTrue(status["available"])

    def test_metadata_preserving_rewrite_is_reparsed_by_digest_audit(
        self,
    ) -> None:
        """Detect a same-fingerprint correction through the rotating digest."""
        source = self.codex / "digest-audit.jsonl"
        old = self._codex_record("digest-before")
        new = self._codex_record("digest-after!")
        self.assertEqual(len(old), len(new))
        source.write_text(old)
        database = Path(self.temporary.name) / "digest-audit.sqlite"
        state = Path(self.temporary.name) / "digest-audit-state"
        brain = Brain(self.codex, self.claude, database, state)
        brain.reconcile()
        original = source_fingerprint(source)
        source.write_text(new)
        with patch(
            "scan.discover",
            side_effect=lambda root: (
                [(source.name, source, original)] if root == self.codex else []
            ),
        ):
            brain.reconcile()
        self.assertTrue(
            brain.recall({"prompt": "digest-after", "repo": "/repo"})[
                "evidence"
            ]
        )

    def test_rebuild_state_is_visible_while_raw_recovery_is_fenced(
        self,
    ) -> None:
        """Expose rebuilding before a slow raw-backed recovery can finish."""
        source = self.codex / "rebuilding.jsonl"
        source.write_text(self._codex_record("rebuilding-needle"))
        database = Path(self.temporary.name) / "rebuilding.sqlite"
        state = Path(self.temporary.name) / "rebuilding-state"
        Brain(self.codex, self.claude, database, state).reconcile()
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM event_fts")
            connection.execute("DELETE FROM events")
        entered = threading.Event()
        release = threading.Event()
        from scan import parse_source_incremental as real_parse_source

        def delayed_parse(
            provider: str,
            root: Path,
            path: Path,
            offset: int,
            line: int,
        ) -> tuple[
            list[dict[str, str | int | None]], str | None, bool, int, int
        ]:
            entered.set()
            self.assertTrue(release.wait(2))
            return real_parse_source(provider, root, path, offset, line)

        restarted = Brain(self.codex, self.claude, database, state)
        with patch("scan.parse_source_incremental", side_effect=delayed_parse):
            worker = threading.Thread(target=restarted.reconcile)
            worker.start()
            self.assertTrue(entered.wait(2))
            status = restarted.status()
            self.assertEqual(status["state"], "rebuilding")
            self.assertTrue(status["rebuilding"])
            self.assertFalse(status["available"])
            self.assertGreaterEqual(
                cast(float, status["dirty_fence_seconds"]), 0
            )
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_missing_source_is_tombstoned_and_explicit_erase_removes_it(
        self,
    ) -> None:
        """Erase each provider's tombstone in one public request."""
        sources = {
            "codex": self.codex / "retention-codex.jsonl",
            "claude": self.claude / "retention-claude.jsonl",
        }
        for provider, source in sources.items():
            source.write_text(e6_record(provider, f"I prefer {provider}"))
            self.wait_for(f"I prefer {provider}")
        status = self.status()
        source_ids = {
            cast(str, item["provider"]): cast(str, item["source_id"])
            for item in cast(list[dict[str, object]], status["sources"])
            if item["provider"] in sources
        }
        self.assertEqual(set(source_ids), set(sources))
        for source in sources.values():
            source.unlink()
        self.wait_for_status(
            lambda packet: all(
                item.get("status") == "missing"
                for item in cast(list[dict[str, object]], packet["sources"])
                if item.get("provider") in sources
            )
        )
        for provider in sources:
            response = run(
                "erase",
                "--socket",
                str(self.socket),
                "--provider",
                provider,
                "--source-id",
                source_ids[provider],
            )
            self.assertEqual(response.returncode, 0, response.stderr)
            packet = json.loads(response.stdout)
            self.assertTrue(packet["erased"], packet)
            self.assertTrue(packet["available"])
        status = self.status()
        self.assertTrue(status["available"])
        self.assertEqual(status["pending_sources"], 0)
        self.assertFalse(status["last_error"])
        self.assertFalse(status["sources"])
        self.assertEqual(
            cast(dict[str, object], status["storage"])["derived_counts"],
            {"events": 0, "assertions": 0, "sources": 0},
        )

    def test_status_counts_prove_erasure_removes_linked_assertions(
        self,
    ) -> None:
        """Expose aggregate counts while proving public erasure."""
        preference = self.codex / "preference.jsonl"
        unrelated = self.codex / "unrelated.jsonl"
        preference.write_text(self._codex_record("I prefer terse replies"))
        unrelated.write_text(self._codex_record("unrelated-needle"))
        self.wait_for("terse replies")
        self.wait_for("unrelated-needle")
        status = self.status()
        counts = cast(dict[str, object], status["storage"])["derived_counts"]
        self.assertEqual(counts, {"events": 2, "assertions": 1, "sources": 2})
        self.assertNotIn("preference.jsonl", json.dumps(status))
        self.assertNotIn("terse replies", json.dumps(status))
        source_id = hashlib.sha256(preference.name.encode()).hexdigest()[:16]
        preference.unlink()
        self.wait_for_status(
            lambda packet: any(
                item.get("source_id") == source_id
                and item.get("status") == "missing"
                for item in cast(list[dict[str, object]], packet["sources"])
            )
        )
        until = time.monotonic() + 5
        erased = False
        while time.monotonic() < until:
            result = run(
                "erase",
                "--socket",
                str(self.socket),
                "--provider",
                "codex",
                "--source-id",
                source_id,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            erased = bool(json.loads(result.stdout)["erased"])
            if erased:
                break
            time.sleep(0.05)
        self.assertTrue(erased)
        status = self.wait_for_status(
            lambda packet: cast(dict[str, object], packet["storage"])[
                "derived_counts"
            ]
            == {"events": 1, "assertions": 0, "sources": 1}
        )
        self.assertNotIn("preference.jsonl", json.dumps(status))
        self._assert_exact_marker("unrelated-needle")

    def test_v1_database_is_backed_up_and_rebuilt_from_raw(self) -> None:
        """WAL cutover retains rollback data and reads the raw corpus."""
        source = self.codex / "migration.jsonl"
        source.write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "migration-needle",
                        "cwd": "/repo",
                    }
                }
            )
            + "\n"
        )
        database = Path(self.temporary.name) / "migration.sqlite"
        state = Path(self.temporary.name) / "migration-state"
        build_index(self.codex, database)
        brain = Brain(self.codex, self.claude, database, state)
        brain.reconcile()
        self.assertTrue((state / "v1-rollback.sqlite").exists())
        self.assertTrue(
            brain.recall({"prompt": "migration-needle", "repo": "/repo"})[
                "evidence"
            ]
        )

    def test_corrupt_derived_database_rebuilds_from_raw_on_restart(
        self,
    ) -> None:
        """Rebuild a disposable corrupted database from the raw source."""
        source = self.codex / "rebuild.jsonl"
        source.write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "rebuild-needle",
                        "cwd": "/repo",
                    }
                }
            )
            + "\n"
        )
        self.wait_for("rebuild-needle")
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=5)
        if self.process.stderr:
            self.process.stderr.close()
        self.database.write_bytes(b"not a sqlite database")
        self.process = self.start()
        self.wait_for("rebuild-needle")
        self.assertTrue((self.state / "v1-rollback.sqlite").exists())

    def test_partial_derived_loss_rebuilds_from_raw_on_restart(self) -> None:
        """Reject intact cursors missing events instead of serving empty."""
        source = self.codex / "partial-loss.jsonl"
        source.write_text(self._codex_record("partial-loss-needle"))
        self.wait_for("partial-loss-needle")
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=5)
        if self.process.stderr:
            self.process.stderr.close()
        with sqlite3.connect(self.database) as connection:
            self.assertTrue(
                connection.execute("SELECT COUNT(*) FROM sources").fetchone()[
                    0
                ]
            )
            connection.execute("DELETE FROM event_fts")
            connection.execute("DELETE FROM events")
        self.process = self.start()
        packet = self.wait_for("partial-loss-needle")
        self.assertTrue(packet["available"])
        self.assertEqual(len(cast(list[object], packet["evidence"])), 1)
        self.assertTrue((self.state / "v1-rollback.sqlite").exists())

    def test_wal_readers_observe_only_cited_packets_or_fenced_unavailable(
        self,
    ) -> None:
        """Concurrent readers remain coherent while an append is indexed."""
        source = self.codex / "readers.jsonl"
        source.write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "reader-first",
                        "cwd": "/repo",
                    }
                }
            )
            + "\n"
        )
        self.wait_for("reader-first")
        with source.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "reader-second",
                            "cwd": "/repo",
                        }
                    }
                )
                + "\n"
            )
        packets = [self.recall("reader") for _ in range(20)]
        packets.append(self.wait_for("reader-second"))
        for packet in packets:
            if not packet["available"]:
                self.assertEqual(packet["evidence"], [])
                continue
            for item in cast(list[dict[str, object]], packet["evidence"]):
                source_data = cast(dict[str, object], item["source"])
                self.assertEqual(source_data["provider"], "codex")
                self.assertIsInstance(source_data["line"], int)
                self.assertTrue(source_data["hash"])
        storage = cast(dict[str, object], self.status()["storage"])
        self.assertEqual(storage["journal_mode"], "wal")
        self.assertIsInstance(storage["checkpoint_at"], float)
        self.assertIsInstance(storage["checkpoint_busy"], bool)
        self.assertIsInstance(storage["checkpoint_log_frames"], int)
        self.assertIsInstance(storage["checkpointed_frames"], int)
        self.assertIsInstance(self.status()["dirty_fence_seconds"], float)

    def test_checkpoint_probe_is_public_bounded_and_releases_reader(
        self,
    ) -> None:
        """Hold a daemon reader while public append and recall clients run."""
        source = self.codex / "probe.jsonl"
        source.write_text(self._codex_record("probe-first"))
        self.wait_for("probe-first")
        probe = subprocess.Popen(
            [
                sys.executable,
                str(CONTEXT),
                "checkpoint-probe",
                "--socket",
                str(self.socket),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(0.05)
            self.assertIsNone(probe.poll())
            with source.open("a") as stream:
                stream.write(self._codex_record("probe-second"))
            packets = [self.recall("probe") for _ in range(4)]
            for packet in packets:
                if packet["available"]:
                    self.assertIn("evidence", packet)
                else:
                    self.assertEqual(packet["evidence"], [])
            storage = cast(dict[str, object], self.status()["storage"])
            self.assertEqual(storage["journal_mode"], "wal")
            output, error = probe.communicate(timeout=2)
        finally:
            if probe.poll() is None:
                probe.kill()
                probe.wait(timeout=2)
            if probe.stdout:
                probe.stdout.close()
            if probe.stderr:
                probe.stderr.close()
        self.assertEqual(probe.returncode, 0, error)
        response = json.loads(output)
        self.assertEqual(response["reader"], "released")
        self.assertEqual(response["held_ms"], 150)
        self.assertNotIn("database", response)
        self.wait_for("probe-second")


if __name__ == "__main__":
    unittest.main()
