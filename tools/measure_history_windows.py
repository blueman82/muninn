"""Report the cost of splitting history windows; reads only, chooses nothing.

Usage: python3.13 -m tools.measure_history_windows [--limit BYTES] PATH...

PATH is a JSONL session file or a folder of them. A file named
`rollout-*.jsonl` is a Codex session, any other is a Claude session. Each
file is copied once into a temporary folder and ingested into a throwaway
store there; the inputs and the real store are not touched. No model call
is made and only numbers are printed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from bisect import bisect_right
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import ingest, review_history, store
from muninn.review_activation import PROJECTION_VERSION
from muninn.review_models import ReviewPolicy

DEFAULT_LIMIT = 1_048_576
STATS = ("p50", "p75", "p90", "p95", "p99", "max")
_RANKS = (0.5, 0.75, 0.9, 0.95, 0.99, 1.0)
_DIGEST = "0" * 64


def _policy(limit: int) -> ReviewPolicy:
    """Build a data-only policy; only its read limit matters here."""
    return ReviewPolicy.model_validate(
        {
            "schema_version": 1,
            "policy_id": "measure",
            "prompt_digest": _DIGEST,
            "projection_version": PROJECTION_VERSION,
            "result_schema_digest": _DIGEST,
            "max_rounds": 1,
            "max_input_bytes": limit,
            "max_output_bytes": 1000,
            "context_bytes": 1000,
            "deadline_seconds": 60.0,
            "max_attempts": 1,
            "retry_backoff_seconds": [],
            "concurrency": 1,
            "hook_wait_seconds": 0.0,
            "allow_global": False,
        }
    )


def layout(path: Path, max_input_bytes: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Describe the lines, turns and windows of one session file.

    The bytes of `path` are read once and copied; the ingest and every
    measure use that copy, so a file that grows cannot skew the numbers.

    Args:
        path: A Codex (`rollout-*.jsonl`) or Claude session file.
        max_input_bytes: The read limit to plan windows for.

    Returns:
        Numbers only: line count, bytes per line, prompt lines and the
        windows with their cut, exclusion and limited flags.
    """
    data = path.read_bytes()
    sizes = [len(row) + 1 for row in data.split(b"\n")[:-1]]
    codex = path.name.startswith("rollout-")
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(os.path.realpath(name))
        roots = {
            "codex-sessions": tmp / "codex" / "sessions",
            "codex-archived": tmp / "codex" / "archived",
            "claude-projects": tmp / "claude" / "projects",
        }
        target = (
            roots["codex-sessions"] / "2026" / "01" / "02"
            if codex
            else roots["claude-projects"] / "project"
        )
        target.mkdir(parents=True)
        (target / path.name).write_bytes(data)
        mapped = {k: str(v) for k, v in roots.items()}
        env = {"MUNINN_ROOTS": json.dumps(mapped)}
        with mock.patch.dict(os.environ, env):
            conn = store.connect_rw(store.db_path(tmp / "home"), False)
            try:
                ingest.ingest(conn, roots, full=True)
                starts = [
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT line FROM event WHERE "
                        "kind='prompt' ORDER BY line"
                    )
                ]
                indexed = [
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT line FROM event ORDER BY line"
                    )
                ]
                policy = _policy(max_input_bytes)
                traced = review_history.trace(conn, policy, None)
            finally:
                conn.close()
    windows: list[dict[str, Any]] = []
    for planned, cause in traced:
        if cause not in (None, "evidence_limit"):
            continue
        window = planned.item.window
        windows.append(
            {
                "from": window.from_line,
                "through": window.through_line,
                "new_from": planned.new_from,
                "cut": planned.cut,
                "excluded": cause is not None,
                "limited": planned.limited,
            }
        )
    return {
        "lines": len(sizes),
        "line_bytes": sizes,
        "indexed": indexed,
        "turn_starts": starts,
        "windows": windows,
    }


def _percentiles(values: list[int]) -> dict[str, int]:
    """Return nearest-rank percentiles; empty input gives zeros."""
    ordered = sorted(values)
    if not ordered:
        return dict.fromkeys(STATS, 0)
    return {
        name: ordered[max(math.ceil(rank * len(ordered)), 1) - 1]
        for name, rank in zip(STATS, _RANKS, strict=True)
    }


def _turns(view: dict[str, Any]) -> tuple[list[int], list[int]]:
    """List the line count and bytes of each turn of one layout."""
    starts: list[int] = view["turn_starts"]
    sizes: list[int] = view["line_bytes"]
    ends = [*starts[1:], len(sizes) + 1]
    return (
        [b - a for a, b in zip(starts, ends, strict=True)],
        [sum(sizes[a - 1 : b - 1]) for a, b in zip(starts, ends, strict=True)],
    )


def _count(view: dict[str, Any], totals: dict[str, int]) -> None:
    """Add the window costs of one layout to the totals."""
    sizes: list[int] = view["line_bytes"]
    starts: list[int] = view["turn_starts"]
    for win in view["windows"]:
        totals["parts"] += 1
        if win["excluded"]:
            totals["excluded_evidence_limit"] += 1
        else:
            first, new, last = win["from"], win["new_from"], win["through"]
            totals["bytes_sent"] += sum(sizes[first - 1 : last])
            totals["bytes_new"] += sum(sizes[new - 1 : last])
        if not win["cut"]:
            continue
        totals["cuts"] += 1
        index = bisect_right(starts, win["new_from"] - 1)
        if index and win["from"] <= starts[index - 1]:
            totals["cuts_with_full_previous_turn"] += 1
        else:
            totals["cuts_shortened"] += 1


def _files(paths: Sequence[Path]) -> list[Path]:
    """Expand folders to their sorted `.jsonl` files."""
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(path.rglob("*.jsonl")))
        else:
            found.append(path)
    return found


def measure(
    paths: Sequence[Path], max_input_bytes: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """Measure turn sizes and the cost of splitting for session files.

    Args:
        paths: Session files or folders of them.
        max_input_bytes: The read limit to plan windows for.

    Returns:
        Numbers only: file count, turn statistics and window totals.
    """
    names = (
        "parts", "excluded_evidence_limit", "cuts",
        "cuts_with_full_previous_turn", "cuts_shortened", "bytes_new",
        "bytes_sent",
    )  # fmt: skip
    totals: dict[str, int] = dict.fromkeys(names, 0)
    lines: list[int] = []
    sizes: list[int] = []
    files = _files(paths)
    for path in files:
        view = layout(path, max_input_bytes)
        more_lines, more_bytes = _turns(view)
        lines.extend(more_lines)
        sizes.extend(more_bytes)
        _count(view, totals)
    return {
        "files": len(files),
        "turns": {
            "count": len(lines),
            "lines": _percentiles(lines),
            "bytes": _percentiles(sizes),
        },
        "windows": totals,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Print the measurement as JSON; return 0."""
    parser = argparse.ArgumentParser(prog="measure_history_windows")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(measure(args.paths, args.limit), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
