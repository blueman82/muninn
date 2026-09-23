#!/usr/bin/env python3
"""Expose source-cited local session evidence to Claude Code."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_ROOT / "hooks"))

from codex import HARD_MAX_BYTES
from codex import main as hook_main
from context import evidence_packet
from service import request


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse Claude command and hook adapter arguments.

    Args:
        argv: Arguments excluding the program name.

    Returns:
        Parsed command arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hook", action="store_true")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--db", type=Path)
    source.add_argument("--socket", type=Path)
    parser.add_argument("--prompt")
    parser.add_argument("--repo")
    parser.add_argument("--max-bytes", type=int, default=1_800)
    args = parser.parse_args(argv)
    if not args.hook and (
        args.prompt is None or (args.db is None and args.socket is None)
    ):
        parser.error(
            "--socket and --prompt are required unless --hook is used"
        )
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Run direct recall or the shared hook adapter.

    Args:
        argv: Optional arguments excluding the program name.

    Returns:
        Zero on successful recall or hook handling.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.hook:
        return hook_main()
    try:
        max_bytes = min(max(args.max_bytes, 1), HARD_MAX_BYTES)
        if args.socket:
            packet = request(
                args.socket,
                {
                    "op": "recall",
                    "prompt": args.prompt,
                    "repo": args.repo,
                    "max_bytes": max_bytes,
                },
            )
        else:
            packet = evidence_packet(
                args.db, args.prompt, args.repo, max_bytes
            )
        print(json.dumps(packet))
    except (OSError, ValueError) as error:
        print(f"claude-context: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
