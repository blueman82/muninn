"""Reject POSIX-only review operations before accessing Windows state."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muninn import knowledge_erase, review_launch_inputs, review_runner


class ReviewPlatformBoundaryTests(unittest.TestCase):
    """Keep unsupported native primitives behind explicit platform checks."""

    def test_windows_proof_helpers_refuse_before_filesystem_access(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "proof"
            path.write_bytes(b"private")
            path.chmod(0o600)
            operations = (
                lambda: review_runner.check_private_path(
                    path, directory=False
                ),
                lambda: review_runner.read_private_file(path, 100),
            )
            for operation in operations:
                with (
                    self.subTest(operation=operation),
                    mock.patch("sys.platform", "win32"),
                    self.assertRaisesRegex(
                        ValueError, "^capability_platform$"
                    ),
                ):
                    operation()
            self.assertEqual(path.read_bytes(), b"private")

    def test_file_digest_uses_portable_regular_file_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "file"
            path.write_bytes(b"synthetic")
            expected = (hashlib.sha256(b"synthetic").hexdigest(), 9)
            self.assertEqual(review_runner.file_digest(path, 100), expected)
            with (
                mock.patch("sys.platform", "win32"),
                mock.patch.object(
                    review_runner,
                    "open_regular",
                    side_effect=lambda target: target.open("rb"),
                    create=True,
                ),
            ):
                self.assertEqual(
                    review_runner.file_digest(path, 100), expected
                )

    def test_windows_launch_storage_refuses_before_file_access(self) -> None:
        root = Path("missing")
        with (
            mock.patch("os.name", "nt"),
            mock.patch.object(
                review_launch_inputs, "read_private_file"
            ) as read,
            self.assertRaisesRegex(ValueError, "^launch_input_platform$"),
        ):
            review_launch_inputs._load(root, root / "prompt", 100)
        read.assert_not_called()

    def test_windows_posix_erasure_helpers_refuse_before_access(self) -> None:
        path = Path("missing")
        operations = (
            lambda: knowledge_erase._unlink_children(999999),
            lambda: knowledge_erase._remove_attempt_posix(
                path, "A_" + "a" * 32
            ),
        )
        for operation in operations:
            with (
                self.subTest(operation=operation),
                mock.patch("sys.platform", "win32"),
                self.assertRaisesRegex(OSError, "^attempt_erasure_platform$"),
            ):
                operation()

    def test_reported_settings_and_usage_unknown_remain_null(self) -> None:
        observed, usage = review_runner.reported_metadata({}, None)
        self.assertEqual(
            observed.model_dump(),
            {
                "runner": None,
                "version": None,
                "model": None,
                "reasoning": None,
            },
        )
        self.assertIsNone(usage)
        observed, usage = review_runner.reported_metadata(
            {"model": "observed-model"}, {"input_tokens": 2}
        )
        self.assertEqual(observed.model, "observed-model")
        self.assertIsNone(observed.reasoning)
        assert usage is not None
        self.assertEqual(usage.input_tokens, 2)
        self.assertIsNone(usage.cost_decimal)
        self.assertIsNone(usage.output_tokens)
        with self.assertRaises(ValueError):
            review_runner.reported_metadata({}, {"input_tokens": -1})
