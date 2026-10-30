"""Temporary current-format installations for checked host-renewal tests."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shlex
import shutil
import sqlite3
import sys
import unittest
from contextlib import closing
from unittest import mock

from pydantic import TypeAdapter

from install import installer, policy_preview
from install.host_renewal_models import ContainmentPlan, HostPlan
from install.host_renewal_preview import StateScan, scan_state
from install.policy_record_models import Control
from install.record import Record, load_record
from muninn import review_codex_policy, review_pause_state, store
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_progress import bootstrap_policy, digest
from tests import host_renewal_inventory
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
        self.executable = self.home / "fixture-python"
        self.executable.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'
        )
        self.executable.chmod(0o700)
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
        number: int = 1,
        selected: ReviewPolicy | None = None,
    ) -> str:
        """Insert a canonical request independent of preview output."""
        policy = selected or (self.candidate if candidate else self.old)
        ident = "R_" + f"{number:x}" * 32
        self.sql(
            "INSERT INTO review_request(id,duplicate_key,provider,thread_id,"
            "session_root,from_line,through_line,boundary_hash,scope_json,"
            "policy_json,policy_digest,trigger,origin,state,created_at) "
            "VALUES(?,?,'codex',?,?,1,1,?,?,?,?,'owner','review',?,1.0)",
            (
                ident,
                hashlib.sha256(ident.encode()).hexdigest(),
                "fixture" if number == 1 else f"fixture-{number}",
                "fixture" if number == 1 else f"fixture-{number}",
                "a" * 64,
                '{"project_id":"fixture","task":null}',
                raw if raw is not None else canonical_json(policy),
                digest(policy),
                state,
            ),
        )
        return ident

    def attempt(self, request_id: str, *, number: int = 1) -> str:
        """Attach an unfinished attempt with its requested runner."""
        ident = "A_" + f"{number:x}" * 32
        self.sql(
            "INSERT INTO review_attempt(id,request_id,generation,"
            "requested_json,"
            "observed_json,started_at,progress_at) VALUES(?,?,1,?,?,2.0,2.0)",
            (
                ident,
                request_id,
                canonical_json(self.old.runner),
                '{"runner":null,"version":null,'
                '"model":null,"reasoning":null}',
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
        """Populate synthetic governed metadata through the shared fixture."""
        host_renewal_inventory.seed_inventory(self)

    def scan(self, containment: ContainmentPlan | None = None) -> StateScan:
        """Read a fresh guarded temporary snapshot for containment tests."""
        with policy_preview.connection(self.path) as conn:
            return scan_state(
                conn,
                self.path,
                candidate_digest=digest(self.candidate),
                containment=containment,
            )

    def read_control(self) -> Control:
        """Read control independently of the renewal projection."""
        with closing(store.connect_ro(self.path)) as conn:
            return TypeAdapter(Control).validate_python(
                dict(conn.execute("SELECT * FROM review_control").fetchone())
            )

    def request_snapshots(self) -> tuple[tuple[str, str, str], ...]:
        """Read immutable request policy bytes and identities directly."""
        with closing(store.connect_ro(self.path)) as conn:
            return tuple(
                tuple(row)
                for row in conn.execute(
                    "SELECT id,policy_json,policy_digest FROM review_request "
                    "ORDER BY id"
                )
            )

    def run_renewal(self, plan: HostPlan, *, approve: bool = True) -> Record:
        """Call the real entry with a fake answer and actual receipt."""
        ctx = dataclasses.replace(self.ctx, dry_run=False)
        args = [
            "--repo",
            str(self.repo),
            "--sha",
            self.sha,
            "--upgrade",
            "--review-policy",
            str(ctx.review_policy),
            "--renew-codex-host",
            plan.digest,
        ]
        with (
            mock.patch.object(installer, "Ctx", return_value=ctx),
            mock.patch("sys.stdin.isatty", return_value=True),
            mock.patch(
                "builtins.input", return_value="yes" if approve else "no"
            ),
        ):
            try:
                result = installer.main(args)
            except SystemExit as error:
                result = error.code
        self.assertEqual(result, 0, "actual renewal entry refused")
        return load_record(ctx.rdir / "rollback-record.json")
