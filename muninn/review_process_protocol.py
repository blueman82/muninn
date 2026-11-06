"""Frame bounded parent activation and finite wrapper lifecycle messages."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError

from muninn.knowledge_models import StrictModel, UtcSeconds
from muninn.review_json import (
    canonical_json_value,
    finite_float,
    reject_constant,
    unique_object,
)
from muninn.review_models import ProcessIdentity
from muninn.review_runtime_models import ActivationRuntime, RuntimeIdentity

CONTROL_HEADER_MAX = 1024 * 1024


class Control(StrictModel):
    """Accept only fixed parent-selected command data without prompt text."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    execution: RuntimeIdentity | None = None
    refresh_count: Literal[0, 1] = 0
    replacement: bool = False


class LaunchNotice(StrictModel):
    """Bind one CLI creation notice to finite typed metadata."""

    launched_at: UtcSeconds
    identity: ProcessIdentity
    identity_digest: str | None = None


class PrestartRefusalNotice(StrictModel):
    """Acknowledge that this owned wrapper created no native client."""

    identity_digest: str
    code: Literal["runtime_changed", "exec_enoent"]


class TerminalNotice(StrictModel):
    """Carry one actual CLI exit code without arbitrary lifecycle fields."""

    exit_code: int


def load(raw: bytes) -> object:
    """Parse strict bounded metadata without duplicate or nonfinite values."""
    if len(raw) > CONTROL_HEADER_MAX:
        raise ValueError("control_limit")
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
        parse_float=finite_float,
    )


def object_packet(raw: bytes) -> dict[str, object]:
    """Require a bounded unique-key wrapper control object."""
    value = load(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid_worker_protocol")
    return cast(dict[str, object], value)


def activation(
    argv: tuple[str, ...],
    cwd: Path,
    env: dict[str, str],
    prompt: bytes,
    limit: int,
    *,
    runtime: ActivationRuntime | None = None,
) -> bytes:
    """Keep raw prompt length separate from escaped launch metadata."""
    data: dict[str, object] = {"argv": argv, "cwd": str(cwd), "env": env}
    if runtime is not None:
        data.update(runtime.model_dump(mode="json"))
    header = canonical_json_value(data).encode()
    if len(header) > CONTROL_HEADER_MAX or len(prompt) > limit:
        raise ValueError("activation_limit")
    total = 4 + len(header) + len(prompt)
    if total >= 2**64:
        raise ValueError("activation_limit")
    return struct.pack("!QI", total, len(header)) + header + prompt


def activation_size(prefix: bytes | bytearray, limit: int) -> int:
    """Reject impossible announced sizes before allocating their body."""
    if len(prefix) < 12:
        raise ValueError("activation_incomplete")
    total, header = struct.unpack("!QI", prefix[:12])
    if (
        total > 4 + CONTROL_HEADER_MAX + limit
        or header > CONTROL_HEADER_MAX
        or total < 4 + header
        or total - 4 - header > limit
    ):
        raise ValueError("activation_limit")
    return total + 8


def decode_control(raw: bytes, limit: int) -> tuple[Control, bytes]:
    """Validate one complete activation and refuse extra activation bytes."""
    if len(raw) != activation_size(raw, limit):
        raise ValueError("activation_incomplete")
    header = struct.unpack("!I", raw[8:12])[0]
    try:
        load(raw[12 : 12 + header])
        control = Control.model_validate_json(raw[12 : 12 + header])
    except (ValidationError, UnicodeError, RecursionError):
        raise ValueError("activation_invalid") from None
    if (
        not control.argv
        or not Path(control.argv[0]).is_absolute()
        or not control.cwd.is_absolute()
        or any("\0" in value for value in control.argv)
        or any("\0" in k + v or "=" in k for k, v in control.env.items())
    ):
        raise ValueError("activation_invalid")
    if (
        control.execution is not None
        and control.argv[0] != control.execution.requested.executable
    ):
        raise ValueError("activation_invalid")
    if control.execution is None and (
        control.refresh_count or control.replacement
    ):
        raise ValueError("activation_invalid")
    return control, raw[12 + header :]


def decode_activation(
    raw: bytes, limit: int
) -> tuple[tuple[str, ...], Path, dict[str, str], bytes]:
    """Retain the exact legacy public activation decoder projection."""
    control, prompt = decode_control(raw, limit)
    return control.argv, control.cwd, control.env, prompt


def packet(tag: bytes, raw: bytes) -> bytes:
    """Prefix transport bytes without letting native output forge events."""
    if tag not in (b"R", b"L", b"T", b"O", b"E", b"P"):
        raise ValueError("invalid_worker_protocol")
    return tag + struct.pack("!I", len(raw)) + raw
