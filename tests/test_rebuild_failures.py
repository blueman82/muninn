"""``muninn rebuild`` when the old store cannot be read or replaced."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from muninn import cli_rebuild, store
from tests.cli_support import CliCase
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

    def test_a_loose_old_store_is_tightened_to_0600(self) -> None:
        self.db.chmod(0o644)
        _, out, _ = self.muninn("rebuild")
        aside = self.home / out["old_kept_as"]
        self.assertEqual(aside.stat().st_mode & 0o777, 0o600)

    def test_the_answer_warns_that_the_ledger_was_not_copied(self) -> None:
        code, out, _ = self.muninn("rebuild")
        self.assertEqual(code, 0)
        self.assertIn("not copied", out["warning"])
        self.assertNotIn("keep this prompt", str(out))
        _, again, _ = self.muninn("rebuild")  # now the old store is readable
        self.assertIsNone(again["warning"])

    def test_a_failed_replace_puts_the_old_store_back(self) -> None:
        real = Path.replace

        def flaky(path: Path, target: str | Path) -> Path:
            if path.name == cli_rebuild.REBUILD:
                raise PermissionError("denied")
            return real(path, target)

        with mock.patch.object(Path, "replace", flaky):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (4, "replace_failed"))
        self.assertIs(out["old_restored"], True)
        self.assertEqual(self.db.read_bytes(), JUNK)
        self.assertEqual(self.asides(), [])
        self.assertNotIn(cli_rebuild.REBUILD, self.names())

    def test_a_failed_put_back_is_reported_with_the_kept_name(self) -> None:
        real_replace, real_rename = Path.replace, Path.rename

        def no_replace(path: Path, target: str | Path) -> Path:
            if path.name == cli_rebuild.REBUILD:
                raise PermissionError("denied")
            return real_replace(path, target)

        def no_return(path: Path, target: str | Path) -> Path:
            if path.name.startswith(store.UNREADABLE_PREFIX):
                raise PermissionError("denied")
            return real_rename(path, target)

        with (
            mock.patch.object(Path, "replace", no_replace),
            mock.patch.object(Path, "rename", no_return),
        ):
            code, out, _ = self.muninn("rebuild")
        self.assertEqual((code, out["error"]), (4, "replace_failed"))
        self.assertIs(out["old_restored"], False)
        self.assertEqual((self.home / out["old_kept_as"]).read_bytes(), JUNK)

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
