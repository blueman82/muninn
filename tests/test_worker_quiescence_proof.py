"""Accept only the specific historical containment assertion as native RED."""

from __future__ import annotations

import unittest

from tools import worker_quiescence_proof as proof


class NativeProofClassificationTests(unittest.TestCase):
    """Keep unrelated failures from masquerading as a reproduced defect."""

    def test_expected_red_requires_one_specific_assertion(self) -> None:
        output = (
            b"FAIL: test_delayed_signal_keeps_outcome_pending_before_reap "
            b"(tests.test_review_group_quiescence.GroupQuiescenceTests)\n"
            b"AssertionError: True is not false : "
            b"premature outcome before group quiescence\n"
            b"Ran 1 test in 0.2s\nFAILED (failures=1)\n"
        )
        self.assertTrue(proof.expected_red(1, output))
        self.assertFalse(proof.expected_red(0, output))
        self.assertFalse(proof.expected_red(1, b"ImportError: missing module"))
        self.assertFalse(
            proof.expected_red(1, output + b"ERROR: other case\n")
        )
        self.assertFalse(
            proof.expected_red(
                1,
                output.replace(
                    b"FAILED (failures=1)", b"FAILED (failures=1, errors=1)"
                ),
            )
        )
        self.assertFalse(
            proof.expected_red(
                1,
                output.replace(
                    b"premature outcome before group quiescence",
                    b"other assertion",
                ),
            )
        )
