"""Test-side recorders that note which product check rejected a step."""

from __future__ import annotations

import json
import types
import xml.etree.ElementTree as ET
from typing import Any

from install import lifecycle, steps_release, verify
from install.context import Ctx
from muninn.obs_service import task_mismatch


def record_task_mismatch() -> list[int]:
    """Make the product ownership check also note which check failed.

    Returns:
        A list that receives the number of each failing check.
    """
    seen: list[int] = []

    def recording(expected: ET.Element, actual: ET.Element) -> bool:
        """Report the product verdict while keeping only the check number."""
        index = task_mismatch(expected, actual)
        if index is not None:
            seen.append(index)
        return index is None

    lifecycle.task_matches = recording
    return seen


def record_writer_mismatch() -> list[int]:
    """Make the product writer binding also note which check failed.

    Returns:
        A list that receives the number of each failing check.
    """
    seen: list[int] = []
    real = lifecycle.writer_mismatch

    def recording(*args: Any) -> int | None:
        """Pass the product verdict through while keeping the check number."""
        index = real(*args)
        if index is not None:
            seen.append(index)
        return index

    lifecycle.writer_mismatch = recording
    return seen


def record_new_job() -> list[int]:
    """Make the new-job wait also note what the job lookup last showed.

    Returns:
        A one-item list holding bit 1 for a job, 2 for a live pid and 4 for
        a job running from the new release.
    """
    seen = [0]
    real = steps_release.is_new

    def recording(ctx: Ctx, job: Any) -> bool:
        """Pass the product verdict through while keeping three flags."""
        verdict = real(ctx, job)
        seen[0] = (
            (1 if job else 0)
            | (2 if job and job["pid"] else 0)
            | (4 if verdict else 0)
        )
        return verdict

    steps_release.is_new = recording
    return seen


def record_doctor_output() -> list[int]:
    """Make verify note the size and first byte of the doctor output.

    Returns:
        A two-item list: the output length and its first byte.
    """
    seen = [0, 0]

    def loads(raw: bytes) -> Any:
        """Parse like json.loads, keeping the length and first byte."""
        seen[:] = [min(len(raw), 65535), raw[0] if raw else 0]
        return json.loads(raw)

    verify.json = types.SimpleNamespace(loads=loads)
    return seen
