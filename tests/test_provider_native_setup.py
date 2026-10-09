"""Pinned provider provisioning verifies digests before binary extraction."""

from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.provider_native_setup import extract_binary


class ProviderSetupTest(unittest.TestCase):
    """Verified archives cannot publish unexpected or linked members."""

    def test_zip_extracts_only_expected_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "codex-fixture.exe.zip"
            with zipfile.ZipFile(archive, "w") as source:
                source.writestr("codex-fixture.exe", b"synthetic executable")
                source.writestr("../unexpected", b"do not publish")
            target = Path(tmp) / "codex.exe"
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            extract_binary(archive, digest, target)
            self.assertEqual(target.read_bytes(), b"synthetic executable")
            self.assertFalse((Path(tmp).parent / "unexpected").exists())

    def test_digest_mismatch_creates_no_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "codex-fixture.exe.zip"
            archive.write_bytes(b"untrusted archive")
            target = Path(tmp) / "codex.exe"
            with self.assertRaisesRegex(ValueError, "digest"):
                extract_binary(archive, "0" * 64, target)
            self.assertFalse(target.exists())

    def test_tar_link_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "codex-fixture.tar.gz"
            with tarfile.open(archive, "w:gz") as source:
                member = tarfile.TarInfo("codex-fixture")
                member.type = tarfile.SYMTYPE
                member.linkname = "/unrelated"
                source.addfile(member, io.BytesIO())
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            target = Path(tmp) / "codex"
            with self.assertRaisesRegex(ValueError, "regular"):
                extract_binary(archive, digest, target)
            self.assertFalse(target.exists())
