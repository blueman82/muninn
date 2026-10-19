"""Keep native proof recorders transparent to product call arguments."""

from __future__ import annotations

import unittest
from unittest import mock

from install import lifecycle
from tests.lifecycle_native_records import record_writer_mismatch


class NativeRecorderTests(unittest.TestCase):
    """Check bounded recorder output without changing the product verdict."""

    def test_writer_recorder_forwards_selected_redirector(self) -> None:
        with mock.patch.object(
            lifecycle, "writer_mismatch", return_value=10
        ) as real:
            seen = record_writer_mismatch()
            self.assertEqual(
                lifecycle.writer_mismatch(
                    None,
                    {},
                    {},
                    "",
                    (None, None),
                    (),
                    redirector={"pid": 7},
                ),
                10,
            )
            real.assert_called_once_with(
                None, {}, {}, "", (None, None), (), redirector={"pid": 7}
            )
            self.assertEqual(seen, [10])
