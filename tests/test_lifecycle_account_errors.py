"""Pre-report ordinary probe failures export finite codes only."""

from __future__ import annotations

import unittest

from tests.lifecycle_account_errors import failure_codes


class AccountErrorTests(unittest.TestCase):
    """Captured bootstrap output never enters the native artifact."""

    def test_single_compiler_id_is_numeric_and_credentials_are_absent(
        self,
    ) -> None:
        raw = b"secret_password user/path error CS0103: unrecognized source\n"
        self.assertEqual(
            failure_codes(1, raw),
            {
                "outer_returncode": 1,
                "outer_compile_code": 103,
                "outer_parser_error": 0,
            },
        )

    def test_parser_category_and_unknown_output_are_distinct(self) -> None:
        self.assertEqual(
            failure_codes(1, b"ParserError: private source"),
            {
                "outer_returncode": 1,
                "outer_compile_code": 0,
                "outer_parser_error": 1,
            },
        )
        self.assertEqual(
            failure_codes(7, b"unknown credential text")["outer_compile_code"],
            0,
        )

    def test_oversized_or_ambiguous_errors_are_not_claimed_as_single_code(
        self,
    ) -> None:
        for raw in (
            b"error CS0103 error CS0246",
            b"x" * 16385 + b"error CS0103",
        ):
            with self.subTest(size=len(raw)):
                self.assertEqual(
                    failure_codes(1, raw)["outer_compile_code"], 0
                )
