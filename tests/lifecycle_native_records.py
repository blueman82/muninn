"""Test-side recorders that note which product check rejected a step."""

from __future__ import annotations

import base64
import json
import subprocess
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
    """Make verify note what the installed doctor command returned.

    Returns:
        Four numbers: the parsed output length and first byte, then the
        doctor process exit status and the length of its error output.
    """
    seen = [0, 0, -1, 0]
    real = subprocess.run

    def loads(raw: bytes) -> Any:
        """Parse like json.loads, keeping the length and first byte."""
        seen[:2] = [min(len(raw), 65535), raw[0] if raw else 0]
        return json.loads(raw)

    def run(*args: Any, **kwargs: Any) -> Any:
        """Run for real, noting the result of the doctor launcher call."""
        result = real(*args, **kwargs)
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list) and "-EncodedCommand" in argv:
            script = base64.b64decode(argv[-1]).decode("utf-16-le")
            if "ZG9jdG9y" in script:
                seen[2:] = [
                    min(abs(result.returncode), 65535),
                    min(len(result.stderr or b""), 65535),
                ]
        return result

    subprocess.run = run
    verify.json = types.SimpleNamespace(loads=loads, dumps=json.dumps)
    return seen
