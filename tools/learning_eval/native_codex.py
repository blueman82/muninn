"""Construct and revalidate the narrowly authorized native Codex trial."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import cast

from muninn.review_codex_output import output_contract_digest
from muninn.review_json import canonical_json
from muninn.review_launch_spec import codex_cli_argv
from muninn.review_runner import (
    RunnerCapability,
    check_private_path,
    config_fingerprint,
    file_digest,
)
from tools.learning_eval.account import Account
from tools.learning_eval.cases import digest, strict_json
from tools.learning_eval.models import Assessment, TrialAuthorization
from tools.learning_eval.native_terms import (
    NativeTerms,
    account_totals,
    authority_bytes,
    bind_account,
    host_identity,
    validate_terms,
)
from tools.learning_eval.pilot_evidence import immutable
from tools.learning_eval.pilot_models import RunnerBinding
from tools.learning_eval.task_protocol import protocol_schema
from tools.learning_eval.trial_models import Candidate

PINNED_EXECUTABLE_SHA256 = (
    "8813367285524ac798dd96d1b071aea30d58708c7226fd236cd275cf1cfdee31"
)

JSON_ONLY_INSTRUCTION = (
    "Return only the required JSON object. Do not invoke tools, commands, "
    "MCP, web search, or file operations."
)


def require_json_instruction(instructions: str) -> None:
    """Require the fixed no-action instruction in both declared prompts."""
    if JSON_ONLY_INSTRUCTION not in instructions:
        raise ValueError("native_prompt_instruction")


def wire_schema() -> dict[str, object]:
    """Constrain provider output to one strict object for both protocols."""
    return {
        "type": "object",
        "properties": {"payload_json": {"type": "string"}},
        "required": ["payload_json"],
        "additionalProperties": False,
    }


def native_prompt(prompt: bytes, protocol: str, limit: int) -> bytes:
    """Bind domain guidance into the actual bounded native stdin bytes."""
    strict_json(prompt)
    value: object = json.loads(prompt)
    if not isinstance(value, dict):
        raise ValueError("native_prompt_instruction")
    data = cast(dict[str, object], value)
    instructions = data.get("instructions")
    if not isinstance(instructions, str):
        raise ValueError("native_prompt_instruction")
    require_json_instruction(instructions)
    if "payload_schema" in data:
        raise ValueError("native_prompt_instruction")
    data["instructions"] = instructions + " " + WIRE_INSTRUCTION
    data["payload_schema"] = protocol_schema(protocol)
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > limit:
        raise ValueError("invalid_prompt_size")
    return raw


WIRE_INSTRUCTION = (
    "Serialize the JSON object matching payload_schema into the payload_json "
    "string of the required outer object. "
    "Do not return the inner object directly."
)


def validate_native_prompt(prompt: bytes, protocol: str) -> None:
    """Require exact program-owned domain guidance in the recorded bytes."""
    strict_json(prompt)
    value: object = json.loads(prompt)
    if not isinstance(value, dict):
        raise ValueError("native_prompt_instruction")
    data = cast(dict[str, object], value)
    instructions = data.get("instructions")
    if (
        not isinstance(instructions, str)
        or not instructions.endswith(" " + WIRE_INSTRUCTION)
        or data.get("payload_schema") != protocol_schema(protocol)
    ):
        raise ValueError("native_prompt_instruction")
    require_json_instruction(instructions)


def native_environment(candidate: Candidate, home: Path) -> dict[str, str]:
    """Select minimal explicit values without inspecting the real auth home."""
    auth, schema, terms = (
        candidate.auth_home,
        candidate.schema_path,
        candidate.native_terms,
    )
    if auth is None or schema is None or terms is None:
        raise ValueError("native_terms_required")
    for path in (home, candidate.cli_cwd):
        check_private_path(path, directory=True)
    if (
        platform.system() != "Darwin"
        or platform.machine() != "arm64"
        or terms.host_identity != host_identity()
        or home not in candidate.cli_cwd.parents
        or home not in schema.parents
        or not auth.is_absolute()
        or auth.resolve() != auth
        or not auth.is_dir()
        or auth == home
        or home in auth.parents
        or candidate.config_roots != (candidate.cli_cwd,)
        or candidate.requested.version != "0.163.0"
        or candidate.executable_sha256 != PINNED_EXECUTABLE_SHA256
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", candidate.requested.model
        )
        is None
        or candidate.requested.reasoning
        not in {"minimal", "low", "medium", "high", "xhigh"}
    ):
        raise ValueError("native_configuration_binding")
    authority_bytes(terms.owner_boundary_path, terms.owner_boundary_digest)
    authority_bytes(terms.carry_in_path, terms.carry_in_digest)
    if time.time() >= terms.started_at + 14400:
        raise ValueError("budget_wall_limit")
    raw = authority_bytes(schema, candidate.schema_digest)
    expected = canonical_schema(candidate.protocol)
    if raw != expected or candidate.semantic_schema_digest != digest(
        protocol_schema(candidate.protocol)
    ):
        raise ValueError("native_schema_binding")
    environment = {
        "HOME": str(home),
        "CODEX_HOME": str(auth),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }
    if candidate.environment_sha256 != {
        key: hashlib.sha256(value.encode()).hexdigest()
        for key, value in environment.items()
    }:
        raise ValueError("native_configuration_binding")
    return environment


def canonical_schema(protocol: str) -> bytes:
    """Return the program-owned canonical schema bytes for one protocol."""
    protocol_schema(protocol)
    return json.dumps(
        wire_schema(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def native_fingerprint(candidate: Candidate) -> str:
    """Bind selected settings and disposable cwd without scanning auth."""
    if candidate.schema_path is None:
        raise ValueError("native_schema_required")
    return digest(
        {
            "wire_instruction": WIRE_INSTRUCTION,
            "json_only_instruction": JSON_ONLY_INSTRUCTION,
            "candidate": candidate.model_dump(
                mode="json", exclude={"config_digest"}
            ),
            "argv": codex_cli_argv(
                candidate.requested, candidate.cli_cwd, candidate.schema_path
            ),
            "cwd": config_fingerprint(
                candidate.config_roots,
                candidate.environment_sha256,
                candidate.cli_cwd,
            ),
        }
    )


def create_binding(
    assessment: Assessment,
    terms: NativeTerms,
    home: Path,
    cwd: Path,
    auth_home: Path,
    candidate_path: Path,
    protocol: str,
) -> RunnerBinding:
    """Write the program schema before fingerprinting a native candidate."""
    validate_terms(terms, assessment, time.time())
    schema = immutable(
        home,
        protocol + ".schema.json",
        canonical_schema(protocol),
        1024 * 1024,
    )
    requested = assessment.policies[0].runner
    environment = {
        "HOME": str(home),
        "CODEX_HOME": str(auth_home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }
    candidate = Candidate.model_validate_json(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "native_codex_trial",
                "recipe": "codex-exec-macos-trial-v1",
                "protocol": protocol,
                "assessment_id": assessment.id,
                "budget_account_id": assessment.budget.account_id,
                "requested": requested.model_dump(mode="json"),
                "executable_sha256": file_digest(
                    Path(requested.executable), 256 * 1024 * 1024
                )[0],
                "schema_digest": hashlib.sha256(
                    canonical_schema(protocol)
                ).hexdigest(),
                "semantic_schema_digest": digest(protocol_schema(protocol)),
                "output_contract_digest": output_contract_digest(),
                "cli_cwd": str(cwd),
                "config_roots": [str(cwd)],
                "environment_sha256": {
                    key: hashlib.sha256(value.encode()).hexdigest()
                    for key, value in environment.items()
                },
                "config_digest": "0" * 64,
                "native_terms": terms.model_dump(mode="json"),
                "schema_path": str(schema),
                "auth_home": str(auth_home),
            }
        )
    )
    native_environment(candidate, home)
    candidate = candidate.model_copy(
        update={"config_digest": native_fingerprint(candidate)}
    )
    raw = canonical_json(candidate).encode()
    immutable(candidate_path.parent, candidate_path.name, raw, 1024 * 1024)
    return RunnerBinding(
        capability=RunnerCapability(
            **requested.model_dump(),
            status="unverified",
            proof_path=candidate_path,
            schema_digest=candidate.schema_digest,
            config_digest=candidate.config_digest,
        ),
        authorization=TrialAuthorization(
            assessment_id=assessment.id,
            candidate_digest=hashlib.sha256(raw).hexdigest(),
            config_digest=candidate.config_digest,
            budget=assessment.budget,
            disposable_home=home,
            approved_by=assessment.approved_by,
            approved_at=assessment.approved_at,
        ),
    )


def execution_terms(
    account: Account,
    capability: RunnerCapability,
    schedule_id: str,
    candidate_digest: str,
) -> NativeTerms | None:
    """Recheck carry-in and retain all known or reserved charges before use."""
    if capability.runner != "codex_cli":
        return None
    from_bytes = authority_bytes(capability.proof_path, candidate_digest)
    candidate = Candidate.model_validate_json(from_bytes)
    terms = candidate.native_terms
    if terms is None:
        raise ValueError("native_terms_required")
    bind_account(account, terms, time.time())
    _, charged = account_totals(account, terms)
    budgets = [
        account.assessment.budget,
        *(p.budget for p in account.assessment.policies),
    ]
    additional = max(
        Decimal(b.conservative_call_cost_decimal or "5") for b in budgets
    )
    if any(c.schedule.id == schedule_id for c in account.calls()):
        additional = Decimal(0)
    elif terms.carried_launches == 2 and len(account.calls()) >= 18:
        raise ValueError("budget_call_limit")
    ceiling = min(Decimal(b.max_cost_decimal or "245") for b in budgets)
    if Decimal(charged) - Decimal(terms.carried_charge) + additional > ceiling:
        raise ValueError("budget_cost_limit")
    return terms
