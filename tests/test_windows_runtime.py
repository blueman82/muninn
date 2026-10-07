"""Native Windows store, key, recovery and CLI safety on synthetic state."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from muninn import (
    erase,
    platform_io,
    platform_rebuild,
    platform_windows,
    store,
    tombstone_key,
)
from muninn.file_sync import sync_fd
from tests import test_platform_windows
from tests.cli_support import CliCase, fake_run
from tests.test_ingest import ROOT, TID

if sys.platform == "win32":

    class NativeStoreTests(test_platform_windows.WindowsFileTests):
        """Exercise native handles, sidecars and concurrent key creators."""

        def test_broad_journal_is_refused_without_changing_any_bytes(
            self,
        ) -> None:
            db = store.db_path(self.home)
            store.connect_rw(db).close()
            before = db.read_bytes()
            journal = self.write(db.name + "-journal", b"original journal")
            self.grant_everyone(journal)
            with self.assertRaises(PermissionError):
                store.connect_rw(db)
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(journal.read_bytes(), b"original journal")

        def test_empty_locked_file_has_no_residue_without_reading(
            self,
        ) -> None:
            self.write("content", b"synthetic retained canary")
            with store.writer_lock(self.home, wait_s=0):
                lock = self.home / "writer.lock"
                self.assertEqual(lock.stat().st_size, 0)
                with (
                    platform_io.open_regular(lock) as handle,
                    self.assertRaises(PermissionError),
                ):
                    handle.read()
                self.assertEqual(
                    erase.residue_scan(self.home, [b"canary"]), ["content"]
                )

        def test_append_only_private_descriptor_can_sync(self) -> None:
            path = self.home / "tombstones.jsonl"
            fd = platform_io.open_private(
                path, os.O_CREAT | os.O_WRONLY | os.O_APPEND
            )
            try:
                os.write(fd, b"synthetic\n")
                sync_fd(fd)
            finally:
                os.close(fd)
            self.assertEqual(path.read_bytes(), b"synthetic\n")

        def test_new_sqlite_journal_inherits_private_acl(self) -> None:
            db = store.db_path(self.home)
            conn = store.connect_rw(db)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO scope(key,label,kind) VALUES ('x','x','dir')"
                )
                platform_windows.assert_private(
                    db.with_name(db.name + "-journal")
                )
                conn.execute("ROLLBACK")
            finally:
                conn.close()

        def test_real_parallel_key_creators_adopt_one_complete_winner(
            self,
        ) -> None:
            code = (
                "from pathlib import Path;"
                "from muninn.tombstone_key import load_key;"
                "import sys; print(load_key(Path(sys.argv[1])).hex())"
            )
            children = [
                subprocess.Popen(
                    [sys.executable, "-c", code, str(self.home)],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(8)
            ]
            try:
                outputs = [child.communicate(timeout=30) for child in children]
                for child, (_, err) in zip(children, outputs, strict=True):
                    self.assertEqual(child.returncode, 0, err)
                keys = {out.strip() for out, _ in outputs}
                self.assertEqual(len(keys), 1)
                key = tombstone_key.load_key(self.home, create=False)
                self.assertEqual(keys, {key.hex().encode()})
                self.assertEqual(len(key), 32)
                self.assertEqual(
                    [p.name for p in self.home.iterdir()], ["tombstone.key"]
                )
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=10)

        def test_recovery_copy_survives_failure_after_publication(
            self,
        ) -> None:
            db, new = self.write("muninn.sqlite", b"old ledger"), self.write(
                "new", b"new"
            )
            real = platform_windows.publish

            def fail_after_move(
                source: Path, target: Path, *, replace: bool
            ) -> None:
                real(source, target, replace=replace)
                if source == new:
                    raise OSError("injected post-publication failure")

            with mock.patch.object(
                platform_windows, "publish", fail_after_move
            ):
                success, kept, restored = platform_rebuild.publish_store(
                    self.home, db, new, readable=True
                )
            self.assertFalse(success)
            self.assertTrue(restored)
            self.assertIsNotNone(kept)
            self.assertEqual(db.read_bytes(), b"old ledger")
            self.assertEqual(
                (self.home / str(kept)).read_bytes(), b"old ledger"
            )

        def test_failed_restoration_retains_durable_prior_copy(self) -> None:
            db, new = self.write("muninn.sqlite", b"old ledger"), self.write(
                "new", b"new"
            )
            real = platform_windows.publish

            def fail_target(
                source: Path, target: Path, *, replace: bool
            ) -> None:
                if target == db:
                    raise OSError("injected sharing failure")
                real(source, target, replace=replace)

            with mock.patch.object(platform_windows, "publish", fail_target):
                success, kept, restored = platform_rebuild.publish_store(
                    self.home, db, new, readable=True
                )
            self.assertFalse(success)
            self.assertFalse(restored)
            self.assertEqual(db.read_bytes(), b"old ledger")
            self.assertEqual(
                (self.home / str(kept)).read_bytes(), b"old ledger"
            )
            platform_windows.assert_private(self.home / str(kept))


class PortableCliTests(CliCase):
    """Exercise CLI commands using the real native store."""

    def test_crlf_unicode_erase_and_rebuild_preserve_raw_identity(
        self,
    ) -> None:
        text = 'nativecanary café 雪 & quoted "answer"'
        source = self.session(TID, text, "native reply")
        quote = "ledger evidence must survive replacement"
        self.session("thr-ledger", quote, "noted")
        source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
        original = source.read_bytes()
        self.conn.close()
        code, out, err = self.muninn("ingest")
        self.assertEqual((code, err), (0, ""), out)
        code, out, err = self.muninn("search", "nativecanary")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(
            out["hits"][0]["snippet"].replace("«", "").replace("»", ""), text
        )
        code, out, err = self.muninn(
            "know",
            "add",
            "--kind",
            "decision",
            "--text",
            "retain ledger",
            "--cite",
            "codex:thr-ledger:2.1",
            "--quote",
            quote,
        )
        self.assertEqual((code, err), (0, ""), out)
        code, out, err = self.muninn("open", f"codex:{TID}:2.1", "--raw")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out["text"], text)
        self.assertTrue(out["hash_ok"], out)
        code, out, err = self.muninn(
            "erase", "--event", f"codex:{TID}:2.1", "--yes"
        )
        self.assertEqual((code, err), (0, ""), out)
        key = tombstone_key.load_key(self.home, create=False)
        code, out, err = self.muninn("rebuild")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(tombstone_key.load_key(self.home, create=False), key)
        code, out, err = self.muninn("search", "nativecanary")
        self.assertEqual((code, err), (0, ""), out)
        self.assertNotIn(text, str(out))
        self.assertEqual(source.read_bytes(), original)
        with mock.patch("muninn.obs.run", fake_run()):
            code, out, err = self.muninn("doctor")
        self.assertEqual(
            (code, err), (1 if sys.platform == "win32" else 0, "")
        )
        checks = {check["check"]: check for check in out["checks"]}
        self.assertTrue(checks["data_dir_mode"]["ok"], out)
        self.assertTrue(checks["file_modes"]["ok"], out)
        if sys.platform == "win32":
            self.assertFalse(checks["launchd_job"]["ok"])


if sys.platform == "win32":

    class NativeCliRefusalTests(CliCase):
        """Actual ACL refusals keep stdout JSON and existing state intact."""

        def setUp(self) -> None:
            super().setUp()
            self.conn.close()

        def broaden(self, path: Path) -> None:
            """Grant read access only on temporary synthetic state."""
            subprocess.run(
                ["icacls", str(path), "/grant", "*S-1-1-0:(R)"],
                check=True,
                capture_output=True,
            )

        def test_broad_home_returns_content_free_json(self) -> None:
            self.broaden(self.home)
            code, out, err = self.muninn("ingest")
            self.assertEqual((code, err), (4, ""))
            self.assertEqual(out["error"], "store_unavailable")
            self.assertNotIn(str(self.home), str(out))

        def test_broad_journal_is_refused_before_replay(self) -> None:
            db = store.db_path(self.home)
            before = db.read_bytes()
            journal = db.with_name(db.name + "-journal")
            fd = platform_io.open_private(journal, os.O_CREAT | os.O_RDWR)
            os.write(fd, b"original synthetic journal")
            os.close(fd)
            self.broaden(journal)
            code, out, err = self.muninn("rebuild")
            self.assertEqual((code, err), (4, ""))
            self.assertEqual(out["error"], "store_unavailable")
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(
                journal.read_bytes(), b"original synthetic journal"
            )
