#!/opt/homebrew/bin/python3.13
"""Run the provider-neutral hook adapter from the Codex package."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "claude-code" / "scripts")
)

from hook_core import main

if __name__ == "__main__":
    raise SystemExit(main())
