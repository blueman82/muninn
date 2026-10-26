"""Private native store fixtures for strict installer preview tests."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

from install import review_policy
from install.context import Ctx
from install.record import Record
from muninn import store
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_progress import bootstrap_policy, digest
from tests.test_review_shared_models import policy_data


def no_command(
    argv: Sequence[str | Path],
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Refuse any unintended external effect from the fixture context."""
    raise AssertionError("preview must not launch an external command")


def states(
    root: Path,
) -> dict[str, tuple[tuple[int, ...], bytes | str | None]]:
    """Capture bytes and metadata, excluding read-only access times."""
    result: dict[str, tuple[tuple[int, ...], bytes | str | None]] = {}
    for path in (root, *root.rglob("*")):
        metadata = path.lstat()
        content = (
            str(path.readlink())
            if path.is_symlink()
            else path.read_bytes() if path.is_file() else None
        )
        result[str(path.relative_to(root))] = (
            (
                metadata.st_mode,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            ),
            content,
        )
    return result


class PreviewFixture(unittest.TestCase):
    """Create real policy state without native services or providers."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "home # preview"
        self.home.mkdir(mode=0o700)
        self.ctx = Ctx(
            home=self.home,
            run=no_command,
            ts="preview-fixture",
            upgrade=True,
            dry_run=True,
        )
        self.path = store.db_path(self.ctx.data)
        self.lock = self.ctx.data / "writer.lock"
        data = policy_data()
        data["projection_version"] = "permitted-fields-v2"
        self.policy = ReviewPolicy.model_validate(data)
        with (
            store.writer_lock(self.ctx.data),
            closing(store.connect_rw(self.path)) as conn,
        ):
            conn.execute("BEGIN IMMEDIATE")
            bootstrap_policy(conn, self.policy, 1.0)
            conn.execute("COMMIT")
        sha = "a" * 40
        self.rec = self.record_for(self.policy, sha)

    def record_for(self, policy: ReviewPolicy, sha: str = "a" * 40) -> Record:
        """Bind a selected policy through the same frozen-input metadata."""
        raw = canonical_json(policy)
        binding = {
            **review_policy.capture(self.ctx, sha),
            "raw": raw,
            "raw_digest": hashlib.sha256(raw.encode()).hexdigest(),
            "canonical": raw,
            "digest": digest(policy),
        }
        return {"sha": sha, "policy_input": binding}
