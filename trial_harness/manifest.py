"""Hashing helpers, Stage-B freeze manifest and post-hoc re-hash (§4)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

MANIFEST = "freeze-manifest.json"
FROZEN_B = "FROZEN-B.sha256"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while block := source.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    """sha256 of every regular file under root except __pycache__."""
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            path = Path(base, name)
            if path.is_file() and not path.is_symlink():
                out[str(path.relative_to(root))] = sha256_file(path)
    return out


def make_dirs(path: Path) -> None:
    """mkdir -p where every directory created is 0700 (not umask)."""
    missing = []
    path = Path(path)
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)


def write_private(path: Path, data: bytes) -> None:
    """Atomic write, mode 0600, creating missing parents as 0700."""
    path = Path(path)
    make_dirs(path.parent)
    temporary = path.with_name(f".{path.name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    os.replace(temporary, path)


def write_json(path: Path, value: object) -> None:
    text = json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False)
    write_private(path, (text + "\n").encode())


def build_stage_b(
    files: dict[str, list[Path]],
    trees: dict[str, Path],
    values: dict[str, object],
) -> dict:
    """Hash named file groups and trees; embed pre-computed values."""
    return {
        "schema_version": 1,
        "files": {
            section: {str(Path(p)): sha256_file(p) for p in paths}
            for section, paths in files.items()
        },
        "trees": {
            section: {"root": str(root), "files": tree_hashes(root)}
            for section, root in trees.items()
        },
        "values": values,
    }


def write_stage_b(trial: Path, built: dict) -> str:
    """Write freeze-manifest.json once and record its sha256."""
    target, frozen = Path(trial, MANIFEST), Path(trial, FROZEN_B)
    if target.exists() or frozen.exists():
        raise FileExistsError(target)
    write_json(target, built)
    target.chmod(0o444)
    digest = sha256_file(target)
    write_private(frozen, f"{digest}  {MANIFEST}\n".encode())
    frozen.chmod(0o444)
    return digest


def load_stage_b(trial: Path) -> dict:
    target = Path(trial, MANIFEST)
    recorded = Path(trial, FROZEN_B).read_text().split()[0]
    if sha256_file(target) != recorded:
        raise ValueError("freeze-manifest.json does not match FROZEN-B")
    return json.loads(target.read_text())


def _diff(section: str, before: dict, after: dict) -> list[dict]:
    out = []
    for path in sorted(set(before) | set(after)):
        if path not in after:
            kind = "REMOVED"
        elif path not in before:
            kind = "ADDED"
        elif before[path] != after[path]:
            kind = "CHANGED"
        else:
            continue
        out.append({"section": section, "path": path, "kind": kind})
    return out


def verify_stage_b(trial: Path, sections: set[str] | None = None) -> list:
    """Re-hash every recorded file and tree; return the differences."""
    built = load_stage_b(trial)
    deviations = []
    for section, recorded in built["files"].items():
        if sections is None or section in sections:
            now = {
                p: sha256_file(p) if Path(p).is_file() else None
                for p in recorded
            }
            now = {p: h for p, h in now.items() if h is not None}
            deviations += _diff(section, recorded, now)
    for section, tree in built["trees"].items():
        if sections is None or section in sections:
            now = tree_hashes(Path(tree["root"]))
            deviations += _diff(section, tree["files"], now)
    return deviations


def posthoc_rehash(
    trial: Path,
    frozen_inputs: set[str],
    sections: set[str] | None = None,
    repo_section: str = "repo_tree",
) -> dict:
    """§4.5: any difference is a DEVIATION; a frozen REPO input voids."""
    deviations = verify_stage_b(trial, sections)
    void = any(
        d["section"] == repo_section and d["path"] in frozen_inputs
        for d in deviations
    )
    status = "VOID" if void else "DEVIATION" if deviations else "CLEAN"
    return {"status": status, "deviations": deviations}


def main(argv: list[str] | None = None) -> int:
    """write SPEC | verify | posthoc (Stage B, §4.4 and §4.5).

    SPEC is JSON: {files: {section: [path...]}, trees: {section: root},
    values: {section: value}}. The REPO frozen inputs for §4.5 are
    prep.FROZEN_INPUTS; exit 1 on any deviation.
    """
    from trial_harness.prep import FROZEN_INPUTS, TRIAL  # lazy: cycle

    parser = argparse.ArgumentParser(prog="trial_harness.manifest")
    parser.add_argument("command", choices=("write", "verify", "posthoc"))
    parser.add_argument("spec", nargs="?", type=Path)
    parser.add_argument("--trial", type=Path, default=TRIAL)
    args = parser.parse_args(argv)
    if args.command == "write":
        spec = json.loads(args.spec.read_text())
        built = build_stage_b(
            {k: list(map(Path, v)) for k, v in spec["files"].items()},
            {k: Path(v) for k, v in spec["trees"].items()},
            spec["values"],
        )
        print(json.dumps({"sha256": write_stage_b(args.trial, built)}))
        return 0
    if args.command == "verify":
        deviations = verify_stage_b(args.trial)
        verdict = {
            "status": "DEVIATION" if deviations else "CLEAN",
            "deviations": deviations,
        }
    else:
        verdict = posthoc_rehash(args.trial, set(FROZEN_INPUTS))
    print(json.dumps(verdict, indent=1))
    return 0 if verdict["status"] == "CLEAN" else 1


if __name__ == "__main__":
    raise SystemExit(main())
