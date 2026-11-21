"""Check learn on and learn off against a real store."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from muninn import review_settings, store
from muninn.cli_learn_switch import switch


class SwitchTests(unittest.TestCase):
    """The file and the control row change together."""

    def activated(self, home: Path) -> float | None:
        """Return when learning first began."""
        with closing(store.connect_ro(store.db_path(home))) as conn:
            value: float | None = conn.execute(
                "SELECT activated_at FROM review_control"
            ).fetchone()[0]
        return value

    def test_on_then_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with store.writer_lock(home, wait_s=0):
                store.connect_rw(store.db_path(home)).close()
            self.assertEqual(switch(home, True, 5.0), {"enabled": True})
            self.assertTrue(review_settings.load(home).enabled)
            self.assertEqual(self.activated(home), 5.0)
            switch(home, False, 6.0)
            self.assertFalse(review_settings.load(home).enabled)
            self.assertEqual(self.activated(home), 5.0)
            switch(home, True, 7.0)
            self.assertEqual(self.activated(home), 5.0)


if __name__ == "__main__":
    unittest.main()
