"""Keep provider path overrides inside temporary installer homes."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


@contextmanager
def fixture_provider_env(home: Path) -> Generator[None]:
    """Bind default provider paths to a disposable installer fixture.

    Args:
        home: Temporary home owned by the current test.
    """
    with mock.patch.dict(
        os.environ,
        {
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "CODEX_HOME": str(home / ".codex"),
        },
    ):
        yield
