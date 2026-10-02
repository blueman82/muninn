"""The pctx command line: argument handling, dispatch and the JSON answer.

Every answer is one JSON object carrying notice, index_age_s, poller and
logged.  Exit codes: 0 ok, 2 refused or bad usage, 3 busy, 4 store
unavailable (doctor: 0 healthy, 1 not).  Readers never write the store; each
call appends one allowlisted line to calls.jsonl.  Text fields are redacted
again just before printing, so a line stored before a redaction rule existed
still never reaches the screen.

The handlers live in ``cli_read``, ``cli_know``, ``cli_maint``,
``cli_rebuild`` and ``cli_serve``; the parser in ``cli_parser``.  The
launcher and the launchd job call ``main``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from argparse import Namespace
from collections.abc import Sequence
from pathlib import Path

from pctx import (
    __version__,
    classify,
    cli_know,
    cli_maint,
    cli_read,
    cli_rebuild,
    cli_serve,
    obs,
    store,
)
from pctx.cli_core import Env, Handler, Out, Record, Result
from pctx.cli_hook import HOOKS, PROVIDERS, hook_main
from pctx.cli_output import redacted
from pctx.cli_parser import build_parser

__all__ = [
    "HOOKS",
    "NOTICE",
    "PROVIDERS",
    "main",
]

NOTICE = classify.NOTICE


_HANDLERS: dict[str, Handler] = {
    "search": cli_read.search,
    "open": cli_read.open_event,
    "sessions": cli_read.sessions,
    "session": cli_read.session,
    "quote-check": cli_read.quote_check,
    "erase": cli_maint.erase_command,
    "ingest": cli_maint.ingest_command,
    "serve": cli_serve.serve,
    "stats": cli_read.stats,
    "doctor": cli_read.doctor,
    "compact": cli_maint.compact,
    "rebuild": cli_rebuild.rebuild,
    "know": cli_know.know,
}


def _dispatch(args: Namespace, env: Env, home: Path, record: Record) -> Result:
    """Run the handler and map store failures to their exit codes."""
    try:
        return _HANDLERS[args.cmd](args, env, home, record)
    except store.BusyError:
        return 3, {"error": "busy"}
    except store.HotJournalError:
        return 4, {"error": "hot_journal"}
    except store.StoreUnavailableError:
        return 4, {"error": "store_unavailable"}
    except (ValueError, LookupError) as refused:  # e.g. erase arguments
        return 2, {"error": "refused", "reason": str(refused)[:200]}


def _answer(
    args: Namespace,
    env: Env,
    home: Path,
    result: tuple[int, Out],
    record: Record,
    started: float,
) -> int:
    """Redact, log and print one answer; return its exit code."""
    code, out = result
    out = redacted(out)
    out.setdefault("notice", NOTICE)
    if "poller" not in out:
        out |= obs.freshness(obs.read_status(home))
    # bytes_out is measured before "logged" is added: the log line cannot
    # describe itself.
    body = json.dumps(out, sort_keys=True)
    record |= {
        "exit": code,
        "bytes_out": len(body),
        "ms": round((time.monotonic() - started) * 1000, 1),
    }
    if "error" in out:
        record["error"] = out["error"]
    out["logged"] = obs.log_call(home, record, env)
    pretty = getattr(args, "pretty", False) or os.environ.get("PCTX_PRETTY")
    print(json.dumps(out, sort_keys=True, indent=2 if pretty else None))
    return code


def _run(args: Namespace, env: Env) -> int:
    """Run one parsed command and print its JSON answer."""
    home = store.data_home(env)
    started = time.monotonic()
    record: Record = {
        "cmd": args.cmd,
        "actor": obs.actor(env),
    }
    code, out = _dispatch(args, env, home, record)
    if out is None:  # serve prints nothing
        return code
    return _answer(args, env, home, (code, out), record, started)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the pctx command line.

    Args:
        argv: Arguments without the program name; defaults to ``sys.argv``.

    Returns:
        The process exit code.
    """
    given = sys.argv[1:] if argv is None else list(argv)
    if given[:1] == ["hook"] and not {"-h", "--help"} & set(given):
        # Handled before argparse: a usage error here would exit 2, and exit
        # 2 from a hook can block the user's prompt.
        return hook_main(given[1:], os.environ)
    pretty = "--pretty" in given  # anywhere on the line; hooks never use it
    given = [a for a in given if a != "--pretty"]
    parser = build_parser()
    try:
        args = parser.parse_args(given)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 2
    if args.cmd is None:
        if args.version:
            print(f"pctx {__version__}")
            return 0
        parser.print_usage(sys.stderr)
        return 2
    if args.version:
        parser.print_usage(sys.stderr)
        return 2
    args.pretty = pretty
    return _run(args, os.environ)
