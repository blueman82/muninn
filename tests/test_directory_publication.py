"""Release directory publication never replaces a winner."""

from __future__ import annotations

import unittest
from pathlib import Path

from install import release_io


class DirectoryDispatchTests(unittest.TestCase):
    """Preserve ordinary file defaults at the installation boundary."""

    def test_directory_replacement_refuses_before_effects(self) -> None:
        source, target = Path("/synthetic/source"), Path("/synthetic/target")
        with self.assertRaises(ValueError):
            release_io.publish(source, target, replace=True, directory=True)
