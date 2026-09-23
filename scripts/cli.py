"""Parse and dispatch provenance context command-line requests."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

BuildIndex = Callable[[Path, Path], int]
EvidencePacket = Callable[[Path, str, str | None, int], dict[str, object]]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse the command-line interface."""
    parser = argparse.ArgumentParser(
        description="Build and query a local, source-cited session evidence "
        "index."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--sessions-root", required=True, type=Path)
    build.add_argument("--db", required=True, type=Path)
    recall = commands.add_parser("recall")
    recall_group = recall.add_mutually_exclusive_group(required=True)
    recall_group.add_argument("--db", type=Path)
    recall_group.add_argument("--socket", type=Path)
    recall.add_argument("--prompt", required=True)
    recall.add_argument("--repo")
    recall.add_argument("--max-bytes", type=int, default=4000)
    serve = commands.add_parser("serve")
    serve.add_argument("--codex-root", required=True, type=Path)
    serve.add_argument("--claude-root", required=True, type=Path)
    serve.add_argument("--db", required=True, type=Path)
    serve.add_argument("--state-dir", required=True, type=Path)
    serve.add_argument("--socket", required=True, type=Path)
    serve.add_argument("--interval", type=float, default=1.0)
    for name in ("status", "doctor"):
        command = commands.add_parser(name)
        command.add_argument("--socket", required=True, type=Path)
    probe = commands.add_parser("checkpoint-probe")
    probe.add_argument("--socket", required=True, type=Path)
    erase = commands.add_parser("erase")
    erase.add_argument("--socket", required=True, type=Path)
    erase.add_argument(
        "--provider", required=True, choices=("codex", "claude")
    )
    erase.add_argument("--source-id", required=True)
    return parser.parse_args(argv)


def doctor_is_healthy(packet: Mapping[str, object]) -> bool:
    """Return whether a public status packet permits a zero doctor exit."""
    scanned = packet.get("last_scan")
    sources = packet.get("sources")
    if packet.get("available") is not True or not isinstance(scanned, float):
        return False
    if time.time() - scanned > 5 or packet.get("pending_sources"):
        return False
    if packet.get("last_error") or not isinstance(sources, Sequence):
        return False
    return not any(
        isinstance(source, Mapping)
        and (source.get("pending") or source.get("error"))
        for source in sources
    )


def _serve(args: argparse.Namespace) -> int:
    from service import serve

    if args.interval <= 0:
        raise ValueError("interval must be positive")
    return serve(
        args.codex_root,
        args.claude_root,
        args.db,
        args.state_dir,
        args.socket,
        args.interval,
    )


def _socket_request(args: argparse.Namespace) -> dict[str, object]:
    from runtime import request
    from service import MAX_REQUEST_BYTES, REQUEST_TIMEOUT_SECONDS

    payload: dict[str, object] = {"op": args.command}
    if args.command == "recall":
        payload |= {
            "prompt": args.prompt,
            "repo": args.repo,
            "max_bytes": args.max_bytes,
        }
    if args.command == "erase":
        payload |= {"provider": args.provider, "source_id": args.source_id}
    timeout = (
        0.5 if args.command == "checkpoint-probe" else REQUEST_TIMEOUT_SECONDS
    )
    return request(args.socket, payload, timeout, MAX_REQUEST_BYTES)


def main(
    build_index: BuildIndex,
    evidence_packet: EvidencePacket,
    argv: Sequence[str] | None = None,
) -> int:
    """Run the builder or evidence retrieval command."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "build":
            print(
                json.dumps(
                    {"indexed": build_index(args.sessions_root, args.db)}
                )
            )
            return 0
        if args.command == "recall" and args.db:
            print(
                json.dumps(
                    evidence_packet(
                        args.db, args.prompt, args.repo, args.max_bytes
                    )
                )
            )
            return 0
        if args.command == "serve":
            return _serve(args)
        packet = _socket_request(args)
        print(json.dumps(packet))
        return int(args.command == "doctor" and not doctor_is_healthy(packet))
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"context: {error}", file=sys.stderr)
        return 2
