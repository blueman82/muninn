"""Freeze explicit policy input and commit first activation under ownership."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing, suppress
from pathlib import Path

from install import (
    learning_scope,
    legacy_state,
    policy_preview,
    policy_records,
    record,
    snapshot_io,
)
from install import legacy_baseline as baseline
from install import review_policy_default as default_policy
from install.context import Ctx, StepFailedError
from install.policy_record_models import (
    Activation,
    InputBinding,
    Intent,
    PolicyInput,
)
from install.record import Record
from install.review_policy_parse import policy as _policy
from install.review_policy_ready import selection as _selection
from install.review_policy_ready import verify_ready as verify_ready
from install.review_policy_transition import apply as apply_transition
from install.review_policy_transition import compatible as compatible
from muninn import (
    file_sync,
    platform_io,
    store,
    store_fingerprint,
    tombstones,
)
from muninn import review_pause as q
from muninn.review_codex_policy import check_activation, check_host
from muninn.review_json import canonical_json
from muninn.review_models import ReviewPolicy
from muninn.review_progress import bootstrap_policy, digest

LIMIT = 1024 * 1024


def read_policy(path: Path) -> str:
    """Read bounded UTF8 from a stable ordinary file."""
    try:
        if path.lstat().st_size > LIMIT:
            raise ValueError
        before = baseline.file_state(path)
        with platform_io.open_regular(path) as handle:
            raw = handle.read(LIMIT + 1)
        if len(raw) > LIMIT or baseline.file_state(path) != before:
            raise ValueError
        return raw.decode("utf-8")
    except (OSError, ValueError):
        raise StepFailedError("review_policy_invalid") from None


def _home(path: Path) -> list[int]:
    """Bind an ordinary home directory without changing its permissions."""
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise StepFailedError("review_policy_home_changed")
    if os.name != "nt" and (
        info.st_uid != os.getuid() or info.st_mode & 0o022
    ):
        raise StepFailedError("review_policy_home_changed")
    return [info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)]


def capture(
    ctx: Ctx, sha: str, profile_raw: bytes | None = None
) -> PolicyInput:
    """Capture raw and canonical input before any installation disruption."""
    result: InputBinding = {
        "status": "activation_pending",
        "operation": ctx.ts,
        "home": str(ctx.home),
        "home_identity": _home(ctx.home),
        "sha": sha,
        "enable_learning": ctx.enable_learning,
    }
    selected: ReviewPolicy | None = None
    raw: str | None = None
    if ctx.review_policy is not None:
        if ctx.platform not in {"darwin", "linux"}:
            raise StepFailedError("review_policy_unsupported")
        raw = read_policy(ctx.review_policy)
        selected = _policy(raw)
        if selected.policy_source is not None:
            raise StepFailedError("review_policy_invalid")
        if selected.experimental_codex is not None:
            try:
                check_host(selected.experimental_codex, ctx.data)
            except ValueError:
                raise StepFailedError("review_policy_unsupported") from None
    else:
        default = default_policy.select(ctx, profile_raw)
        if default is not None:
            selected, source = default
            return policy_records.policy_input({**result, **source})
    if selected is not None:
        if raw is None:
            raw = canonical_json(selected)
        return policy_records.policy_input(
            {
                **result,
                "raw": raw,
                "raw_digest": hashlib.sha256(raw.encode()).hexdigest(),
                "canonical": canonical_json(selected),
                "digest": digest(selected),
            }
        )
    return policy_records.policy_input(result)


def frozen(
    ctx: Ctx, rec: Record, *, check_supplied: bool = False
) -> ReviewPolicy | None:
    """Authenticate the retained snapshot and any explicitly repeated input."""
    value = rec.get("policy_input")
    if "policy_input" not in rec:
        if check_supplied and ctx.review_policy is not None:
            raise StepFailedError("review_policy_snapshot_missing")
        return None
    value = policy_records.policy_input(value)
    if any(
        value.get(key) != expected
        for key, expected in (
            ("operation", ctx.ts),
            ("home", str(ctx.home)),
            ("home_identity", _home(ctx.home)),
            ("sha", rec["sha"]),
        )
    ):
        raise StepFailedError("review_policy_snapshot_changed")
    if type(value.get("enable_learning", False)) is not bool:
        raise StepFailedError("review_policy_snapshot_changed")
    raw = value.get("raw")
    if raw is None:
        if any(
            key in value
            for key in ("raw", "raw_digest", "canonical", "digest")
        ) or (check_supplied and ctx.review_policy is not None):
            raise StepFailedError("review_policy_snapshot_changed")
        return None
    if not isinstance(raw, str) or len(raw.encode()) > LIMIT:
        raise StepFailedError("review_policy_snapshot_changed")
    policy = _policy(raw)
    if (
        value.get("raw_digest") != hashlib.sha256(raw.encode()).hexdigest()
        or value.get("canonical") != canonical_json(policy)
        or value.get("digest") != digest(policy)
        or (
            check_supplied
            and ctx.review_policy is not None
            and read_policy(ctx.review_policy) != raw
        )
    ):
        raise StepFailedError("review_policy_snapshot_changed")
    return policy


def preflight(ctx: Ctx, rec: Record) -> None:
    """Check installed policy before stopping the current runtime."""
    policy = frozen(ctx, rec)
    if not ctx.upgrade or policy is None:
        return
    policy_preview.validate(ctx, policy)


def _activation(conn: sqlite3.Connection, path: Path) -> Intent:
    """Bind control and sorted source boundaries in the caller snapshot."""
    control = dict(conn.execute("SELECT * FROM review_control").fetchone())
    rows = [
        tuple(row)
        for row in conn.execute(
            "SELECT * FROM review_source ORDER BY provider,thread_id"
        )
    ]
    info = path.lstat()
    return policy_records.intent(
        {
            "store": [info.st_dev, info.st_ino],
            "control": control,
            "boundaries": hashlib.sha256(
                json.dumps(rows, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    )


def _check_live(ctx: Ctx, rec: Record, path: Path) -> None:
    """Refuse substituted live data and the legacy key before reopening."""
    info = path.lstat()
    if rec.get("policy_store_identity", [info.st_dev, info.st_ino]) != [
        info.st_dev,
        info.st_ino,
    ]:
        raise StepFailedError("review_activation_store_changed")
    if "legacy" in rec:
        key = store_fingerprint.capture(ctx.data).key
        actual = (
            json.loads(json.dumps(dataclasses.asdict(key)))
            if key is not None
            else None
        )
        wanted = rec["legacy"]["publication_binding"]["source"]["key"]
        if actual != json.loads(json.dumps(wanted)):
            raise StepFailedError("legacy_key_changed")


def activate(
    ctx: Ctx,
    rec: Record,
    owner: q.MaintenanceOwner,
    path: Path,
    operation_check: legacy_state.OperationCheck,
) -> None:
    """Replay latest erasure and activate only on installer normal exit."""
    policy_records.validate(rec)
    policy = frozen(ctx, rec)

    def authority() -> tuple[int, int]:
        identity = operation_check()
        if "legacy" not in rec and (
            "policy_ready" in rec
            or (rec.get("runtime_prepared") and policy is not None)
        ):
            verify_ready(ctx, rec)
        operation_check()
        return identity

    authority()
    with q.maintenance_writer(owner), snapshot_io.validated(path) as check:
        _check_live(ctx, rec, path)
        with closing(store.connect_rw(path)) as conn:
            check()
            q.verify_current(owner, conn)
            authority()
            tombstones.reapply_tombstones(conn, ctx.data)
            compatible(conn, policy)
            if (
                policy is not None
                and policy.policy_source == "repository_default"
                and conn.execute(
                    "SELECT policy_digest FROM review_control "
                    "WHERE singleton=1"
                ).fetchone()[0]
                != digest(policy)
            ):
                default_policy.prepare(ctx, rec, policy)
            apply_transition(
                conn,
                policy,
                enable_admission=policy_records.learning_consent(rec),
            )
            control = conn.execute("SELECT * FROM review_control").fetchone()
            previous = (
                policy_records.activation(rec["policy_activation"])
                if "policy_activation" in rec
                else None
            )
            if control["policy_json"] is not None:
                if previous and previous.get("intent"):
                    actual = _activation(conn, path)
                    wanted = previous["intent"]
                    admission = actual["control"]["admission"]
                    expected = wanted["control"].copy()
                    expected["admission"] = admission
                    if not admission:
                        if (
                            actual["control"]["generation"]
                            < expected["generation"]
                        ):
                            raise StepFailedError("review_activation_changed")
                        expected["generation"] = actual["control"][
                            "generation"
                        ]
                    if actual != wanted | {"control": expected}:
                        raise StepFailedError("review_activation_changed")
                    previous["phase"] = "complete"
                    rec["policy_activation"] = previous
                    rec["policy_status"] = (
                        "active" if admission else "activation_incomplete"
                    )
                else:
                    rec["policy_status"] = "configured"
            elif policy is None:
                rec["policy_status"] = "activation_pending"
            else:
                _bootstrap(ctx, rec, conn, path, policy, authority)
            learning_scope.apply(ctx, rec, conn)
            check()
        file_sync.sync_path(path)
        check()
        record.save(ctx, rec)


def _bootstrap(
    ctx: Ctx,
    rec: Record,
    conn: sqlite3.Connection,
    path: Path,
    policy: ReviewPolicy,
    operation_check: legacy_state.OperationCheck,
) -> None:
    """Durably record exact uncommitted activation before its single commit."""
    previous = (
        policy_records.activation(rec["policy_activation"])
        if "policy_activation" in rec
        else None
    )
    now = (
        previous["intent"]["control"]["activated_at"]
        if previous
        else float(ctx.now())
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        default_policy.prepare(ctx, rec, policy)
        if policy.experimental_codex is not None:
            check_activation(policy.experimental_codex, ctx.data)
        bootstrap_policy(conn, policy, now)
        intent = _activation(conn, path)
        if previous and previous["intent"] != intent:
            raise StepFailedError("review_activation_changed")
        activation: Activation = {"phase": "intent", "intent": intent}
        rec["policy_activation"] = activation
        record.save(ctx, rec)
        operation_check()
        conn.execute("COMMIT")
    except BaseException:
        with suppress(BaseException):
            conn.execute("ROLLBACK")
        raise
    activation["phase"] = "complete"
    rec["policy_status"] = "active"


def pending(ctx: Ctx) -> tuple[Ctx, Record] | None:
    """Find one incomplete first activation in private records."""
    candidates: list[tuple[Ctx, Record]] = []
    paths = sorted(ctx.rdir.parent.glob("muninn-install-*"))
    if len(paths) > 128:
        raise StepFailedError("review_activation_ambiguous")
    for path in paths:
        rec = legacy_state.read(path / "rollback-record.json")
        if "legacy" in rec or not rec.get("policy_activation"):
            continue
        bound = policy_records.envelope(rec)
        if bound.get("install_complete") or bound.get("superseded_by"):
            continue
        active = dataclasses.replace(
            ctx, ts=bound["ts"], fresh=bound["fresh"], upgrade=bound["upgrade"]
        )
        if (
            path != active.rdir
            or rec.get("rdir") != str(path)
            or rec.get("home") != str(ctx.home)
        ):
            raise StepFailedError("review_activation_record_changed")
        frozen(active, rec, check_supplied=True)
        candidates.append((active, rec))
    if len(candidates) > 1:
        raise StepFailedError("review_activation_ambiguous")
    return candidates[0] if candidates else None


def ready(ctx: Ctx, rec: Record) -> None:
    """Freeze compatible runtime and configuration before activation."""
    if (
        not rec.get("runtime_prepared")
        or "legacy" in rec
        or frozen(ctx, rec) is None
    ):
        return
    if baseline.tree_state(ctx.lib / rec["sha"], links=True) != rec.get(
        "prepared_runtime"
    ):
        raise StepFailedError("review_activation_runtime_changed")
    if "policy_ready" not in rec:
        rec["policy_ready"] = policy_records.ready(
            {
                "runtime": baseline.tree_state(
                    ctx.lib / rec["sha"], links=True
                ),
                "files": {
                    str(path): (
                        baseline.file_state(path)
                        if os.path.lexists(path)
                        else None
                    )
                    for path in (ctx.settings, ctx.config, ctx.cursor_settings)
                },
                "selection": _selection(ctx),
            },
            ctx,
        )
        record.save(ctx, rec)
    verify_ready(ctx, rec)
