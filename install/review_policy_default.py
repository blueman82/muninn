"""Select and prepare the repository default learning policy."""

from __future__ import annotations

import hashlib
import platform
import shutil
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import cast

from install import review_policy_transition, snapshot_io
from install.context import Ctx, StepFailedError
from install.record import Record
from muninn import review_codex_launch, store
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from tools import prepare_codex_policy


def select(
    ctx: Ctx, profile_raw: bytes | None
) -> tuple[ReviewPolicy, Record] | None:
    """Freeze a default policy only for a supported, unconfigured install."""
    if (
        profile_raw is None
        or not ctx.default_home
        or ctx.platform != "darwin"
        or platform.system() != "Darwin"
        or platform.machine() != "arm64"
        or _configured(ctx)
    ):
        return None
    found = shutil.which("codex")
    if found is None or not ctx.codex_home.is_dir():
        return None
    stable_executable = Path(found)
    auth_home = ctx.codex_home
    if auth_home.is_symlink() or auth_home.resolve() != auth_home:
        return None
    try:
        executable_target, executable_digest = (
            review_codex_launch.verify_codex_executable(stable_executable)
        )
        selected, _ = prepare_codex_policy.default_policy(
            ctx.data, stable_executable, profile_raw
        )
        auth_identity = _directory_identity(auth_home)
        profile_digest = prepare_codex_policy.profile_digest(profile_raw)
    except (OSError, ValueError):
        return None
    raw = canonical_json(selected)
    frozen: Record = {
        "policy_origin": "repository_default",
        "raw": raw,
        "raw_digest": hashlib.sha256(raw.encode()).hexdigest(),
        "canonical": raw,
        "digest": hashlib.sha256(raw.encode()).hexdigest(),
        "default_executable": str(stable_executable),
        "default_executable_target": str(executable_target),
        "default_executable_digest": executable_digest,
        "default_auth_home": str(auth_home),
        "default_auth_identity": auth_identity,
        "default_profile": profile_raw.decode("utf-8"),
        "default_profile_digest": profile_digest,
    }
    return selected, frozen


def prepare(ctx: Ctx, rec: Record, policy: ReviewPolicy) -> None:
    """Materialize capability files from the frozen default selection."""
    raw_source = rec.get("policy_input")
    if not isinstance(raw_source, dict):
        return
    source = cast(dict[str, object], raw_source)
    if source.get("policy_origin") != "repository_default":
        return
    profile_value = source.get("default_profile")
    if not isinstance(profile_value, str):
        raise StepFailedError("review_policy_default_changed")
    profile_raw = profile_value.encode("utf-8")
    if source.get("default_profile_digest") != (
        prepare_codex_policy.profile_digest(profile_raw)
    ):
        raise StepFailedError("review_policy_default_changed")
    executable = Path(policy.runner.executable)
    stable_executable = Path(cast(str, source.get("default_executable")))
    executable_target = Path(
        cast(str, source.get("default_executable_target"))
    )
    auth_home = Path(cast(str, source.get("default_auth_home")))
    try:
        current, digest = review_codex_launch.verify_codex_executable(
            executable
        )
        if (
            executable != stable_executable
            or current != executable_target
            or digest != source.get("default_executable_digest")
            or _directory_identity(auth_home)
            != source.get("default_auth_identity")
        ):
            raise ValueError("review_policy_default_changed")
        budget = policy.budget
        path = prepare_codex_policy.prepare(
            ctx.data,
            executable,
            auth_home,
            budget.approved_by,
            budget.approved_at,
            revision=(
                2
                if policy.policy_id == "muninn-codex-ongoing-production-v2"
                else 1
            ),
            default_profile=profile_raw,
        )
        if path.read_text(encoding="utf-8") != source.get("raw"):
            raise ValueError("review_policy_default_changed")
    except (OSError, ValueError):
        raise StepFailedError("review_policy_default_changed") from None


def _directory_identity(path: Path) -> list[int]:
    """Bind a normal local auth directory without reading credentials."""
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ValueError("default_auth_home_changed")
    return [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]


def _configured(ctx: Ctx) -> bool:
    """Select only unconfigured stores or unused enabled project defaults."""
    if not ctx.upgrade:
        return False
    path = store.db_path(ctx.data)
    if not path.is_file():
        return False
    try:
        with (
            store.writer_lock(ctx.data, wait_s=0),
            snapshot_io.validated(path) as check,
            closing(
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            ) as conn,
        ):
            check()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 3:
                return False
            store.assert_store_format(conn)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT admission,policy_json FROM review_control "
                "WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise StepFailedError("review_policy_state_invalid")
            review_policy_transition.compatible(conn, None)
            check()
            if row["policy_json"] is None:
                return False
            installed = ReviewPolicy.model_validate_json(row["policy_json"])
            return not (
                installed.policy_source == "repository_default"
                and row["admission"] == 1
                and not any(
                    conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                    for table in ("review_request", "knowledge")
                )
            )
    except (OSError, sqlite3.Error, store.StoreUnavailableError):
        raise StepFailedError("review_policy_state_invalid") from None
