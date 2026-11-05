"""Prepare a nonwriting exact legacy-to-managed interface proposal."""

from __future__ import annotations

import hashlib
import sqlite3
import time

from install import policy_preview
from install.codex_interface_models import InterfacePlan
from install.context import Ctx, StepFailedError
from install.host_renewal_preview import scan_state
from install.policy_source_format import validate_policy_source
from muninn import (
    review_codex_launch,
    review_managed_files,
    review_managed_probe,
)
from muninn.review_codex_policy import check_host
from muninn.review_json import (
    canonical_json,
    canonical_json_value,
    result_schema,
)
from muninn.review_launch_inputs import PromptArtifact
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from muninn.review_rounds import round_schema
from muninn.review_runner import (
    RunnerCapability,
    check_private_path,
    read_private_file,
)
from muninn.review_runtime_bridge import capture, selection_only
from muninn.review_runtime_models import (
    ManagedCodexSelection,
    require_legacy_runner,
)
from muninn.store import db_path


def legacy_binding(
    ctx: Ctx, policy: ReviewPolicy
) -> review_codex_launch.ExperimentalBinding:
    """Check saved approval integrity without the old runtime."""
    require_legacy_runner(policy.runner)
    terms = policy.experimental_codex
    if terms is None:
        raise ValueError("codex_approval_required")
    check_host(terms, ctx.data)
    directory = ctx.data / "review-capabilities"
    for path in (
        ctx.data,
        directory,
        directory / "selected",
        ctx.data / "review-prompts",
    ):
        check_private_path(path, directory=True)
    capability = RunnerCapability.model_validate_json(
        read_private_file(
            directory / "selected" / (digest(policy) + ".json"),
            65536,
        )
    )
    binding = review_codex_launch.stored_binding(ctx.data, capability)
    raw = read_private_file(
        ctx.data / "review-prompts" / (policy.prompt_digest + ".json"),
        policy.max_input_bytes,
    )
    if (
        binding.requested != policy.runner
        or binding.host_identity != terms.host_identity
        or capability.status != "unverified"
        or any(
            getattr(capability, key) != value
            for key, value in policy.runner.model_dump().items()
        )
        or capability.schema_digest
        != hashlib.sha256(
            canonical_json_value(round_schema()).encode()
        ).hexdigest()
        or policy.result_schema_digest
        != hashlib.sha256(
            canonical_json_value(result_schema()).encode()
        ).hexdigest()
        or hashlib.sha256(raw).hexdigest() != policy.prompt_digest
        or canonical_json(PromptArtifact.model_validate_json(raw)).encode()
        != raw
    ):
        raise ValueError("capability_changed")
    return binding


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
            previous = legacy_binding(ctx, old)
            selected = ManagedCodexSelection(
                selection="codex_managed_interface_v1",
                runner="codex_cli",
                manager_root=ctx.home / ".codex/packages/standalone",
                selector="current",
                interface="codex-json-review-v1",
                model=old.runner.model,
                reasoning=old.runner.reasoning,
            )
            candidate = ReviewPolicy.model_validate_json(
                canonical_json(old.model_copy(update={"runner": selected}))
            )
            selection_only(old, candidate)
            capsule = capture(conn, old, candidate)
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
                or capture(conn, old, candidate) != capsule
                or legacy_binding(ctx, old) != previous
            ):
                raise StepFailedError("codex_interface_state_changed")
        return InterfacePlan(
            kind="codex-managed-interface-plan-v1",
            home=ctx.data,
            old=old,
            candidate=candidate,
            old_binding=previous,
            observation=observed,
            bridge=capsule,
            state=scanned.state,
            containment=scanned.containment,
            minimum_runtime="codex-managed-interface-v1",
        )
    except ValueError as exc:
        code = str(exc)
        if code in {
            "runtime_selection_unavailable",
            "runtime_selection_unsafe",
            "runtime_interface_unsupported",
            "runtime_probe_unavailable",
            "runtime_structure_limit",
        }:
            raise StepFailedError(code) from None
        raise StepFailedError("codex_interface_state_changed") from None
    except (OSError, TypeError):
        raise StepFailedError("codex_interface_state_changed") from None


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
        "trust": "owner-trusted-local-manager;auxiliary-or"
        "igin-unknown;same-uid-co"
        "mpromise-possible",
        "activation": "fresh-owner-approval-required",
    }
