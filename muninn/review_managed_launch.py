"""Prepare a checked current execution under immutable managed authority."""

from __future__ import annotations

import hashlib
import os
import platform
from dataclasses import replace

from muninn import (
    review_codex_launch,
    review_managed_files,
    review_managed_probe,
    store,
)
from muninn.review_json import (
    canonical_json,
    canonical_json_value,
    result_schema,
)
from muninn.review_launch_spec import LaunchSpec, launch_spec
from muninn.review_models import ReviewContext
from muninn.review_rounds import round_schema
from muninn.review_runtime_authority import (
    artifact as artifact,
)
from muninn.review_runtime_authority import (
    checked_binding as checked_binding,
)
from muninn.review_runtime_authority import (
    fingerprint as fingerprint,
)
from muninn.review_runtime_models import (
    ManagedCapability,
)


def prepare_managed(
    context: ReviewContext, capability: ManagedCapability, prompt: bytes
) -> LaunchSpec:
    """Negotiate the current interface without changing the stable request."""
    home = store.data_home(os.environ)
    binding, approval = checked_binding(home, capability)
    terms = context.policy.experimental_codex
    if terms is None:
        raise ValueError("codex_approval_required")
    if terms.data_home != home:
        raise ValueError("codex_data_home_changed")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("runtime_interface_unsupported")
    if (
        terms.host_identity != review_codex_launch.host_identity()
        or terms.host_identity != binding.host_identity
    ):
        raise ValueError("codex_host_changed")
    if context.scope.project_id is None:
        raise ValueError("codex_project_required")
    if (
        binding.requested != context.policy.runner
        or approval.new_policy_digest
        != hashlib.sha256(canonical_json(context.policy).encode()).hexdigest()
        or capability.schema_digest
        != hashlib.sha256(
            canonical_json_value(round_schema()).encode()
        ).hexdigest()
        or context.policy.result_schema_digest
        != hashlib.sha256(
            canonical_json_value(result_schema()).encode()
        ).hexdigest()
    ):
        raise ValueError("capability_changed")
    native = review_codex_launch.native_prompt(context, prompt)
    observed = review_managed_files.observe(
        home, capability.requested, deadline=context.deadline
    )
    observed = review_managed_probe.negotiate(
        home,
        capability.requested,
        observed,
        deadline=context.deadline,
        persist=True,
    )
    spec = launch_spec(
        context,
        binding.cli_cwd,
        {
            "HOME": str(home),
            "CODEX_HOME": str(binding.auth_home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        },
        native,
        schema_path=binding.schema_path,
        requested=observed.requested,
    )
    return replace(spec, execution=observed)
