"""Probe supported native security setters on validated empty siblings only."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from unittest import mock

from install import config_windows, configedit


def sample_fields(
    variant: int,
    stage: str,
    original: tuple[int, bytes, bytes, bytes],
    current: tuple[int, bytes, bytes, bytes],
) -> dict[str, int]:
    """Describe exact components using finite variant and stage codes.

    Args:
        variant: One of the three supported setter flag combinations.
        stage: Initial creation or final setter readback.
        original: Original descriptor-bound components.
        current: Empty sibling's descriptor-bound components.

    Returns:
        Numeric controls and component equality only.

    Raises:
        ValueError: If variant or stage is outside the probe contract.
    """
    if variant not in {1, 2, 3} or stage not in {"initial", "final"}:
        raise ValueError("unsupported security probe sample")
    prefix = f"probe_v{variant}_{stage}_"
    return {
        prefix + "control": current[0],
        prefix + "owner_equal": int(original[1] == current[1]),
        prefix + "group_equal": int(original[2] == current[2]),
        prefix + "dacl_equal": int(original[3] == current[3]),
    }


def _parts(handle: int) -> tuple[int, bytes, bytes, bytes]:
    """Read native security from the already validated empty sibling handle."""
    if sys.platform != "win32":
        raise OSError("security probe is Windows-only")
    descriptor = ctypes.c_void_p()
    code = config_windows._get(
        handle, 1, 7, None, None, None, None, ctypes.byref(descriptor)
    )
    if code:
        raise ctypes.WinError(code)
    try:
        return config_windows.security_parts(
            config_windows._descriptor_bytes(descriptor)
        )
    finally:
        config_windows._free(descriptor)


def probe(path: Path) -> dict[str, int]:
    """Test setter flags without writing content or altering the source.

    Args:
        path: New synthetic private config under the isolated provider home.

    Returns:
        Finite numeric results for creation and three setter variants.
    """
    if sys.platform != "win32":
        return {}
    configedit.atomic_write(path, b"{}", 0o600)
    metrics: dict[str, int] = {}
    for variant, flags in ((1, 7), (2, 7 | 0x20000000), (3, 3)):

        def capture(
            handle: int,
            owner: ctypes.c_void_p,
            group: ctypes.c_void_p,
            dacl: ctypes.c_void_p,
            descriptor: ctypes.c_void_p,
            variant_code: int = variant,
            setter_flags: int = flags,
        ) -> None:
            if sys.platform != "win32" or not all(
                (owner.value, group.value, dacl.value)
            ):
                raise OSError("native security probe components unavailable")
            original = config_windows.security_parts(
                config_windows._descriptor_bytes(descriptor)
            )
            metrics["probe_original_control"] = original[0]
            metrics.update(
                sample_fields(
                    variant_code, "initial", original, _parts(handle)
                )
            )
            code = config_windows._set(
                handle,
                1,
                setter_flags,
                owner,
                group,
                dacl if setter_flags & 4 else None,
                None,
            )
            metrics[f"probe_v{variant_code}_error"] = int(code)
            metrics.update(
                sample_fields(variant_code, "final", original, _parts(handle))
            )

        with mock.patch.object(
            config_windows, "_restore", side_effect=capture
        ):
            fd, temporary = config_windows.replacement(path)
        try:
            if os.fstat(fd).st_size:
                raise OSError("security probe sibling is not empty")
        finally:
            os.close(fd)
            temporary.unlink()
    path.unlink()
    return metrics
