"""Poller heartbeat: ``status.json`` and what it says about freshness.

The poller writes counts and timestamps only. Readers (the CLI, the hooks
and doctor) treat a missing or unreadable file as "no heartbeat" rather
than as an error.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from muninn import platform_io, query, store
from muninn.platform_paths import read_selection, windows_base
from muninn.query.index_age import ALIVE_AT, seconds_since
from muninn.review_models import ReviewPolicy
from muninn.review_policy_status import admission_check, policy_status
from muninn.review_policy_status import (
    configured_policy_check as _review_policy,
)
from muninn.review_recovery import health_checks as learning_checks

DATA_FILES = frozenset(
    {
        "muninn.sqlite",
        "writer.lock",
        "status.json",
        "calls.jsonl",
        "calls.jsonl.1",
        "poller.log",
        "poller.log.1",
        "tombstones.jsonl",
        "tombstone.key",
        "recall.off",
        "recall.advisory",
        "recall-review.sqlite",
        "recall-review.sqlite-journal",
        "stop.json",
        "review.supervisor.lock",
        "review.supervisor.json",
        "review.supervisor.repair.json",
        "review-capabilities",
        "review-codex-schema.json",
        "review-policy-codex-ongoing.json",
        "review-policy-codex-ongoing-v2.json",
        "review-policy-codex-ongoing-v3.json",
        "review-prompts",
        "review-runner-cwd",
    }
)


def read_status(home: Path) -> dict[str, object]:
    """Read ``status.json``.

    Args:
        home: Data directory.

    Returns:
        The parsed object, or ``{}`` when absent, unreadable or not an
        object.
    """
    try:
        path = home / "status.json"
        with platform_io.open_regular(path, root=home) as handle:
            platform_io.assert_private_fd(handle.fileno())
            raw = handle.read(16385)
        if len(raw) > 16384:
            return {}
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    # json.loads gives Any; JSON object keys are always strings.
    return cast("dict[str, object]", data) if isinstance(data, dict) else {}


_ERROR_CODE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def poller_error(status: Mapping[str, object]) -> str | None:
    """Return the poller's last error class name, if it is one.

    ``status.json`` is read back from disk and could be hand-edited, so only
    a bare identifier passes; anything else could smuggle text into doctor
    or a hook block.

    Args:
        status: Parsed ``status.json``.

    Returns:
        The exception class name, or None when absent or malformed.
    """
    err = status.get("last_error")
    if isinstance(err, str) and _ERROR_CODE.fullmatch(err):
        return err
    return None


def count_field(status: Mapping[str, object], key: str) -> int:
    """Read a count from ``status.json``, treating anything odd as zero.

    Args:
        status: Parsed ``status.json``.
        key: Field name.

    Returns:
        The non-negative int, else 0.
    """
    n = status.get(key)
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0


def write_status(home: Path, fields: Mapping[str, object]) -> None:
    """Merge fields into ``status.json`` atomically, mode 0600.

    Args:
        home: Data directory.
        fields: Values that replace the same keys in the current file.
    """
    store.write_json_atomic(
        home / "status.json", {**read_status(home), **fields}
    )


def freshness(status: Mapping[str, object]) -> dict[str, object]:
    """Report how old the index is and whether the poller is alive.

    Args:
        status: Parsed ``status.json``; an empty one counts as stale.

    Returns:
        ``index_age_s`` and ``poller`` (``ok`` or ``stale``).
    """
    return query.freshness(status or {})


def alive_age(status: Mapping[str, object]) -> int | None:
    """Return the age of the alive stamp of a pass that is still running.

    Args:
        status: Parsed status.json.

    Returns:
        Seconds since a running pass last stamped alive, or None between
        passes and when no usable stamp exists (not written yet, failed to
        write, or from the future).
    """
    alive = seconds_since(status.get(ALIVE_AT), future_ok=False)
    last = seconds_since(status.get("last_pass_at"))
    # A stamp older than the last finished pass was left by a poller that
    # died mid-pass, not by a pass that is running now.
    if alive is None or (last is not None and alive >= last):
        return None
    return alive


def install_sha(env: Mapping[str, str]) -> str | None:
    """Name the installed code version.

    Args:
        env: Environment; ``MUNINN_INSTALL_SHA`` wins when set.

    Returns:
        The variable, else the name of the pinned code directory that
        ``~/.local/lib/muninn/current`` points at, else None.
    """
    if env.get("MUNINN_INSTALL_SHA"):
        return env["MUNINN_INSTALL_SHA"]
    if sys.platform == "win32":
        try:
            selected = read_selection(windows_base(env))
        except (OSError, ValueError):
            return None
        return selected[0].name if selected else None
    home = Path(env.get("HOME") or Path.home())
    current = home / ".local/lib/muninn/current"
    return current.readlink().name if current.is_symlink() else None


def review_health(
    conn: sqlite3.Connection, now: float
) -> tuple[tuple[str, bool | None, str, str], ...]:
    """Derive finite review diagnostics without probing processes or repairing.

    Args:
        conn: Current normalized read snapshot.
        now: Observation time in UTC seconds.

    Returns:
        Check arguments with safe codes and counts, never stored explanations.
    """
    rows = conn.execute(
        "SELECT a.started_at,a.ended_at,a.generation,r.policy_json,"
        "r.state,EXISTS(SELECT 1 FROM review_call c WHERE c.attempt_id=a.id "
        "AND c.ended_at IS NULL),a.error_code,a.outcome "
        "FROM review_attempt a JOIN review_request r ON r.id=a.request_id"
    ).fetchall()
    control = conn.execute(
        "SELECT generation FROM review_control WHERE singleton=1"
    ).fetchone()
    generation = None if control is None else control[0]
    counts = dict.fromkeys(
        (
            "process_deadline",
            "commit_delay",
            "cancel_delay",
            "abandoned",
            "auth",
            "config",
        ),
        0,
    )
    for (
        started,
        ended,
        epoch,
        raw,
        state,
        process_pending,
        error,
        outcome,
    ) in rows:
        try:
            policy = ReviewPolicy.model_validate_json(raw)
        except ValueError:
            counts["config"] += 1
            continue
        overdue = ended is None and now > started + policy.deadline_seconds
        counts["process_deadline"] += error == "deadline" or (
            overdue and bool(process_pending)
        )
        counts["commit_delay"] += (
            overdue and not process_pending and state == "running"
        )
        counts["cancel_delay"] += overdue and state == "cancelled"
        counts["abandoned"] += outcome == "abandoned" or (
            ended is None and generation is not None and epoch != generation
        )
        if error in ("auth", "config"):
            counts[error] += 1
    output: list[tuple[str, bool | None, str, str]] = [
        (
            "review_" + key,
            None if key == "abandoned" and control is None else not value,
            (
                "generation_unavailable"
                if key == "abandoned" and control is None
                else str(value)
            ),
            "warn",
        )
        for key, value in counts.items()
    ]
    output.append(
        (
            "review_backlog",
            True,
            str(
                conn.execute(
                    "SELECT count(*) FROM review_request "
                    "WHERE state IN ('pending','waiting')"
                ).fetchone()[0]
            ),
            "info",
        )
    )
    output.extend(_review_gap_checks(conn, control is not None))
    output.append(_review_policy(conn))
    output.append(admission_check(policy_status(conn)))
    output.extend(learning_checks(conn))
    return tuple(output)


def review_protocol(
    home: Path,
) -> tuple[tuple[str, bool | None, str, str], ...]:
    """Observe protocol-file presence without granting containment authority.

    Args:
        home: Data directory, which is never initialized by this observer.

    Returns:
        Finite observations only; no witness, boot or journal content.
    """
    witness = _protocol_present(home / "review.supervisor.json")
    repair = _protocol_present(home / "review.supervisor.repair.json")
    intent = _protocol_present(home / "tombstones.jsonl")
    results: tuple[tuple[str, bool | None, str, str], ...] = (
        (
            "review_pause",
            None,
            (
                "witness_present_containment_unverified"
                if witness
                else "witness_absent_containment_unverified"
            ),
            "info",
        ),
        (
            "review_repair",
            not repair,
            (
                "baseline_present_recovery_unverified"
                if repair
                else "baseline_absent"
            ),
            "warn",
        ),
        (
            "review_erasure_intent",
            None if intent else True,
            (
                "journal_present_consistency_unverified"
                if intent
                else "journal_absent"
            ),
            "info",
        ),
    )

    if sys.platform == "win32":
        results += (
            (
                "review_windows_repair",
                False,
                "boot_repair_and_baseline_retirement_unqualified",
                "warn",
            ),
        )
    return results


REVIEW_RECORDS = (
    " FROM (SELECT s.provider,s.thread_id,s.id AS source_id,e.line,"
    "CASE WHEN count(DISTINCT e.line_sha256)=1 THEN min(e.line_sha256) "
    "END AS line_sha256 FROM event e JOIN source s ON s.id=e.source_id "
    "WHERE e.kind IN ('prompt','reply') GROUP BY s.id,e.line) e "
)
ELIGIBLE = (
    "(EXISTS (SELECT 1 FROM review_source p WHERE p.provider=e.provider "
    "AND p.thread_id=e.thread_id AND e.line>p.activation_line "
    "AND e.line<=p.eligible_line) OR EXISTS (SELECT 1 FROM review_request r "
    "WHERE r.provider=e.provider AND r.thread_id=e.thread_id "
    "AND e.line BETWEEN r.from_line AND r.through_line))"
)
_CURRENT_INPUT = (
    "EXISTS (SELECT 1 FROM source s WHERE s.id=e.source_id "
    "AND s.status='active' AND s.replay_mode!='unverified' "
    "AND s.session_root=r.session_root AND (EXISTS (SELECT 1 FROM event b "
    "WHERE b.source_id=s.id AND b.line=r.through_line "
    "AND b.line_sha256=r.boundary_hash) OR (s.cursor_line=r.through_line "
    "AND s.anchor_sha256=r.boundary_hash)) "
    "AND NOT EXISTS (SELECT 1 FROM event b WHERE b.source_id=s.id "
    "AND b.line=r.through_line AND b.line_sha256!=r.boundary_hash)) "
    "AND ((e.line=r.through_line AND e.line_sha256=r.boundary_hash) "
    "OR EXISTS (SELECT 1 FROM review_input i JOIN review_evidence v "
    "ON v.id=i.evidence_id WHERE i.request_id=r.id AND v.state='live' "
    "AND v.provider=e.provider AND v.thread_id=e.thread_id "
    "AND v.line=e.line AND v.line_sha256=e.line_sha256 "
    "AND v.projection_version="
    "json_extract(r.policy_json,'$.projection_version')))"
)
CURRENT_ADMISSION = (
    "EXISTS (SELECT 1 FROM review_request r WHERE r.provider=e.provider "
    "AND r.thread_id=e.thread_id AND e.line BETWEEN r.from_line "
    "AND r.through_line AND " + _CURRENT_INPUT + ")"
)
RECEIPTED = (
    "EXISTS (SELECT 1 FROM review_request r JOIN review_receipt c "
    "ON c.request_id=r.id WHERE r.provider=e.provider "
    "AND r.thread_id=e.thread_id AND e.line BETWEEN r.from_line "
    "AND r.through_line AND " + _CURRENT_INPUT + ")"
)


def writer_secure_delete() -> bool:
    """Whether writers turn ``secure_delete`` on.

    It is a per-connection setting, not stored in the file, so the writer
    pragmas are applied to an in-memory connection. Opening the real store
    would roll back a hot journal that doctor is meant to report.

    Returns:
        True when ``secure_delete`` reads back as on.
    """
    conn = sqlite3.connect(":memory:")
    try:
        for pragma in store.WRITER_PRAGMAS:
            conn.execute(pragma)
        return conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    finally:
        conn.close()


def _protocol_present(path: Path) -> bool:
    """Keep unsafe or uninspectable protocol paths visible without opening."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _review_gap_checks(
    conn: sqlite3.Connection,
    control_present: bool,
) -> list[tuple[str, bool | None, str, str]]:
    """Count indexed capture and completion gaps independently of logs."""
    output: list[tuple[str, bool | None, str, str]] = []
    for check, sql in (
        (
            "coverage_gaps",
            "SELECT count(*)"
            + REVIEW_RECORDS
            + "WHERE "
            + ELIGIBLE
            + " AND NOT "
            + RECEIPTED,
        ),
        (
            "capture_gaps",
            "SELECT count(*)"
            + REVIEW_RECORDS
            + "WHERE ("
            + ELIGIBLE
            + " AND NOT "
            + CURRENT_ADMISSION
            + ") OR EXISTS ("
            "SELECT 1 FROM source s LEFT JOIN review_source p "
            "ON p.provider=s.provider AND p.thread_id=s.thread_id "
            "JOIN review_control c WHERE s.id=e.source_id "
            "AND c.activated_at IS NOT NULL AND s.status='active' "
            "AND s.thread_class='primary' "
            "AND e.line>coalesce(p.eligible_line, "
            "CASE WHEN s.first_seen>c.activated_at THEN 0 "
            "ELSE s.cursor_line END))",
        ),
    ):
        if check == "capture_gaps" and not control_present:
            output.append(
                ("review_capture_gaps", None, "control_absent", "warn")
            )
            continue
        count = conn.execute(sql).fetchone()[0]
        output.append(("review_" + check, not count, str(count), "warn"))
    return output
