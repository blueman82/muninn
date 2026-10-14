"""Replay known questions against a store and report retrieval quality.

Reads QUESTIONS.json, ``{"questions": [{"id", "query", "required": [refs],
"control": bool}]}``, and a store opened read-only. Nothing is logged and
nothing is written under MUNINN_HOME. Output is JSON with ids, ranks and
counts only, never query text. A control passes only with 0 candidate hits;
that is strict, because FTS queries are OR queries.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from muninn import ingest, query, store
from muninn.query.ranked import SearchArgs, candidate_rows

REAL_CANDIDATES = 300  # muninn.query.hits.CANDIDATES
REAL_FINAL = 10  # the CLI search page limit default


class _BadInputError(Exception):
    """The questions file is not valid; its text is never echoed."""


def _load(path: Path) -> list[dict[str, Any]]:
    """Read and validate the questions file.

    Args:
        path: The JSON file.

    Returns:
        The question objects, each with the four keys present.

    Raises:
        _BadInputError: If the file or any question is malformed.
    """
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
        questions: list[dict[str, Any]] = document["questions"]
        if not questions:
            raise _BadInputError
        seen: set[str] = set()
        for item in questions:
            control = item.get("control", False)
            required: Any = item.get("required", [])
            if (
                set(item) - {"id", "query", "required", "control"}
                or not isinstance(item["id"], str)
                or item["id"] in seen
                or not isinstance(item["query"], str)
                or not isinstance(control, bool)
                or not all(isinstance(r, str) for r in required)
                or not isinstance(required, list)
                or (not required) != control
            ):
                raise _BadInputError
            item["control"], item["required"] = control, required
            seen.add(item["id"])
        return questions
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as bad:
        raise _BadInputError from bad


def _resolve(
    conn: sqlite3.Connection, ref: str, roots: dict[str, Path]
) -> tuple[int | None, bool]:
    """Open one cited ref: its event id and whether its line hash checks.

    Args:
        conn: Read-only store connection.
        ref: An event id or a ``provider:thread:line.part`` reference.
        roots: Provider roots used to re-read the source line.

    Returns:
        The event id (None if the ref does not resolve) and ``hash_ok``.
    """
    opened = query.open_event(conn, ref, roots=roots, context=0)
    if "error" in opened:
        return None, False
    return opened["id"], opened["hash_ok"] is True


def _replay(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    args: argparse.Namespace,
    roots: dict[str, Path],
) -> dict[str, Any]:
    """Score one question; the result carries ids, ranks and counts only."""
    base: SearchArgs = {"cwd": args.cwd or "/", "env": {}}
    if not args.cwd:
        base["all_projects"] = True
    fts = query.build_fts_query(item["query"])
    rows = candidate_rows(conn, fts, base, args.candidates).rows if fts else []
    ranked = [row["id"] for row in rows]
    if item["control"]:
        return {"id": item["id"], "control": True, "results": len(ranked)}
    final_args: SearchArgs = {**base, "limit": args.final}
    final = query.search(conn, item["query"], **final_args)
    final_ids = [hit["id"] for hit in final["hits"]]
    refs: list[dict[str, Any]] = []
    for ref in item["required"]:
        event_id, resolves = _resolve(conn, ref, roots)
        rank = ranked.index(event_id) + 1 if event_id in ranked else None
        refs.append(
            {
                "ref": ref,
                "rank": rank,
                "in_final": event_id in final_ids,
                "resolves": resolves,
            }
        )
    return {
        "id": item["id"],
        "control": False,
        "refs": refs,
        "all_in_candidates": all(r["rank"] for r in refs),
        "all_in_final": all(r["in_final"] for r in refs),
    }


def _rate(hit: int, total: int) -> float | None:
    """Return hit / total, or None when there is nothing to measure."""
    return round(hit / total, 4) if total else None


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the per-question results into the four headline numbers."""
    gold = [r for r in results if not r["control"]]
    controls = [r for r in results if r["control"]]
    refs = [ref for r in gold for ref in r["refs"]]
    return {
        "candidate_recall": _rate(
            sum(ref["rank"] is not None for ref in refs), len(refs)
        ),
        "complete_required_set_at_final": _rate(
            sum(r["all_in_final"] for r in gold), len(gold)
        ),
        "citation_resolves_rate": _rate(
            sum(ref["resolves"] for ref in refs), len(refs)
        ),
        "controls_passed": sum(r["results"] == 0 for r in controls),
        "controls_total": len(controls),
    }


def _parser() -> argparse.ArgumentParser:
    """Create the command parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("questions", type=Path)
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--cwd", default=None, help="default: all projects")
    parser.add_argument("--candidates", type=int, default=REAL_CANDIDATES)
    parser.add_argument("--final", type=int, default=REAL_FINAL)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Replay the questions and print the JSON report.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        0 on success, 2 for an invalid questions file or option, 4 when the
        store cannot be read.
    """
    args = _parser().parse_args(argv)
    if not 1 <= args.final <= 30 or args.candidates < 1:
        print("invalid option", file=sys.stderr)
        return 2
    try:
        questions = _load(args.questions)
    except _BadInputError:
        print("invalid questions file", file=sys.stderr)
        return 2
    home = args.home or store.data_home()
    try:
        roots = ingest.default_roots(os.environ)
        with contextlib.closing(store.connect_ro(store.db_path(home))) as conn:
            results = [_replay(conn, q, args, roots) for q in questions]
    except store.StoreUnavailableError:
        print("store unavailable", file=sys.stderr)
        return 4
    report = {
        "candidates": args.candidates,
        "final": args.final,
        "summary": _summary(results),
        "questions": results,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
