"""Compatibility home of the CLI helpers other test modules import from here.

The tests themselves live in the ``test_cli_*`` modules and the shared
fixtures in ``tests/cli_support.py``.
"""

from __future__ import annotations

from pctx import cli
from tests.cli_support import CliCase
from tests.test_ingest import TID

__all__ = ["TID", "CliCase", "cli"]
