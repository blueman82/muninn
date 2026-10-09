"""``muninn rebuild`` when the old store cannot be read or replaced."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Generator
from pathlib import Path
from unittest import mock

from muninn import cli_rebuild, platform_windows, store
from tests.cli_support import CliCase
from tests.store_support import assert_private, public_read
from tests.test_ingest import TID

JUNK = b"not a database" * 100
STAMP = "20260101T000000Z"


class RebuildFailureTests(CliCase):
    """An unreadable old store is kept, and kept safely."""

    def setUp(self) -> None:
        super().setUp()
        self.session(TID, "keep this prompt", "kept")
        self.conn.close()
        self.db = store.db_path(self.home)
        self.db.write_bytes(JUNK)

    def names(self) -> list[str]:
        """Return the file names in the data directory."""
        return sorted(p.name for p in self.home.iterdir())

    def asides(self) -> list[str]:
        """Return the set-aside store names."""
        return [n for n in self.names() if store.UNREADABLE_PREFIX in n]

    def test_a_second_set_aside_in_one_second_gets_a_suffix(self) -> None:
        first = self.home / f"{store.UNREADABLE_PREFIX}{STAMP}"
        first.write_bytes(b"earlier")
        with mock.patch.object(
            cli_rebuild.time, "strftime", return_value=STAMP
        ):
            name = cli_rebuild._set_aside(self.home, self.db)
        self.assertEqual(name, f"{store.UNREADABLE_PREFIX}{STAMP}-2")
        self.assertEqual(first.read_bytes(), b"earlier")
        self.assertEqual((self.home / name).read_bytes(), JUNK)

    def test_a_loose_old_store_is_tightened_or_refused(self) -> None:
        if sys.platform == "win32":
            with store.writer_lock(self.home):
                pass
            self.muninn("stats")
            self.assertEqual(self.db.read_bytes(), JUNK)
            with public_read(self, self.db):
                before = self.names()
                code, out, _ = self.muninn("rebuild")
                self.assertEqual(
                    (code, out["error"]), (4, "store_unavailable")
                )
                self.assertEqual(self.db.read_bytes(), JUNK)
                self.assertEqual(self.names(), before)
        else:
            self.db.chmod(0o644)
            _, out, _ = self.muninn("rebuild")
            assert_private(self, self.home / out["old_kept_as"])

    @contextlib.contextmanager
    def refuse_native_publication(self, *, restore: bool) -> Generator[None]:
        """Refuse the new native move and optionally the restore move."""
        calls: list[str] = []
        if sys.platform == "win32":
            real_publish = platform_windows.publish

            def native(
                source: Path,
                target: Path,
                *,
                replace: bool,
                retry_move: bool = False,
                directory: bool = False,
            ) -> None:
                if target == self.db and replace:
                    if source.name == cli_rebuild.REBUILD:
                        calls.append("new")
                        raise PermissionError("denied")
                    if source.name.startswith(".restore-"):
                        calls.append("restore")
                        if restore:
                            raise PermissionError("denied")
                real_publish(
                    source,
                    target,
                    replace=replace,
                    retry_move=retry_move,
                    directory=directory,
                )

            with mock.patch.object(platform_windows, "publish", native):
                yield
            self.assertEqual(calls, ["new", "restore"])
        else:
            raise AssertionError("native publication fixture requires Windows")

    @contextlib.contextmanager
    def refuse_publication(self, *, restore: bool) -> Generator[None]:
        """Interrupt the active publication backend at its actual move."""
        if sys.platform == "win32":
            with self.refuse_native_publication(restore=restore):
                yield
            return
        calls: list[str] = []
        real_replace, real_rename = Path.replace, Path.rename

        def no_replace(path: Path, target: str | Path) -> Path:
            if path.name == cli_rebuild.REBUILD:
                calls.append("new")
                raise PermissionError("denied")
            return real_replace(path, target)

        def no_return(path: Path, target: str | Path) -> Path:
            if restore and path.name.startswith(store.UNREADABLE_PREFIX):
                calls.append("restore")
                raise PermissionError("denied")
            return real_rename(path, target)

        with (
            mock.patch.object(Path, "replace", no_replace),
            mock.patch.object(Path, "rename", no_return),
        ):
            yield
        self.assertEqual(calls, ["new", "restore"] if restore else ["new"])

    def assert_recovery_files(self, out: dict[str, object]) -> None:
        """Check the retained original and absence of publication scratch."""
        if sys.platform == "win32" or out["old_restored"] is True:
            self.assertEqual(self.db.read_bytes(), JUNK)
        else:
            self.assertFalse(self.db.exists())
        self.assertEqual(
            (self.home / "muninn.sqlite-unknown").read_bytes(), b"owner"
        )
        self.assertFalse(
            any(
                n.startswith((cli_rebuild.REBUILD, ".restore-", ".recovery-"))
                for n in self.names()
            )
        )
        if sys.platform == "win32" or out["old_restored"] is False:
            kept = out["old_kept_as"]
            self.assertIsInstance(kept, str)
            assert isinstance(kept, str)
            self.assertEqual(self.asides(), [kept])
            self.assertEqual((self.home / kept).read_bytes(), JUNK)
            assert_private(self, self.home / kept)
        else:
            self.assertEqual(self.asides(), [])

    def test_the_answer_warns_that_the_ledger_was_not_copied(self) -> None:
        code, out, _ = self.muninn("rebuild")
        self.assertEqual(code, 0)
        self.assertIn("not copied", out["warning"])
        self.assertNotIn("keep this prompt", str(out))
        _, again, _ = self.muninn("rebuild")  # now the old store is readable
        self.assertIsNone(again["warning"])

    def test_a_failed_replace_puts_the_old_store_back(self) -> None:
        (self.home / "muninn.sqlite-unknown").write_bytes(b"owner")
        with self.refuse_publication(restore=False):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (4, "replace_failed"))
        self.assertIs(out["old_restored"], True)
        self.assert_recovery_files(out)

    def test_a_failed_put_back_is_reported_with_the_kept_name(self) -> None:
        (self.home / "muninn.sqlite-unknown").write_bytes(b"owner")
        with self.refuse_publication(restore=True):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (4, "replace_failed"))
        self.assertIs(out["old_restored"], False)
        self.assert_recovery_files(out)

    def test_a_failed_quick_check_sets_nothing_aside(self) -> None:
        built = cli_rebuild._Built(False, {}, 0, mock.MagicMock(), "bad")
        with mock.patch.object(cli_rebuild, "_build", return_value=built):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (2, "quick_check_failed"))
        self.assertEqual(self.asides(), [])
        self.assertEqual(self.db.read_bytes(), JUNK)

    def test_a_hot_journal_leaves_no_half_built_store(self) -> None:
        (self.home / "muninn.sqlite-journal").write_bytes(b"\x00" * 512)
        code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (4, "hot_journal"))
        names = self.names()
        self.assertNotIn(cli_rebuild.REBUILD, names)
        self.assertNotIn(f"{cli_rebuild.REBUILD}-journal", names)
        self.assertEqual(self.asides(), [])
