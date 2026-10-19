"""Exercise explicit private launch artifacts with disposable storage."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import review_launch_inputs
from muninn.review_progress import digest
from tests import test_review_runner as runner_tests


class ReviewLaunchInputTests(unittest.TestCase):
    """Reject implicit selection, untrusted storage and artifact drift."""

    def setUp(self) -> None:
        self.case = runner_tests.ReviewRunnerTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.home = self.case.home
        self.policy = self.case.context.policy
        self.selected = self.case.proofs / "selected"
        self.selected.mkdir(mode=0o700)
        self.prompts = self.home / "review-prompts"
        self.prompts.mkdir(mode=0o700)

    def write(self, path: Path, raw: bytes) -> None:
        """Simulate controlled publication solely in disposable storage."""
        path.write_bytes(raw)
        path.chmod(0o600)

    def prompt(self, raw: bytes | None = None) -> bytes:
        """Publish explicit fake instructions with an exact artifact digest."""
        if raw is None:
            raw = (
                b'{"schema_version":1,"assembly":"muninn-review-prompt-v1",'
                b'"instructions":"Review the data."}'
            )
        self.policy = self.policy.model_copy(
            update={"prompt_digest": hashlib.sha256(raw).hexdigest()}
        )
        self.write(self.prompts / (self.policy.prompt_digest + ".json"), raw)
        return raw

    def test_exact_selection_and_prompt_bytes(self) -> None:
        raw = self.prompt()
        self.write(
            self.selected / (digest(self.policy) + ".json"),
            self.case.capability.model_dump_json().encode(),
        )
        self.assertEqual(
            review_launch_inputs.load_capability(self.home, self.policy),
            self.case.capability,
        )
        self.assertEqual(
            review_launch_inputs.load_prompt(self.home, self.policy), raw
        )

    def test_missing_selection_never_scans_or_defaults(self) -> None:
        self.write(
            self.selected / "latest.json",
            self.case.capability.model_dump_json().encode(),
        )
        with self.assertRaisesRegex(ValueError, "^launch_input_unavailable$"):
            review_launch_inputs.load_capability(self.home, self.policy)
        with self.assertRaises(ValueError):
            review_launch_inputs.load_prompt(self.home, self.policy)

    def test_prompt_strictness_digest_size_and_home_binding(self) -> None:
        for raw in (
            b"{}",
            b'{"schema_version":1,"assembly":"wrong","instructions":"x"}',
            b'{"schema_version":1,"assembly":"muninn-review-prompt-v1","instructions":"x","extra":1}',
            b'{"x":1,"x":2}',
            b"\xff",
            b"1" * 5000,
        ):
            self.prompt(raw)
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                review_launch_inputs.load_prompt(self.home, self.policy)
        self.prompt()
        self.write(self.prompts / (self.policy.prompt_digest + ".json"), b"{}")
        with self.assertRaises(ValueError):
            review_launch_inputs.load_prompt(self.home, self.policy)
        raw = self.prompt()
        with self.assertRaises(ValueError):
            review_launch_inputs.load_prompt(
                self.home,
                self.policy.model_copy(
                    update={"max_input_bytes": len(raw) - 1}
                ),
            )
        with (
            mock.patch.dict(
                os.environ, {"MUNINN_HOME": str(self.home / "different")}
            ),
            self.assertRaises(ValueError),
        ):
            review_launch_inputs.load_prompt(self.home, self.policy)

    def test_private_paths_and_changed_reads_refuse(self) -> None:
        self.prompt()
        path = self.prompts / (self.policy.prompt_digest + ".json")
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            review_launch_inputs.load_prompt(self.home, self.policy)
        path.chmod(0o600)
        saved = self.home / "saved"
        path.rename(saved)
        path.symlink_to(saved)
        with self.assertRaises(ValueError):
            review_launch_inputs.load_prompt(self.home, self.policy)
        path.unlink()
        saved.rename(path)
        original = os.fstat
        calls = 0

        def changed(fd: int) -> os.stat_result:
            nonlocal calls
            calls += 1
            if calls == 2:
                path.write_bytes(b"changed")
            return original(fd)

        with (
            mock.patch("os.fstat", side_effect=changed),
            self.assertRaises(ValueError),
        ):
            review_launch_inputs.load_prompt(self.home, self.policy)

    def test_selection_is_strict_bounded_and_preserves_unverified_status(
        self,
    ) -> None:
        path = self.selected / (digest(self.policy) + ".json")
        for raw in (
            b"{}",
            b'{"x":1,"x":2}',
            b"[" * 5000 + b"0" + b"]" * 5000,
            b"0" * (1024 * 1024 + 1),
            self.case.capability.model_dump_json()
            .replace('"verified"', '"guessed"')
            .encode(),
        ):
            self.write(path, raw)
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                review_launch_inputs.load_capability(self.home, self.policy)
        data = self.case.capability.model_dump(mode="json")
        for change in ({"extra": True}, {"model": "other"}, {"proof_path": 4}):
            self.write(path, json.dumps({**data, **change}).encode())
            with self.subTest(change=change), self.assertRaises(ValueError):
                review_launch_inputs.load_capability(self.home, self.policy)
        self.write(path, json.dumps({**data, "status": "unverified"}).encode())
        self.assertEqual(
            review_launch_inputs.load_capability(
                self.home, self.policy
            ).status,
            "unverified",
        )
        self.selected.chmod(0o755)
        with self.assertRaises(ValueError):
            review_launch_inputs.load_capability(self.home, self.policy)

    def test_platform_ownership_and_path_replacement_refuse(self) -> None:
        if sys.platform == "win32":
            self.skipTest("requires POSIX ownership or process APIs")
        self.prompt()
        path = self.prompts / (self.policy.prompt_digest + ".json")
        with mock.patch("os.name", "nt"), self.assertRaises(ValueError):
            review_launch_inputs.load_prompt(self.home, self.policy)
        with (
            mock.patch("os.getuid", return_value=os.getuid() + 1),
            self.assertRaises(ValueError),
        ):
            review_launch_inputs.load_prompt(self.home, self.policy)
        original = os.fstat
        calls = 0

        def replaced(fd: int) -> os.stat_result:
            nonlocal calls
            calls += 1
            if calls == 2:
                path.rename(self.home / "old")
                self.write(path, (self.home / "old").read_bytes())
            return original(fd)

        with (
            mock.patch("os.fstat", side_effect=replaced),
            self.assertRaises(ValueError),
        ):
            review_launch_inputs.load_prompt(self.home, self.policy)

    def test_prompt_required_literal_fields_refuse(self) -> None:
        data = {
            "schema_version": 1,
            "assembly": "muninn-review-prompt-v1",
            "instructions": "trusted",
        }
        for changes in (
            {"schema_version": True},
            {"instructions": "  "},
            {"instructions": None},
            {"assembly": None},
        ):
            self.prompt(json.dumps({**data, **changes}).encode())
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                review_launch_inputs.load_prompt(self.home, self.policy)

    def test_invalid_required_wrapper_bounds_refuse(self) -> None:
        for field in ("max_input_bytes", "max_output_bytes"):
            changed = self.case.context.model_copy(
                update={
                    "policy": self.case.context.policy.model_copy(
                        update={field: 0}
                    )
                }
            )
            with self.subTest(field=field), self.assertRaises(ValueError):
                runner_tests.review_runner.prepare_launch(
                    changed, self.case.capability, b"x"
                )

    def test_growing_prompt_bounds_actual_raw_fetch_before_refusal(
        self,
    ) -> None:
        raw = self.prompt().ljust(100, b" ")
        self.prompt(raw)
        self.policy = self.policy.model_copy(update={"max_input_bytes": 100})
        path = self.prompts / (self.policy.prompt_digest + ".json")
        fetched: list[int] = []
        requests: list[int] = []

        class CountedFile(io.FileIO):
            """Count bytes returned by real raw reads below any buffering."""

            def read(self, size: int | None = -1) -> bytes:
                """Observe direct unbuffered reads without changing data."""
                result = super().read(size)
                assert result is not None
                requests.append(size if size is not None else -1)
                fetched.append(len(result))
                return result

            def readinto(self, buffer: Any) -> int | None:
                """Observe raw reads requested by an optional buffer."""
                result = super().readinto(buffer)
                requests.append(len(buffer))
                fetched.append(result or 0)
                return result

        def counted_open(
            fd: int, mode: str, buffering: int = -1
        ) -> io.FileIO | io.BufferedReader:
            raw_stream = CountedFile(fd, mode, closefd=True)
            if buffering == 0:
                return raw_stream
            return io.BufferedReader(raw_stream)

        original = os.fstat
        calls = 0

        def grow(fd: int) -> os.stat_result:
            nonlocal calls
            observed = original(fd)
            calls += 1
            if calls == 1:
                with path.open("ab") as stream:
                    stream.write(b"x" * 10000)
            return observed

        with (
            mock.patch("os.fdopen", side_effect=counted_open),
            mock.patch("os.fstat", side_effect=grow),
            self.assertRaisesRegex(ValueError, "^capability_changed$"),
        ):
            review_launch_inputs.load_prompt(self.home, self.policy)
        self.assertGreater(sum(fetched), 0)
        self.assertLessEqual(sum(fetched), 100)
        self.assertTrue(all(0 <= request <= 100 for request in requests))
