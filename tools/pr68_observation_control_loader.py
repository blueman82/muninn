"""Load exact diagnostic copies with one temporary logical collector alias."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import muninn

ROOT = Path(__file__).resolve().parent.parent


def source_module(name: str, path: Path) -> ModuleType:
    """Load fixture bytes without running a CLI, native call or process."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("fixture_source_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load() -> tuple[ModuleType, ModuleType, ModuleType]:
    """Share one collector and restore temporary import aliases."""
    names = (
        "muninn._cleanup_observation",
        "_pr68_native_controls",
        "_pr68_process_controls",
    )
    previous = {name: sys.modules.get(name) for name in names}
    missing = object()
    attribute = getattr(muninn, "_cleanup_observation", missing)
    try:
        collector = source_module(
            names[0], ROOT / "tools/_cleanup_observation.py"
        )
        muninn.__dict__["_cleanup_observation"] = collector
        native = source_module(
            names[1],
            ROOT
            / "tools/pr68_observation_payload/review_process_quiescence.py",
        )
        process = source_module(
            names[2], ROOT / "tools/pr68_observation_payload/test_process.py"
        )
        return collector, native, process
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        if attribute is missing:
            muninn.__dict__.pop("_cleanup_observation", None)
        else:
            muninn.__dict__["_cleanup_observation"] = attribute


observation, native, process = load()
