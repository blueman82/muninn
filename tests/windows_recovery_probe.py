"""Collect native Windows boot-counter readiness without granting recovery.

Microsoft documents the registry counter in its OEM memory-integrity guide.
Same-boot samples establish availability only. They do not prove reboot,
hibernation, restore, file retirement, or desktop recovery safety.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from muninn import platform_io, store

if sys.platform == "win32":
    import winreg

_KEY = (
    r"SYSTEM\CurrentControlSet\Control\Session Manager"
    r"\Memory Management\PrefetchParameters"
)


def validate_counter(value: object, kind: int) -> int:
    """Require an exact unsigned DWORD observation from the native registry.

    Args:
        value: Untrusted value returned by the registry API.
        kind: Native registry value type, where REG_DWORD is four.

    Returns:
        The exact counter, including its zero and maximum endpoints.

    Raises:
        ValueError: If the type or range cannot establish a counter.
    """
    if kind != 4 or type(value) is not int or not 0 <= value < 2**32:
        raise ValueError("boot_counter_invalid")
    return value


def observe() -> int:
    """Read one counter from the native 64-bit system registry view.

    Returns:
        The observed boot counter, which is not recovery authority.

    Raises:
        RuntimeError: If the caller is not on native Windows.
        OSError: If the native key or value is unavailable.
        ValueError: If the native value is malformed.
    """
    if sys.platform != "win32":
        raise RuntimeError("windows_host_required")
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        _KEY,
        access=winreg.KEY_QUERY_VALUE | winreg.KEY_WOW64_64KEY,
    ) as key:
        value, kind = winreg.QueryValueEx(key, "BootId")
    return validate_counter(value, kind)


def verify_samples(samples: list[int]) -> int:
    """Refuse empty or changing observations before reporting readiness.

    Args:
        samples: Observations from this and independent fresh processes.

    Returns:
        The common counter, without inferring any boot transition.

    Raises:
        ValueError: If samples are absent, malformed, or disagree.
    """
    if not samples:
        raise ValueError("boot_counter_unstable")
    checked = [validate_counter(value, 4) for value in samples]
    if any(value != checked[0] for value in checked):
        raise ValueError("boot_counter_unstable")
    return checked[0]


def _fresh_sample() -> int:
    """Read a counter in a separate interpreter without provider activity."""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "tests.windows_recovery_probe",
            "--sample",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=True,
        timeout=30,
    )
    return validate_counter(json.loads(result.stdout), 4)


def failure_code(error: Exception) -> str:
    """Project probe errors to finite diagnostics without exception content.

    Args:
        error: Native read, parsing, or subprocess failure.

    Returns:
        A fixed code suitable for the content-free native evidence report.
    """
    if isinstance(error, ValueError) and str(error) == "boot_counter_unstable":
        return "boot_counter_unstable"
    categories = (
        (FileNotFoundError, "boot_counter_absent"),
        (PermissionError, "boot_counter_access_denied"),
        (subprocess.TimeoutExpired, "fresh_process_timeout"),
        (subprocess.CalledProcessError, "fresh_process_failed"),
        (ValueError, "boot_counter_invalid"),
        (RuntimeError, "windows_host_required"),
        (OSError, "native_registry_unavailable"),
    )
    for kind, code in categories:
        if isinstance(error, kind):
            return code
    return "native_boot_counter_probe_failed"


def main() -> int:
    """Write finite native evidence under the configured proof scratch root.

    Returns:
        Zero for stable native readiness, two for an explicit probe failure.
    """
    if sys.argv[1:] == ["--sample"]:
        print(json.dumps(observe()))
        return 0
    root = Path(os.environ["MUNINN_NATIVE_SCRATCH"])
    if not root.is_dir() or not platform_io.is_private(root, directory=True):
        raise ValueError("private_proof_scratch_required")
    directory = root / "muninn-native-diagnostics"
    platform_io.ensure_private_dir(directory)
    result: dict[str, object] = {
        "schema_version": 1,
        "probe": "windows_boot_counter_readiness",
        "sha": os.environ.get("GITHUB_SHA"),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "qualified": False,
        "reboot_proof": False,
        "desktop_proof": False,
        "retirement_proof": False,
    }
    try:
        samples = [observe() for _ in range(3)]
        samples.extend(_fresh_sample() for _ in range(2))
        result.update(
            code="stable_native_counter",
            boot_counter=verify_samples(samples),
            sample_count=len(samples),
            fresh_process_count=2,
        )
        exit_code = 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        result.update(code=failure_code(error))
        exit_code = 2
    store.write_json_atomic(directory / "muninn-recovery-probe.json", result)
    print(json.dumps(result, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
