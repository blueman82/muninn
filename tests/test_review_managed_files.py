"""Check fixed-manager package identity and finite storage boundaries."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest import mock, skipIf

from muninn import review_managed_files as files
from tests.ingest_support import IngestCase
from tests.managed_codex_support import package, selection


@skipIf(sys.platform == "win32", "requires POSIX ownership and file modes")
class ManagedFileTests(IngestCase):
    """Inspect disposable package trees without running native software."""

    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "manager"
        self.release = package(self.root)
        self.selected = selection(self.root)
        for patch in (
            mock.patch.object(files, "manager_root", return_value=self.root),
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
            mock.patch(
                "muninn.review_codex_launch._has_acl", return_value=False
            ),
            mock.patch(
                "muninn.review_codex_launch.verify_codex_executable",
                side_effect=self.signed,
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def signed(
        self, path: Path, *, limit: int, deadline: float
    ) -> tuple[Path, str]:
        """Model the external Apple verification, preserving real hashes."""
        self.assertEqual(limit, 384 * 1024 * 1024)
        return path, files.hash_file(path, limit, deadline)[0]

    def observe(self) -> files.RuntimeIdentity:
        """Run the real bounded resolver within a fresh finite deadline."""
        return files.observe(
            self.home, self.selected, deadline=time.monotonic() + 10
        )

    def test_current_update_changes_facts_not_selection(self) -> None:
        first = self.observe()
        package(self.root, "0.167.0")
        second = self.observe()
        self.assertNotEqual(first.package_sha256, second.package_sha256)
        self.assertNotEqual(first.capability_key, second.capability_key)
        self.assertEqual(first.requested.version, "0.166.0")
        self.assertEqual(second.requested.version, "0.167.0")
        self.assertEqual(self.selected, selection(self.root))

    def test_changed_same_version_invalidates_identity(self) -> None:
        first = self.observe()
        (self.release / "codex-path/rg").write_bytes(b"substituted helper")
        self.assertNotEqual(
            first.capability_key, self.observe().capability_key
        )
        self.assertFalse(files.matches(first))

    def test_external_selector_refuses(self) -> None:
        current = self.root / "current"
        current.unlink()
        current.symlink_to(self.home)
        with self.assertRaisesRegex(ValueError, "runtime_selection_unsafe"):
            self.observe()

    def test_unsafe_entries_refuse(self) -> None:
        path = self.release / "unsafe"
        for kind in ("mode", "hardlink", "symlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "hardlink":
                    path.hardlink_to(self.release / "bin/codex")
                elif kind == "symlink":
                    path.symlink_to(self.home)
                elif kind == "fifo":
                    if sys.platform == "win32":
                        continue
                    os.mkfifo(path)
                else:
                    path.write_bytes(b"unsafe")
                    path.chmod(0o666)
                with self.assertRaisesRegex(
                    ValueError, "runtime_selection_unsafe"
                ):
                    self.observe()
                path.unlink()

    def test_structural_limit_refuses_before_large_read(self) -> None:
        with (self.release / "oversized").open("wb") as stream:
            stream.truncate(384 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "runtime_structure_limit"):
            self.observe()

    def test_project_root_and_wrong_platform_refuse(self) -> None:
        with self.assertRaisesRegex(ValueError, "runtime_selection_unsafe"):
            files.observe(
                self.home, selection(self.home), deadline=time.monotonic() + 10
            )
        with (
            mock.patch("platform.system", return_value="Linux"),
            self.assertRaisesRegex(
                ValueError, "runtime_interface_unsupported"
            ),
        ):
            self.observe()

    def test_safe_new_metadata_field_does_not_pin_a_package_version(
        self,
    ) -> None:
        metadata = self.release / "codex-package.json"
        data = json.loads(metadata.read_text())
        data["buildDate"] = "future-compatible-metadata"
        metadata.write_text(json.dumps(data))
        self.assertEqual(self.observe().requested.version, "0.166.0")

    def test_directory_scan_stops_before_draining_unbounded_entries(
        self,
    ) -> None:
        consumed: list[int] = []

        def entries() -> Iterator[SimpleNamespace]:
            for number in range(10000):
                consumed.append(number)
                yield SimpleNamespace(
                    path=str(self.release / f"entry-{number}")
                )

        scanning = mock.MagicMock()
        scanning.__enter__.return_value = entries()
        with (
            mock.patch("os.scandir", return_value=scanning),
            self.assertRaisesRegex(ValueError, "runtime_structure_limit"),
        ):
            self.observe()
        self.assertLessEqual(len(consumed), 257)

    def test_nested_selector_alias_refuses(self) -> None:
        alias = self.root / "releases/alias"
        alias.symlink_to(self.release)
        current = self.root / "current"
        current.unlink()
        current.symlink_to(alias)
        with self.assertRaisesRegex(ValueError, "runtime_selection_unsafe"):
            self.observe()

    def test_safe_internal_directory_link_does_not_pin_old_layout(
        self,
    ) -> None:
        (self.release / "resource-alias").symlink_to("codex-resources")
        self.assertEqual(self.observe().requested.version, "0.166.0")

    def test_internal_cycle_returns_a_finite_safe_cause(self) -> None:
        (self.release / "cycle-a").symlink_to("cycle-b")
        (self.release / "cycle-b").symlink_to("cycle-a")
        with self.assertRaisesRegex(ValueError, "runtime_selection_unsafe"):
            self.observe()

    def test_revalidation_refuses_a_new_alias_above_the_release(self) -> None:
        observed = self.observe()
        (self.root / "releases").rename(self.root / "moved")
        (self.root / "releases").symlink_to(self.root / "moved")
        self.assertFalse(files.matches(observed))

    def test_hashing_rejects_a_different_opened_inode(self) -> None:
        actual = files.open_regular
        selected = self.release / "bin/codex"
        other = self.home / "other-file"
        other.write_bytes(b"x" * selected.stat().st_size)
        with (
            mock.patch.object(
                files, "open_regular", side_effect=lambda _: actual(other)
            ),
            self.assertRaisesRegex(
                ValueError, "runtime_selection_unavailable"
            ),
        ):
            files.hash_file(selected, 384 * 1024 * 1024, time.monotonic() + 10)


class ManagedPlatformTests(IngestCase):
    """Refuse unsupported native ownership checks without POSIX calls."""

    def test_windows_refuses_before_posix_owner_lookup(self) -> None:
        path = self.home / "native-owner-check"
        path.write_bytes(b"synthetic")
        info = files.stamp(path)
        with (
            mock.patch("sys.platform", "win32"),
            mock.patch.object(
                os,
                "getuid",
                create=True,
                side_effect=AssertionError("unexpected POSIX owner lookup"),
            ),
            self.assertRaisesRegex(
                ValueError, "runtime_interface_unsupported"
            ),
        ):
            files._safe(path, info)
