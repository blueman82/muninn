"""Build frozen historical fixtures for ordinary upgrade acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from unittest import mock

from muninn import store
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from tests.host_renewal_inventory import IndexedSource, seed_indexed
from tests.ingest_support import TID
from tests.policy_preview_v1_support import populate
from tests.test_host_renewal_apply import ApplyFixture
from tools import prepare_codex_policy


class PrerequisiteFixture(ApplyFixture):
    """Build exact old storage and executable readers in temporary HOME."""

    def frozen_runtime_accepts_old(self) -> None:
        """Run the pinned old reader against the frozen source."""
        frozen = Path(__file__).parent / "fixtures/installer/marked-v1-runtime"
        manifest = json.loads((frozen / "manifest.json").read_text())
        self.assertEqual(
            manifest["commit"], "63e9a62ab4a546456ac59f6a2d324b842ac1027e"
        )
        runtime = self.home / "old-runtime"
        runtime.mkdir()
        for name, info in manifest["files"].items():
            raw = (frozen / (name + ".txt")).read_bytes()
            self.assertEqual(len(raw), info["bytes"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), info["sha256"])
            self.assertEqual(
                hashlib.sha1(
                    b"blob " + str(len(raw)).encode() + b"\0" + raw
                ).hexdigest(),
                info["blob"],
            )
            target = runtime / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        probe = runtime / "probe.py"
        probe.write_text(
            "import sys\nfrom pathlib import "
            "Path\nsys.path.insert(0,str(Path(__file__).parent))\nfrom "
            "muninn import store\nassert store.SCHEMA_VERSION == "
            "1\nc=store.connect_ro(Path(sys.argv[1]))\nassert "
            "c.execute('PRAGMA user_version').fetchone()[0] == "
            "1\nc.close()\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", str(probe), str(self.path)],
            env=dict(
                os.environ, HOME=str(self.home), MUNINN_HOME=str(self.ctx.data)
            ),
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def prepare_v1(self, mode: str) -> tuple[ReviewPolicy, IndexedSource]:
        """Build frozen storage with synthetic indexed originals."""
        with mock.patch(
            "muninn.review_codex_policy.host_identity",
            return_value=self.old_host,
        ):
            selected = prepare_codex_policy.prepare(
                self.ctx.data,
                self.executable,
                self.ctx.codex_home,
                "fixture-owner",
                1.0,
                revision=3,
                default_profile=(
                    self.repo / "tools/default_codex_policy.json"
                ).read_bytes(),
            )
        old = ReviewPolicy.model_validate_json(selected.read_bytes())
        raw = selected.read_text()
        source = seed_indexed(self)
        with closing(store.connect_ro(self.path)) as indexed:
            tables = {
                table: [
                    dict(row)
                    for row in indexed.execute("SELECT * FROM " + table)
                ]
                for table in ("scope", "scope_path", "source", "event")
            }
        self.path.unlink()
        fixture = (
            Path(__file__).parent / "fixtures/installer/marked-v1-state.sql"
        )
        with closing(sqlite3.connect(self.path, isolation_level=None)) as conn:
            conn.executescript(fixture.read_text())
            populate(conn, raw, digest(old), self.home)
            for table, rows in tables.items():
                for row in rows:
                    conn.execute(
                        "INSERT INTO "
                        + table
                        + " ("
                        + ",".join(row)
                        + ") VALUES("
                        + ",".join("?" for _ in row)
                        + ")",
                        tuple(row.values()),
                    )
            conn.execute(
                "INSERT INTO review_source VALUES('codex',?,0,3,3,0,?,1.0)",
                (TID, source.window().boundary_hash),
            )
            conn.execute(
                (
                    "UPDATE review_request SET "
                    "thread_id=?,session_root=?,from_line=2,through_line=3,boun"
                    "dary_hash=?,scope_json=?"
                ),
                (
                    TID,
                    TID,
                    source.window().boundary_hash,
                    canonical_json(source.scope),
                ),
            )
            conn.execute(
                (
                    "UPDATE review_control SET "
                    "generation=1,activated_at=1.0,admission=?,policy_json=?,po"
                    "licy_digest=?"
                ),
                (1, raw, digest(old)),
            )
            conn.execute(
                "INSERT INTO learning_setup VALUES(1,?,NULL,1.5,NULL)", (mode,)
            )
        self.path.chmod(0o600)
        return old, source
