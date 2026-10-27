"""Prepare original-HEAD source in an isolated manual hosted worktree."""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import os
import shutil
from pathlib import Path

from tools.pr68_observation_binding import BASE, ROOT, digest, git, write

PAYLOADS = {
    "muninn/_cleanup_observation.py": "tools/_cleanup_observation.py",
    "tools/test_process.py": "tools/pr68_observation_payload/test_process.py",
    "muninn/review_process_quiescence.py": (
        "tools/pr68_observation_payload/review_process_quiescence.py"
    ),
}


def original_sources() -> dict[str, dict[str, str]]:
    """Refuse carrier drift and bind original objects before any overlay."""
    records: dict[str, dict[str, str]] = {}
    for entry in git("ls-tree", "-rz", BASE).split(b"\0"):
        if not entry:
            continue
        metadata, name_bytes = entry.split(b"\t", 1)
        name = name_bytes.decode()
        mode, kind, blob = metadata.decode().split()
        if kind != "blob":
            raise ValueError("unsupported_base_entry")
        original = git("cat-file", "blob", blob)
        current = (ROOT / name).read_bytes()
        if (
            name != ".github/workflows/worker-quiescence.yml"
            and current != original
        ):
            raise ValueError("original_source_drift")
        records[name] = {
            "mode": mode,
            "blob": blob,
            "base_sha256": hashlib.sha256(original).hexdigest(),
            "carrier_sha256": digest(ROOT / name),
        }
    return records


def copy_overlay(destination: Path) -> list[str]:
    """Copy reviewed diagnostic sources without replacing carrier sources."""
    names = git("ls-files", "-z").decode().split("\0")
    copied = sorted(
        name
        for name in names
        if name == "muninn/_cleanup_observation.pyi"
        or (
            name.endswith(".py")
            and (
                name == "tools/_cleanup_observation.py"
                or name.startswith(
                    (
                        "tools/pr68_observation_",
                        "tools/pr68_observation_payload/",
                    )
                )
            )
        )
    )
    required = {
        "tools/_cleanup_observation.py",
        "tools/pr68_observation_binding.py",
        *PAYLOADS.values(),
    }
    if not required.issubset(copied):
        raise ValueError("diagnostic_source_missing")
    for name in copied:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    for name, payload in PAYLOADS.items():
        shutil.copy2(ROOT / payload, destination / name)
    return [*copied, *PAYLOADS]


def clear_overlay_caches(destination: Path) -> list[str]:
    """Remove caches only for the owned diagnostic overlay modules."""
    removed: list[str] = []
    for name in PAYLOADS:
        source = destination / name
        expected = Path(importlib.util.cache_from_source(str(source)))
        for candidate in expected.parent.glob(source.stem + ".*.pyc"):
            if candidate.is_relative_to(destination) and candidate.is_file():
                removed.append(str(candidate.relative_to(destination)))
                candidate.unlink()
    return removed


def apply() -> None:
    """Create a detached original-HEAD worktree and stage its exact overlay."""
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_REF")
        != "refs/heads/diagnostic/pr68-cleanup-observation-one-shot"
    ):
        raise ValueError("hosted_manual_carrier_required")
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("carrier_not_clean")
    records = original_sources()
    destination = Path(os.environ["MUNINN_CLEANUP_OBSERVATION_WORKTREE"])
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("fresh_diagnostic_worktree_required")
    carrier_head = git("rev-parse", "HEAD").decode().strip()
    carrier_tree = git("rev-parse", "HEAD^{tree}").decode().strip()
    git("merge-base", "--is-ancestor", BASE, "HEAD")
    git("worktree", "add", "--detach", str(destination), BASE)
    copied = copy_overlay(destination)
    removed = clear_overlay_caches(destination)
    (destination / ".venv").symlink_to(
        ROOT / ".venv", target_is_directory=True
    )
    git("-C", str(destination), "add", "--", *copied)
    index_tree = git("-C", str(destination), "write-tree").decode().strip()
    index_digest = hashlib.sha256(
        git("-C", str(destination), "ls-files", "--stage", "-z")
    ).hexdigest()
    actual_head = (
        git("-C", str(destination), "rev-parse", "HEAD").decode().strip()
    )
    if actual_head != BASE:
        raise ValueError("diagnostic_head_changed")
    patch = "".join(
        line
        for name in PAYLOADS
        for line in difflib.unified_diff(
            (
                (ROOT / name).read_text() if (ROOT / name).is_file() else ""
            ).splitlines(keepends=True),
            (destination / name).read_text().splitlines(keepends=True),
            fromfile=name,
            tofile=name,
        )
    )
    write(
        "overlay.json",
        {
            "base": BASE,
            "original": records,
            "patch": patch,
            "carrier_head": carrier_head,
            "carrier_tree": carrier_tree,
            "diagnostic_worktree": str(destination),
            "diagnostic_head": actual_head,
            "diagnostic_index_tree": index_tree,
            "diagnostic_index_digest": index_digest,
            "copied": {name: digest(destination / name) for name in copied},
            "removed_overlay_caches": removed,
            "cache_effect": (
                "fresh_worktree_overlay_caches_removed_not_original_warmth"
            ),
            "venv_link_target": str((ROOT / ".venv").resolve()),
            "child_loaded_closure": "not_observed",
        },
    )


if __name__ == "__main__":
    apply()
