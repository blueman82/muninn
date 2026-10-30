"""Capture complete installer inputs and the checked lock projection."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from pydantic import TypeAdapter

from install import legacy_baseline as baseline
from install import policy_preview, policy_records, preflight
from install.constants import GIT_ENV, INSTALL_RECORD
from install.context import Ctx, StepFailedError, must
from install.host_renewal_binding_models import BindingInputs, Configuration
from install.host_renewal_files import lock_identity
from install.host_renewal_models import (
    HostPlan,
    PendingFacts,
    SetupNotice,
)
from install.host_renewal_operation import HeldOperation, validate_operation
from install.host_renewal_scan import metadata_frame
from install.policy_record_models import FileState
from install.preflight import check_repo as checked_repo
from install.provider_apps import installed
from install.provider_paths import cursor_commands
from install.record import Record
from install.review_policy_ready import selection
from install.runtime import runtime_python
from install.transforms import (
    drop_trust,
    edit_cursor_settings,
    edit_settings,
    enable,
    repoint,
    write_trust,
)
from install.trust import codex_hooks
from muninn import review_codex_launch, review_recovery, review_runner, store
from muninn import review_pause as q
from muninn.review_codex_policy import check_host
from muninn.review_models import ReviewPolicy
from muninn.review_pause_state import WITNESS
from muninn.tombstone_key import KEY_FILE, TOMBSTONE_FILE


def configuration(ctx: Ctx, files: dict[str, bytes]) -> dict[str, object]:
    """Bind initial identities and exact allowed pure upgrade transforms."""
    result: dict[str, object] = {"cursor_hooks": ctx.cursor_hooks}
    for name, path in (
        ("claude", ctx.settings),
        ("codex", ctx.config),
        ("cursor", ctx.cursor_settings),
    ):
        present = os.path.lexists(path)
        if not present and not (name == "cursor" and ctx.cursor_hooks):
            result[name] = None
            continue
        before = baseline.file_state(path) if present else None
        raw = (
            baseline.read_bytes(path)
            if present
            else preflight.EMPTY_CURSOR_CONFIG
        )
        expected = [hashlib.sha256(raw).hexdigest()]
        if name == "cursor" and ctx.cursor_hooks:
            fragment = json.loads(files[preflight.CURSOR_HOOKS_FILE])["hooks"]
            expected = [
                hashlib.sha256(
                    edit_cursor_settings(raw, cursor_commands(ctx), fragment)
                ).hexdigest()
            ]
        elif installed(ctx, name):
            if name == "claude":
                fragment = json.loads(
                    files["integrations/claude/settings-hooks.json"]
                )["hooks"]
                expected = [
                    hashlib.sha256(edit_settings(raw, fragment)).hexdigest()
                ]
            elif name == "codex":
                text = enable(
                    repoint(
                        drop_trust(raw.decode()),
                        str(ctx.lib / "current/integrations/codex"),
                    )
                )
                trusted = write_trust(
                    text, codex_hooks(files[preflight.CODEX_HOOKS_FILE])
                )
                expected = sorted(
                    {
                        hashlib.sha256(value.encode()).hexdigest()
                        for value in (text, trusted)
                    }
                )
        after = baseline.file_state(path) if os.path.lexists(path) else None
        if after != before:
            raise StepFailedError("host_renewal_state_changed")
        result[name] = {
            "path": str(path),
            "before": before,
            "expected": expected,
        }
    return result


def installation(
    ctx: Ctx, repo: Path, sha: str, operation: HeldOperation | None = None
) -> dict[str, object]:
    """Bind runtime, target source and managed upgrade inputs."""
    if (
        repo.resolve(strict=True) != repo
        or ctx.home.resolve(strict=True) != ctx.home
    ):
        raise StepFailedError("host_renewal_state_changed")
    files, _ = checked_repo(ctx, repo, sha)
    current = (ctx.lib / "current").resolve(strict=True)
    python = (ctx.lib / "python").resolve(strict=True)
    lock = ctx.lib / ".install-record.lock"
    result: dict[str, object] = {
        "home": baseline.directory(ctx.home),
        "repo": baseline.directory(repo),
        "library": baseline.directory(ctx.lib, private=True),
        "sha": sha,
        "runtime": baseline.tree_state(current, links=True),
        "selection": selection(ctx),
        "python": baseline.file_state(python),
        "service": baseline.file_state(ctx.plist),
        "configuration": configuration(ctx, files),
        "target_contract": hashlib.sha256(
            must(
                ctx,
                [
                    "git",
                    "-C",
                    repo,
                    "show",
                    f"{sha}:muninn/review_codex_output.py",
                ],
                env=GIT_ENV,
            ).stdout
        ).hexdigest(),
        "target_files": {
            name: hashlib.sha256(raw).hexdigest()
            for name, raw in files.items()
        },
        "expected_selection": {
            "current": sha,
            "launcher": str(ctx.lib / "current/bin/muninn"),
            "python": str(python),
        },
        "operation_lock": (
            baseline.file_state(lock, private=True)
            if os.path.lexists(lock)
            else None
        ),
    }

    if operation is not None:
        validate_operation(ctx, operation)
        if result["operation_lock"] != operation.held:
            raise StepFailedError("host_renewal_state_changed")
        result["operation_lock"] = operation.approved
    return result


def check_transformed(
    ctx: Ctx, expected: BindingInputs, current: BindingInputs
) -> None:
    """Allow only the originally captured normal runtime/config effects."""
    before, after = expected.installation, current.installation
    for key in (
        "home",
        "repo",
        "library",
        "sha",
        "service",
        "target_contract",
        "target_files",
        "operation_lock",
    ):
        if before[key] != after[key]:
            raise StepFailedError("host_renewal_plan_changed")
    if (
        expected.artifacts != current.artifacts
        or expected.protected_files[:2] != current.protected_files[:2]
    ):
        raise StepFailedError("host_renewal_plan_changed")
    if expected.protected_files[3] != current.protected_files[3]:
        raise StepFailedError("host_renewal_state_changed")
    check_configuration(ctx, before["configuration"], after["configuration"])
    if (
        baseline.file_state(Path(before["expected_selection"]["python"]))
        != before["python"]
    ):
        raise StepFailedError("host_renewal_plan_changed")
    links = after["selection"]["links"]
    if links[str(ctx.lib / "python")][0] != str(
        runtime_python(ctx.lib / before["sha"], ctx.platform)
    ):
        raise StepFailedError("host_renewal_plan_changed")
    for path, target in (
        (ctx.lib / "current", before["sha"]),
        (ctx.muninn, before["expected_selection"]["launcher"]),
    ):
        if links[str(path)][0] != target:
            raise StepFailedError("host_renewal_plan_changed")


def capture_setup_notice(ctx: Ctx, plan: HostPlan) -> SetupNotice | None:
    """Verify one bounded setup row against the complete approved scan."""
    with policy_preview.connection(store.db_path(ctx.data)) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='review_issue'"
        ).fetchone()
        row = (
            conn.execute(
                "SELECT * FROM review_issue WHERE id='setup'"
            ).fetchone()
            if exists
            else None
        )
        found = (
            hashlib.sha256(metadata_frame(dict(row))).hexdigest()
            if row
            else None
        )
        if found != plan.containment.setup_before_digest:
            raise StepFailedError("host_renewal_setup_changed")
        if row is None or row["code"] not in {
            "codex_host_changed",
            "codex_experimental_boundary",
        }:
            return None
        assert found is not None
        return SetupNotice(code=row["code"], row_digest=found)


def clear_host_notice(
    ctx: Ctx, owner: q.MaintenanceOwner, rec: Record
) -> None:
    """Retire only the exact stale approved notice after a durable grant."""
    receipt = policy_records.host_renewal(rec["host_renewal"])
    if not isinstance(receipt, PendingFacts) or receipt.phase not in {
        "grant_committed",
        "restart_pending",
    }:
        raise StepFailedError("host_renewal_recovery_required")
    notice = receipt.setup_notice
    if notice is None:
        return
    candidate = ReviewPolicy.model_validate_json(receipt.plan.candidate["raw"])
    terms = candidate.experimental_codex
    if terms is None:
        raise StepFailedError("host_renewal_delta")
    check_host(terms, ctx.data)
    selected = (
        ctx.data
        / "review-capabilities/selected"
        / (receipt.plan.candidate["digest"] + ".json")
    )
    before = baseline.file_state(selected, private=True)
    capability = review_runner.RunnerCapability.model_validate_json(
        review_runner.read_private_file(selected, 1024 * 1024)
    )
    if capability.status != "unverified":
        return
    review_codex_launch.checked_binding(ctx.data, capability)
    operation = ctx.host_operation
    if operation is None:
        raise StepFailedError("host_renewal_approval_required")
    with (
        q.maintenance_writer(owner),
        closing(store.connect_rw(store.db_path(ctx.data))) as conn,
    ):
        q.verify_current(owner, conn)
        validate_operation(ctx, operation)
        check_host(terms, ctx.data)
        if baseline.file_state(selected, private=True) != before:
            raise StepFailedError("host_renewal_state_changed")
        control = dict(conn.execute("SELECT * FROM review_control").fetchone())
        if control != receipt.successor:
            return
        row = conn.execute(
            "SELECT * FROM review_issue WHERE id='setup'"
        ).fetchone()
        if (
            row is None
            or row["code"] != notice.code
            or hashlib.sha256(metadata_frame(dict(row))).hexdigest()
            != notice.row_digest
        ):
            return
        if (
            conn.execute(
                "SELECT 1 FROM review_request WHERE policy_digest=? LIMIT 1",
                (receipt.plan.candidate["digest"],),
            ).fetchone()
            is not None
        ):
            return
        review_recovery.clear(conn, "setup")


def verify_captured(ctx: Ctx, plan: HostPlan, captured: BindingInputs) -> None:
    """Recheck exact file observations without signatures or subprocesses."""
    installation = captured.installation
    for path, expected, private in (
        (ctx.home, installation["home"], False),
        (Path(plan.repo), installation["repo"], False),
        (ctx.lib, installation["library"], True),
        (
            captured.artifacts["binding"].cli_cwd,
            captured.artifacts["cwd"],
            True,
        ),
        (
            captured.artifacts["binding"].auth_home,
            captured.artifacts["auth"],
            False,
        ),
    ):
        if baseline.directory(path, private=private) != expected:
            raise StepFailedError("host_renewal_state_changed")
    artifact = captured.artifacts
    for name, expected in artifact["artifacts"].items():
        if baseline.file_state(Path(name), private=True) != expected:
            raise StepFailedError("host_renewal_state_changed")
    if (
        tuple(artifact["binding"].cli_cwd.iterdir())
        or baseline.file_state(Path(artifact["executable"]["target"]))
        != artifact["executable"]["file"]
        or Path(artifact["binding"].requested.executable).resolve(strict=True)
        != Path(artifact["executable"]["target"])
        or baseline.file_state(ctx.plist) != installation["service"]
        or selection(ctx) != installation["selection"]
        or baseline.file_state((ctx.lib / "python").resolve(strict=True))
        != installation["python"]
    ):
        raise StepFailedError("host_renewal_state_changed")
    terms = ReviewPolicy.model_validate_json(
        plan.candidate["raw"]
    ).experimental_codex
    if terms is None:
        raise StepFailedError("host_renewal_delta")
    check_host(terms, ctx.data)
    if (
        ctx.review_policy is None
        or baseline.read_bytes(ctx.review_policy).decode()
        != plan.candidate["raw"]
    ):
        raise StepFailedError("host_renewal_plan_changed")
    protected = tuple(
        (
            baseline.file_state(ctx.data / name, private=True)
            if os.path.lexists(ctx.data / name)
            else None
        )
        for name in (KEY_FILE, TOMBSTONE_FILE, WITNESS)
    )
    if (
        protected != captured.protected_files[:3]
        or list(lock_identity(ctx.data, lambda: None))
        != captured.protected_files[3]
    ):
        raise StepFailedError("host_renewal_state_changed")


def check_configuration(
    ctx: Ctx, wanted: Configuration, current: Configuration
) -> None:
    """Check each provider against saved approved transformation hashes."""
    if wanted["cursor_hooks"] != ctx.cursor_hooks:
        raise StepFailedError("host_renewal_plan_changed")
    for name in ("claude", "codex", "cursor"):
        config = wanted[name]
        actual = current[name]
        if config is None:
            if actual is not None:
                raise StepFailedError("host_renewal_plan_changed")
        elif (
            actual is None
            or actual["path"] != config["path"]
            or actual["before"] is None
            or actual["before"]["sha256"] not in config["expected"]
        ):
            raise StepFailedError("host_renewal_plan_changed")


def capture_prior_summary(ctx: Ctx) -> FileState | None:
    """Bind the private pre-disruption summary without retaining its body."""
    path = ctx.lib / INSTALL_RECORD
    return (
        TypeAdapter(FileState).validate_python(
            baseline.file_state(path, private=True)
        )
        if os.path.lexists(path)
        else None
    )
