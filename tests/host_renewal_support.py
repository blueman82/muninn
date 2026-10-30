"""Temporary current-format installations for checked host-renewal tests."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from install import policy_preview
from install.host_renewal_models import ContainmentPlan
from install.host_renewal_preview import StateScan, scan_state
from muninn import review_codex_policy, review_pause_state, store
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_progress import bootstrap_policy, digest
from tests.installer_support import ROOT, World, git
from tests.policy_preview_support import states as states
from tools import prepare_codex_policy


@unittest.skipIf(sys.platform == "win32", "macOS private-path contract")
class HostFixture(unittest.TestCase):
    """Create retained policy artifacts with a different saved host."""

    def setUp(self) -> None:
        self.world = World(self)
        self.home, self.repo = self.world.home, self.world.repo
        target = self.repo / "muninn/review_codex_output.py"
        shutil.copy2(ROOT / "muninn/review_codex_output.py", target)
        git(self.repo, "add", "muninn/review_codex_output.py")
        git(self.repo, "commit", "-qm", "synthetic output contract")
        self.sha = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.home.chmod(0o700)
        self.ctx = self.world.ctx(upgrade=True, dry_run=True)
        self.ctx.data.mkdir(mode=0o700, parents=True)
        self.path = store.db_path(self.ctx.data)
        actual = review_codex_policy.host_identity()
        self.old_host = "1" * 64 if actual != "1" * 64 else "2" * 64
        self.current_host = "3" * 64
        self.executable = Path(sys.executable).resolve()
        for site in (
            "muninn.review_codex_policy.host_identity",
            "muninn.review_codex_launch.host_identity",
            "tools.prepare_codex_policy.host_identity",
        ):
            self.enterContext(mock.patch(site, return_value=self.old_host))
        self.enterContext(mock.patch("platform.system", return_value="Darwin"))
        self.enterContext(mock.patch("platform.machine", return_value="arm64"))
        self.enterContext(
            mock.patch(
                "muninn.review_codex_launch.verify_codex_executable",
                return_value=(self.executable, "a" * 64),
            )
        )
        self.policy_path = prepare_codex_policy.prepare(
            self.ctx.data,
            self.executable,
            self.ctx.codex_home,
            "fixture-owner",
            1.0,
            revision=2,
        )
        self.old = ReviewPolicy.model_validate_json(
            self.policy_path.read_bytes()
        )
        candidate = self.old.model_dump(mode="json")
        candidate["experimental_codex"]["host_identity"] = self.current_host
        self.candidate = ReviewPolicy.model_validate_json(
            json.dumps(candidate)
        )
        self.ctx.review_policy = self.home / "candidate.json"
        self.write_candidate(self.candidate)
        self.enterContext(
            mock.patch(
                "muninn.review_codex_policy.host_identity",
                return_value=self.current_host,
            )
        )
        with (
            store.writer_lock(self.ctx.data),
            closing(store.connect_rw(self.path)) as conn,
        ):
            conn.execute("BEGIN IMMEDIATE")
            bootstrap_policy(conn, self.old, 1.0)
            conn.execute("COMMIT")
        self.prepare_installation()

    def prepare_installation(self) -> None:
        """Create the existing witnessed owner and selected runtime fixture."""
        lock = self.ctx.data / review_pause_state.LOCK
        lock.write_bytes(b"")
        lock.chmod(0o600)
        identity = lock.stat()
        witness = {
            "version": 1,
            "lock_identity": [identity.st_dev, identity.st_ino],
            "owner": None,
            "state": "complete",
        }
        witness_path = self.ctx.data / review_pause_state.WITNESS
        witness_path.write_text(json.dumps(witness))
        witness_path.chmod(0o600)
        runtime = self.ctx.lib / ("b" * 40)
        runtime.mkdir(parents=True, mode=0o700)
        (runtime / "marker").write_bytes(b"installed runtime")
        self.ctx.lib.chmod(0o700)
        (self.ctx.lib / "current").symlink_to(runtime.name)
        (self.ctx.lib / "python").symlink_to(self.executable)
        self.ctx.muninn.symlink_to(runtime / "marker")
        self.ctx.plist.parent.mkdir(parents=True, exist_ok=True)
        self.ctx.plist.write_bytes(b"fixture service")

    def write_candidate(self, policy: ReviewPolicy) -> None:
        """Save the supplied candidate in the temporary home."""
        assert self.ctx.review_policy is not None
        self.ctx.review_policy.write_text(canonical_json(policy))
        self.ctx.review_policy.chmod(0o600)

    def sql(self, statement: str, values: tuple[object, ...] = ()) -> None:
        """Commit one mutation with a temporary SQLite writer."""
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(statement, values)
            conn.commit()

    def request(
        self,
        state: str = "pending",
        *,
        candidate: bool = False,
        raw: str | None = None,
    ) -> str:
        """Insert a canonical request independent of preview output."""
        policy = self.candidate if candidate else self.old
        ident = "R_" + "1" * 32
        self.sql(
            "INSERT INTO review_request(id,duplicate_key,provider,thread_id,"
            "session_root,from_line,through_line,boundary_hash,scope_json,"
            "policy_json,policy_digest,trigger,origin,state,created_at) "
            "VALUES(?,?,'codex','fixture','fixture',1,1,?,?,?,?,'owner','review',?,1.0)",
            (
                ident,
                hashlib.sha256(ident.encode()).hexdigest(),
                "a" * 64,
                '{"project_id":"fixture","task":null}',
                raw if raw is not None else canonical_json(policy),
                digest(policy),
                state,
            ),
        )
        return ident

    def attempt(self, request_id: str) -> str:
        """Attach an unfinished attempt with its requested runner."""
        ident = "A_" + "1" * 32
        self.sql(
            "INSERT INTO review_attempt(id,request_id,generation,"
            "requested_json,"
            "observed_json,started_at,progress_at) VALUES(?,?,1,?,?,2.0,2.0)",
            (
                ident,
                request_id,
                canonical_json(self.old.runner),
                (
                    '{"runner":null,"version":null,'
                    '"model":null,"reasoning":null}'
                ),
            ),
        )
        self.sql(
            "UPDATE review_request SET current_attempt_id=?,state='running' "
            "WHERE id=?",
            (ident, request_id),
        )
        return ident

    def insert(self, table: str, values: dict[str, object]) -> None:
        """Insert a literal synthetic row with named columns."""
        columns = ",".join(values)
        slots = ",".join("?" for _ in values)
        self.sql(
            f"INSERT INTO {table}({columns}) VALUES({slots})",
            tuple(values.values()),
        )

    def seed_inventory(self) -> None:
        """Populate every inventoried table without provider source bodies."""
        request = self.request("completed")
        attempt = self.attempt(request)
        self.sql(
            "UPDATE review_attempt SET ended_at=3.0,progress_at=3.0,"
            "outcome='completed'"
        )
        self.sql(
            "UPDATE review_request SET state='completed',"
            "current_attempt_id=NULL"
        )
        self.insert(
            "review_source",
            {
                "provider": "codex",
                "thread_id": "fixture",
                "activation_line": 0,
                "examined_line": 1,
                "eligible_line": 1,
                "completed_line": 1,
                "updated_at": 1.0,
            },
        )
        self.insert(
            "source",
            {
                "provider": "codex",
                "thread_id": "fixture",
                "session_root": "fixture",
                "thread_class": "primary",
                "class_reason": "synthetic",
                "replay_mode": "none",
                "root": "codex-sessions",
                "path": "synthetic.jsonl",
                "first_line_sha256": "a" * 64,
                "ino": 1,
                "size": 0,
                "mtime_ns": 1,
                "classifier_version": 1,
                "first_seen": 1.0,
                "last_seen": 1.0,
            },
        )
        self.insert(
            "tombstone",
            {
                "provider": "codex",
                "level": "thread",
                "thread_id": "erased-fixture",
                "created_at": 1.0,
            },
        )
        self.insert(
            "learning_setup",
            {"singleton": 1, "mode": "now", "selected_at": 1.0},
        )
        self.insert(
            "review_issue",
            {
                "id": "fixture",
                "code": "config",
                "stage": "startup",
                "cause": "safe",
                "action": "safe",
                "created_at": 1.0,
            },
        )
        self.insert(
            "review_call",
            {
                "attempt_id": attempt,
                "round_index": 1,
                "budget_account_id": "synthetic-account",
                "policy_digest": digest(self.old),
                "state": "completed",
                "reserved_at": 2.0,
                "requested_json": "{}",
                "observed_json": "{}",
            },
        )
        self.insert(
            "review_receipt",
            {
                "request_id": request,
                "attempt_id": attempt,
                "applied_json": "[]",
                "unresolved_json": "[]",
                "committed_at": 3.0,
                "outcome": "no_change",
            },
        )
        self.seed_knowledge(request)

    def seed_knowledge(self, request: str) -> None:
        """Add linked synthetic evidence, knowledge and decision metadata."""
        evidence, knowledge, candidate = (
            "E_" + "1" * 32,
            "K_" + "1" * 32,
            "candidate",
        )
        self.insert(
            "review_evidence",
            {
                "id": evidence,
                "provider": "codex",
                "thread_id": "fixture",
                "session_root": "fixture",
                "line": 1,
                "part": 1,
                "line_sha256": "a" * 64,
                "source_version": "synthetic",
                "projection_version": "permitted-fields-v2",
                "original_locator_json": "{}",
                "selector_json": "{}",
                "selector_digest": "b" * 64,
                "evidence_type": "message",
                "authority": "owner",
                "scope_json": "{}",
                "state": "live",
                "selected_at": 1.0,
            },
        )
        self.insert(
            "review_input", {"request_id": request, "evidence_id": evidence}
        )
        self.insert(
            "knowledge",
            {"id": knowledge, "current_revision": 1, "state": "active"},
        )
        self.insert(
            "knowledge_version",
            {
                "knowledge_id": knowledge,
                "revision": 1,
                "request_id": request,
                "form": "note",
                "kind": "fact",
                "scope_json": "{}",
                "content_json": "{}",
                "origin": "review",
                "created_at": 3.0,
            },
        )
        self.insert(
            "knowledge_claim",
            {
                "knowledge_id": knowledge,
                "revision": 1,
                "claim_key": "claim",
                "text": "synthetic",
                "dependency_keys_json": "[]",
                "state": "live",
            },
        )
        self.insert(
            "knowledge_support",
            {
                "knowledge_id": knowledge,
                "revision": 1,
                "claim_key": "claim",
                "evidence_id": evidence,
            },
        )
        self.insert(
            "unresolved",
            {
                "id": "U_" + "1" * 32,
                "request_id": request,
                "item_json": "{}",
                "affected_json": "[]",
                "support_json": "[]",
                "created_at": 3.0,
                "state": "open",
            },
        )
        self.seed_decisions(request, evidence, candidate)

    def seed_decisions(
        self, request: str, evidence: str, candidate: str
    ) -> None:
        """Add immutable synthetic candidates and their linked decisions."""
        self.insert(
            "memory_candidate",
            {
                "id": candidate,
                "request_id": request,
                "payload_json": "{}",
                "digest": "c" * 64,
                "normalization_version": 1,
                "enrichment_version": 1,
                "state": "waiting_verification",
                "created_at": 3.0,
            },
        )
        self.insert(
            "memory_decision",
            {
                "id": "decision",
                "candidate_id": candidate,
                "candidate_digest": "c" * 64,
                "intent_key": "intent",
                "action": "reject",
                "submitted_at": 4.0,
                "state": "waiting_verification",
            },
        )
        self.insert(
            "memory_candidate_support",
            {
                "candidate_id": candidate,
                "evidence_id": evidence,
                "role": "original",
            },
        )

    def scan(self, containment: ContainmentPlan | None = None) -> StateScan:
        """Read a fresh guarded temporary snapshot for containment tests."""
        with policy_preview.connection(self.path) as conn:
            return scan_state(
                conn,
                self.path,
                candidate_digest=digest(self.candidate),
                containment=containment,
            )
