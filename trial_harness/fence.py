"""Mechanical T0 fence (protocol §1.2 F1, F2, G1).

F1 matches a record's own cwd (Claude top-level `cwd`, Codex
`payload.cwd`) by string prefix, so the sibling development checkouts
(`provenance-context-build`, `-memsys`, ...) are fenced too; a strict
path-component prefix would fence none of them. F2 applies only to
files whose original physical path the v3 inventory does not list.
"""

from __future__ import annotations

import json
from pathlib import Path

from trial_harness.extractor import Corpus, ws_norm
from trial_harness.manifest import sha256_file

F1_PREFIXES = (
    "/Users/garyharr/Github/provenance-context",
    "/Users/garyharr/.coderails",
)


def record_cwd(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    for cwd in (
        record.get("cwd"),
        payload.get("cwd") if isinstance(payload, dict) else None,
    ):
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def strings(value: object):
    """Every JSON-decoded string value (dict values and list items)."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def scan(path: Path, needles: dict[str, str], check_f2: bool):
    """Return (F1 cwds, F2 needle ids) for one file."""
    cwds, hits = set(), set()
    with open(path, "rb") as source:
        for raw in source:
            try:
                record = json.loads(raw)
            except ValueError:
                record = raw.decode("utf-8", "replace")
            cwd = record_cwd(record)
            if cwd and cwd.startswith(F1_PREFIXES):
                cwds.add(cwd)
            if check_f2 and len(hits) < len(needles):
                for text in map(ws_norm, strings(record)):
                    hits.update(k for k, n in needles.items() if n in text)
    return cwds, hits


def compute(
    corpus: Corpus,
    needles: dict[str, str],
    labelled: dict[tuple[str, str], set[str]],
) -> list[dict]:
    """Fence rows for every file F1 or F2 would remove (G1 applied)."""
    needles = {k: ws_norm(n) for k, n in needles.items()}
    rows = []
    for key, path in sorted(corpus.files.items()):
        unlisted = corpus.original(path) not in corpus.listed
        cwds, hits = scan(path, needles, check_f2=unlisted)
        if not (cwds or hits):
            continue
        reasons = (["F1"] if cwds else []) + (["F2"] if hits else [])
        detail = [f"F1:{c}" for c in sorted(cwds)]
        detail += [f"F2:{h}" for h in sorted(hits)]
        flagged = sorted(labelled.get(key, ()))
        rows.append(
            {
                "path": str(path.relative_to(corpus.mirror)),
                "provider": key[0],
                "logical_key": key[1],
                "reasons": reasons,
                "detail": ";".join(detail),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
                "action": "KEEP_G1" if flagged else "DELETE",
                "flagged_questions": flagged,
            }
        )
    return rows


def apply(mirror: Path, rows: list[dict]) -> int:
    """Delete DELETE rows from the mirror, after checking every hash."""
    doomed = [row for row in rows if row["action"] == "DELETE"]
    for row in doomed:
        if sha256_file(mirror / row["path"]) != row["sha256"]:
            raise ValueError(f"fenced clone changed: {row['path']}")
    for row in doomed:
        (mirror / row["path"]).unlink()
    return len(doomed)


def tsv(rows: list[dict]) -> str:
    lines = ["path\treasons\tdetail\tsha256\tbytes\taction\tflagged"]
    lines += [
        "\t".join(
            (
                row["path"],
                "+".join(row["reasons"]),
                row["detail"],
                row["sha256"],
                str(row["bytes"]),
                row["action"],
                ",".join(row["flagged_questions"]),
            )
        )
        for row in rows
    ]
    return "\n".join(lines) + "\n"
