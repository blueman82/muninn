"""Select and prepare the repository default learning policy."""

from __future__ import annotations

import hashlib
import platform
import shutil
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from install import (
    learning_policy_recovery,
    policy_records,
    review_policy_transition,
    snapshot_io,
)
from install.context import Ctx, StepFailedError
from install.policy_record_models import DefaultSelection
from install.record import Record
from muninn import review_codex_launch, store
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from tools import prepare_codex_policy


def select(
    ctx: Ctx, profile_raw: bytes | None
) -> tuple[ReviewPolicy, DefaultSelection] | None:
    """Freeze a default policy only for a supported, unconfigured install."""
    if (
        profile_raw is None
        or not ctx.default_home
        or ctx.platform != "darwin"
        or platform.system() != "Darwin"
        or platform.machine() != "arm64"
    ):
        return None
    configured, installed = _configured(ctx)
    if configured:
        return None
    found = (
        installed.runner.executable
        if installed is not None
        else shutil.which("codex")
    )
    if found is None:
        return None
    stable_executable = Path(found)
    auth_binding = _auth_binding(ctx, installed)
    if auth_binding is None:
        return None
    auth_home, auth_identity = auth_binding
    try:
        prepare_codex_policy.default_policy(
            ctx.data, stable_executable, profile_raw
        )
        profile_raw = learning_policy_recovery.profile(
            profile_raw, installed, ctx.enable_learning, float(ctx.now())
        )
        selected, _ = prepare_codex_policy.default_policy(
            ctx.data, stable_executable, profile_raw
        )
    except ValueError:
        raise StepFailedError("default_review_policy_invalid") from None
    try:
        executable_target, executable_digest = (
            review_codex_launch.verify_codex_executable(stable_executable)
        )
        profile_digest = prepare_codex_policy.profile_digest(profile_raw)
    except (OSError, ValueError):
        return None
    raw = canonical_json(selected)
    frozen: DefaultSelection = {
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
    return selected, policy_records.default_selection(frozen)


def _auth_binding(
    ctx: Ctx, installed: ReviewPolicy | None
) -> tuple[Path, list[int]] | None:
    """Keep a supported upgrade on the installed authentication directory.

    Args:
        ctx: Current installation paths.
        installed: Existing eligible default policy, if this is an upgrade.

    Returns:
        The auth directory and its identity, or None when it is unavailable.
    """
    try:
        auth_home = (
            review_codex_launch.selected_auth_home(ctx.data, installed)
            if installed is not None
            else ctx.codex_home
        )
        if (
            not auth_home.is_dir()
            or auth_home.is_symlink()
            or auth_home.resolve() != auth_home
        ):
            return None
        return auth_home, _directory_identity(auth_home)
    except (OSError, ValueError):
        if ctx.enable_learning:
            auth_home = ctx.codex_home
            if (
                auth_home.is_dir()
                and not auth_home.is_symlink()
                and auth_home.resolve() == auth_home
            ):
                return auth_home, _directory_identity(auth_home)
        return None


def prepare(ctx: Ctx, rec: Record, policy: ReviewPolicy) -> None:
    """Materialize capability files from the frozen default selection."""
    raw_source = rec.get("policy_input")
    if raw_source is None:
        return
    try:
        checked = policy_records.policy_input(raw_source)
    except StepFailedError:
        raise StepFailedError("review_policy_default_changed") from None
    if checked.get("policy_origin") != "repository_default":
        return
    source = policy_records.default_source(checked)
    profile_raw = source["default_profile"].encode("utf-8")
    if source.get("default_profile_digest") != (
        prepare_codex_policy.profile_digest(profile_raw)
    ):
        raise StepFailedError("review_policy_default_changed")
    executable = Path(policy.runner.executable)
    stable_executable = Path(source["default_executable"])
    executable_target = Path(source["default_executable_target"])
    auth_home = Path(source["default_auth_home"])
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
            revision={
                "muninn-codex-ongoing-production": 1,
                "muninn-codex-ongoing-production-v2": 2,
                "muninn-codex-ongoing-production-v3": 3,
            }[policy.policy_id],
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


def _configured(ctx: Ctx) -> tuple[bool, ReviewPolicy | None]:
    """Read upgrade state and retain an eligible default runner binding."""
    if not ctx.upgrade:
        return False, None
    path = store.db_path(ctx.data)
    if not path.is_file():
        return False, None
    try:
        with (
            snapshot_io.validated(path) as check,
            closing(
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            ) as conn,
        ):
            check()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 3:
                return False, None
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
                return False, None
            installed = ReviewPolicy.model_validate_json(row["policy_json"])
            if ctx.enable_learning:
                learning_policy_recovery.require_finished(conn)
            permitted = row["admission"] == 1 or ctx.enable_learning
            retained_default = installed.policy_source == "repository_default"
            legacy_choice = (
                ctx.learning_mode is not None or ctx.enable_learning
            ) and learning_policy_recovery.bundled(installed)
            eligible = (
                permitted
                and (retained_default or legacy_choice)
                and (
                    installed.policy_id != "muninn-codex-ongoing-production-v3"
                    or ctx.enable_learning
                    or not any(
                        conn.execute(
                            f"SELECT 1 FROM {table} LIMIT 1"
                        ).fetchone()
                        for table in ("review_request", "knowledge")
                    )
                )
            )
            return not eligible, installed if eligible else None
    except (OSError, sqlite3.Error, store.StoreUnavailableError):
        raise StepFailedError("review_policy_state_invalid") from None
