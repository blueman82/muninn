"""Prepare a nonwriting exact legacy-to-managed interface proposal."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from install import policy_preview
from install.codex_interface_legacy import legacy_facts
from install.codex_interface_models import InterfacePlan
from install.context import Ctx, StepFailedError
from install.host_renewal_preview import scan_state
from install.policy_source_format import validate_policy_source
from muninn import (
    review_codex_launch,
    review_codex_policy,
    review_managed_files,
    review_managed_host,
    review_managed_probe,
)
from muninn.review_codex_policy import CodexExperiment, ManagedCodexExperiment
from muninn.review_json import (
    canonical_json,
)
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from muninn.review_runtime_bridge import capture, migration_delta
from muninn.review_runtime_models import (
    MANAGED_HOST_RUNTIME,
    HostEnrollment,
    ManagedCodexSelection,
    ManagedHostIdentity,
)
from muninn.store import db_path


def legacy_binding(
    ctx: Ctx, policy: ReviewPolicy
) -> review_codex_launch.ExperimentalBinding:
    """Retain the installer-only integrity reader projection."""
    return legacy_facts(ctx, policy)[0]


def preview(ctx: Ctx) -> InterfacePlan:
    """Disclose one complete candidate without publishing or activating it."""
    if (
        not ctx.upgrade
        or ctx.fresh
        or ctx.enable_learning
        or ctx.learning_mode is not None
        or ctx.renew_codex_host is not None
        or ctx.review_policy is not None
        or ctx.platform != "darwin"
    ):
        raise StepFailedError("codex_interface_delta")
    try:
        with policy_preview.connection(db_path(ctx.data)) as conn:
            conn.row_factory = sqlite3.Row
            validate_policy_source(conn)
            control = conn.execute("SELECT * FROM review_control").fetchone()
            old = ReviewPolicy.model_validate_json(control["policy_json"])
            if digest(old) != control["policy_digest"]:
                raise ValueError("capability_changed")
            _supported_decisions(conn, old)
            previous, legacy_digest = legacy_facts(ctx, old)
            target = review_managed_host.observe()
            candidate, enrollment = _candidate(
                ctx.home, old, target, review_codex_policy.host_identity()
            )
            selected = candidate.runner
            assert isinstance(selected, ManagedCodexSelection)
            migration_delta(old, candidate)
            capsule = capture(conn, old, candidate, host_enrollment=enrollment)
            scanned = scan_state(
                conn,
                db_path(ctx.data),
                candidate_digest=digest(candidate),
                containment=None,
                check=None,
            )
        deadline = time.monotonic() + 60.0
        observed = review_managed_files.observe(
            ctx.data, selected, deadline=deadline
        )
        observed = review_managed_probe.negotiate(
            ctx.data, selected, observed, deadline=deadline, persist=False
        )
        with policy_preview.connection(db_path(ctx.data)) as conn:
            conn.row_factory = sqlite3.Row
            again = scan_state(
                conn,
                db_path(ctx.data),
                candidate_digest=digest(candidate),
                containment=None,
                check=None,
            )
            if (
                again != scanned
                or capture(conn, old, candidate, host_enrollment=enrollment)
                != capsule
                or legacy_facts(ctx, old) != (previous, legacy_digest)
            ):
                raise StepFailedError("codex_interface_state_changed")
            if review_managed_host.observe(deadline=deadline) != target:
                raise StepFailedError("codex_host_changed")
        return InterfacePlan(
            kind="codex-managed-interface-plan-v1",
            home=ctx.data,
            old=old,
            candidate=candidate,
            old_binding=previous,
            host_enrollment=enrollment,
            legacy_artifacts_digest=legacy_digest,
            observation=observed,
            bridge=capsule,
            state=scanned.state,
            containment=scanned.containment,
            minimum_runtime=MANAGED_HOST_RUNTIME,
        )
    except ValueError as exc:
        code = str(exc)
        if code in {
            "runtime_selection_unavailable",
            "runtime_selection_unsafe",
            "runtime_interface_unsupported",
            "runtime_probe_unavailable",
            "runtime_structure_limit",
            "codex_interface_legacy_approval_invalid",
            "codex_host_identity_unavailable",
            "codex_host_changed",
            "codex_platform_changed",
            "codex_data_home_changed",
        }:
            raise StepFailedError(code) from None
        raise StepFailedError("codex_interface_state_changed") from None
    except (OSError, TypeError):
        raise StepFailedError("codex_interface_state_changed") from None


def _candidate(
    home: Path, old: ReviewPolicy, target: ManagedHostIdentity, observed: str
) -> tuple[ReviewPolicy, HostEnrollment]:
    """Copy exact non-host terms into the one disclosed new host rule."""
    terms = old.experimental_codex
    assert isinstance(terms, CodexExperiment)
    enrollment = HostEnrollment(
        kind="codex-managed-host-enrollment-v1",
        saved_legacy_host=terms.host_identity,
        observed_legacy_host=observed,
        target=target,
    )
    selected = ManagedCodexSelection(
        selection="codex_managed_interface_v1",
        runner="codex_cli",
        manager_root=home / ".codex/packages/standalone",
        selector="current",
        interface="codex-json-review-v1",
        model=old.runner.model,
        reasoning=old.runner.reasoning,
    )
    enrolled = ManagedCodexExperiment(
        kind="ongoing-mac-codex-managed-v1",
        activation_authorized=terms.activation_authorized,
        host_identity=target,
        data_home=terms.data_home,
        accepted_uncertainties=terms.accepted_uncertainties,
    )
    candidate = ReviewPolicy.model_validate_json(
        canonical_json(
            old.model_copy(
                update={"runner": selected, "experimental_codex": enrolled}
            )
        )
    )
    return candidate, enrollment


def _supported_decisions(conn: sqlite3.Connection, old: ReviewPolicy) -> None:
    """Refuse old decision work that cannot use the finite request bridge."""
    unsupported = conn.execute(
        "SELECT 1 FROM memory_candidate c JOIN review_request r "
        "ON r.id=c.request_id WHERE r.policy_digest=? AND "
        "(c.state IN ('waiting_verification','approved_pending','blocked') "
        "OR EXISTS (SELECT 1 FROM memory_decision d WHERE "
        "d.candidate_id=c.id AND d.state='waiting_verification')) LIMIT 1",
        (digest(old),),
    ).fetchone()
    if unsupported is not None:
        raise StepFailedError("codex_interface_decision_unsupported")


def same_plan(ctx: Ctx, approved: InterfacePlan) -> bool:
    """Normalize only a newly observed legacy diagnostic in both copies."""
    fresh = preview(ctx)
    diagnostic = approved.host_enrollment.observed_legacy_host
    enrollment = fresh.host_enrollment.model_copy(
        update={"observed_legacy_host": diagnostic}
    )
    normalized = fresh.model_copy(
        update={
            "host_enrollment": enrollment,
            "bridge": fresh.bridge.model_copy(
                update={"host_enrollment": enrollment}
            ),
        }
    )
    return (
        InterfacePlan.model_validate_json(canonical_json(normalized))
        == approved
    )


def summary(plan: InterfacePlan) -> dict[str, object]:
    """Expose safe trust differences and counts without proof bodies."""
    selected = plan.candidate.runner
    assert isinstance(selected, ManagedCodexSelection)
    return {
        "plan_digest": plan.digest,
        "kind": plan.kind,
        "manager_root": str(selected.manager_root),
        "interface": "codex-json-review-v1",
        "platform": "Darwin/arm64",
        "observed_version": plan.observation.requested.version,
        "observed_executable_sha256": plan.observation.executable_sha256,
        "package_sha256": plan.observation.package_sha256,
        "preserved_requests": len(plan.bridge.requests),
        "preserved_inputs": sum(
            len(item.inputs) for item in plan.bridge.requests
        ),
        "nonruntime_terms_changed": False,
        "host_binding_changed": True,
        "saved_legacy_host": plan.host_enrollment.saved_legacy_host,
        "observed_legacy_host": plan.host_enrollment.observed_legacy_host,
        "legacy_observation": "observed at preview",
        "target_host": plan.host_enrollment.target.model_dump(mode="json"),
        "predecessor_host_continuity": "unverified",
        "trust": "owner-trusted-local-manager;auxiliary-or"
        "igin-unknown;same-uid-co"
        "mpromise-possible",
        "activation": "fresh-owner-approval-required",
    }
