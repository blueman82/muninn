"""Replay known questions against a store and report retrieval quality.

Reads QUESTIONS.json, ``{"questions": [{"id", "query", "required": [refs],
"control": bool}]}``, and a store opened read-only. Nothing is logged and
nothing is written under MUNINN_HOME. Output is JSON with ids, ranks and
counts only, never query text. A control passes only with 0 candidate hits
and some FTS terms; that is strict, because FTS queries are OR queries. A
query with no terms is flagged ``no_terms``. ``in_final`` reflects
the real selector, including exact-repeat collapse and the per-session cap.

Scope is explicit so ranks are reproducible: all projects by default,
``--no-all-projects`` scopes to ``--cwd`` (default: the current directory)
and ``--scope`` restricts to events whose cwd equals it, as in ``muninn
search``. The same arguments feed the candidate rows and the final search.
No current session is passed and the environment is empty, so the caller's
own session is never excluded.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, cast

from muninn import ingest, query, store
from muninn.query.answers import BadArgumentError
from muninn.query.hits import CANDIDATES, PER_SESSION
from muninn.query.ranked import SearchArgs, candidate_rows

REAL_FINAL = 10  # the CLI search page limit default


class _BadInputError(Exception):
    """The questions file is not valid; its text is never echoed."""


def _refs_ok(value: object) -> bool:
    """Say whether value is a list of distinct strings."""
    if not isinstance(value, list):
        return False
    refs = cast("list[object]", value)
    return all(isinstance(r, str) for r in refs) and len(set(refs)) == len(
        refs
    )


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
                or not _refs_ok(required)
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


def _diversity(hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Count sessions and bytes of the final hits, as search reports them."""
    sessions = Counter(hit["session"] for hit in hits)
    return {
        "distinct_sessions": len(sessions),
        "max_session_share": _rate(
            max(sessions.values(), default=0), len(hits)
        ),
        "final_bytes": len(json.dumps(hits).encode()),
    }


def _replay(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    args: argparse.Namespace,
    roots: dict[str, Path],
) -> dict[str, Any]:
    """Score one question; the result carries ids, ranks and counts only."""
    base: SearchArgs = {
        "cwd": args.cwd or str(Path.cwd()),
        "env": {},
        "all_projects": args.all_projects,
        "scope": args.scope,
    }
    fts = query.build_fts_query(item["query"])
    rows = candidate_rows(conn, fts, base, args.candidates).rows if fts else []
    ranked = [row["id"] for row in rows]
    no_terms = fts is None
    final_args: SearchArgs = {
        **base,
        "limit": args.final,
        "per_session_cap": args.per_session,
    }
    final = query.search(conn, item["query"], **final_args)
    if "error" in final:
        raise BadArgumentError(str(final["error"]))
    final_ids = [hit["id"] for hit in final["hits"]]
    if item["control"]:
        return {
            "id": item["id"],
            "control": True,
            "no_terms": no_terms,
            "results": len(ranked),
            "final_hits": len(final_ids),
        }
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
        "no_terms": no_terms,
        "refs": refs,
        "all_in_candidates": all(r["rank"] is not None for r in refs),
        "all_in_final": all(r["in_final"] for r in refs),
        "final_hits": len(final_ids),
        **_diversity(final["hits"]),
    }


def _rate(hit: int, total: int) -> float | None:
    """Return hit / total, or None when there is nothing to measure."""
    return round(hit / total, 4) if total else None


def _mean(values: list[float]) -> float | None:
    """Return the mean rounded to 4 places, or None for no values."""
    return round(sum(values) / len(values), 4) if values else None


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the per-question results into the headline numbers.

    The diversity and cost means share one set: gold questions whose final
    list is not empty.

    Args:
        results: One result per question, from ``_replay``.

    Returns:
        The headline numbers, counts and ratios only.
    """
    gold = [r for r in results if not r["control"]]
    shown = [r for r in gold if r["final_hits"]]
    controls = [r for r in results if r["control"]]
    refs = [ref for r in gold for ref in r["refs"]]
    return {
        "questions_with_final_hits": len(shown),
        **{
            f"{key}_mean": _mean([r[key] for r in shown])
            for key in (
                "distinct_sessions",
                "max_session_share",
                "final_bytes",
            )
        },
        "candidate_recall": _rate(
            sum(ref["rank"] is not None for ref in refs), len(refs)
        ),
        "complete_required_set_at_final": _rate(
            sum(r["all_in_final"] for r in gold), len(gold)
        ),
        "citation_resolves_rate": _rate(
            sum(ref["resolves"] for ref in refs), len(refs)
        ),
        "controls_passed": sum(
            r["results"] == 0 and not r["no_terms"] for r in controls
        ),
        "controls_total": len(controls),
        "unresolved_refs": sum(not ref["resolves"] for ref in refs),
        "no_terms_questions": sum(r["no_terms"] for r in results),
    }


def _parser() -> argparse.ArgumentParser:
    """Create the command parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("questions", type=Path)
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument(
        "--all-projects",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="search every project (default); --no-all-projects scopes",
    )
    parser.add_argument(
        "--scope", help="only events whose cwd is exactly this"
    )
    parser.add_argument("--cwd", help="scope root when --no-all-projects")
    parser.add_argument("--candidates", type=int, default=CANDIDATES)
    parser.add_argument("--final", type=int, default=REAL_FINAL)
    parser.add_argument(
        "--per-session",
        type=int,
        default=PER_SESSION,
        help="hits one session may keep in the final list (>= 1)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Replay the questions and print the JSON report.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        0 whenever a report is printed (report-only: misses never change the
        exit code), 2 for an invalid questions file, option or scope, 4 when
        the store cannot be read.
    """
    args = _parser().parse_args(argv)
    for name, ok in (
        ("final", 1 <= args.final <= 30),
        ("candidates", args.candidates >= 1),
        ("per-session", args.per_session >= 1),
    ):
        if not ok:
            print(f"invalid option: {name}", file=sys.stderr)
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
    except BadArgumentError as bad:
        print(f"invalid option: {bad}", file=sys.stderr)
        return 2
    except (store.StoreUnavailableError, sqlite3.Error):
        print("store unavailable", file=sys.stderr)
        return 4
    report = {
        "candidates": args.candidates,
        "final": args.final,
        "per_session": args.per_session,
        "summary": _summary(results),
        "questions": results,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
