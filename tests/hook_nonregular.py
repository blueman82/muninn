"""Live FIFO fixtures for refusing transcript reads."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def nonregular(path: Path, data: bytes = b"{}\n") -> Generator[str]:
    """Provide a real pipe with readable data that must never be consumed.

    Args:
        path: POSIX FIFO path under a synthetic home.
        data: Available bytes, making an accidental pipe read observable.

    Yields:
        FIFO pathname; it cannot be a regular transcript.

    Raises:
        OSError: If fixture creation fails.
    """
    os.mkfifo(path)
    descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        os.write(descriptor, data)
        yield str(path)
    finally:
        os.close(descriptor)
