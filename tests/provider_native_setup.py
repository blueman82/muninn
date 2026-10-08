"""Provision pinned official Codex binaries for hosted native proof."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from muninn import platform_io

_BASE = "https://github.com/openai/codex/releases/download/rust-v0.159.2/"
_ASSETS = {
    ("win32", "x86_64"): (
        "x86_64-pc-windows-msvc.exe.zip",
        "583a577b127304eb8cc88c427641d730257ef64dc608c581990cc23eb0161052",
        "bf87e3bfaa2cb46499d59bdb169adaf9a0633f2878166eed447ca4467e4ef24b",
    ),
    ("win32", "aarch64"): (
        "aarch64-pc-windows-msvc.exe.zip",
        "c84578aa147e326ccee47bcff2d512a8dca4de0e89e977eb9142491c3fa6c055",
        "66d58220229a6fd7f7fa09e56034e1bcc33dffca70fe36847022baf89e4d5f5a",
    ),
    ("linux", "x86_64"): (
        "x86_64-unknown-linux-musl.tar.gz",
        "26586b0d246d41a799b0ef8ee1add370f0fb0721b3709340f28db612381616ea",
        "17d91bf7962fa3d171f077e0b50cb89156f5e7109eedcf9f82803e5d86fba7fc",
    ),
    ("darwin", "aarch64"): (
        "aarch64-apple-darwin.tar.gz",
        "025deffd84cbc99d88ef952e585dc67c7c94e4273f89ba3d45f7e5799743c13c",
        "888d09d76206fce33d268962424bbc809ac54cc7a04a6943a4d3c7fc27f467d6",
    ),
    ("darwin", "x86_64"): (
        "x86_64-apple-darwin.tar.gz",
        "3f4e1f71aa05b1dd3d02dd66e780f7857cf19c693498a171f038f74ef3ef3be9",
        "a2f508ad1ba5b5064f5e30d51d9e696bb50619c3786a85ab24d036832d6481b2",
    ),
}


def extract_binary(archive: Path, digest: str, target: Path) -> None:
    """Publish only the expected regular binary from a verified archive.

    Args:
        archive: Official archive with its original asset filename.
        digest: Pinned SHA256 digest of the complete archive.
        target: Binary destination beneath a private proof directory.

    Raises:
        ValueError: If checksum, member name or member type is invalid.
    """
    with archive.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != digest:
        raise ValueError("provider asset digest mismatch")
    if archive.name.endswith(".zip"):
        expected = archive.name.removesuffix(".zip")
        with zipfile.ZipFile(archive) as source:
            members = [
                item for item in source.infolist() if item.filename == expected
            ]
            if len(members) != 1 or members[0].is_dir():
                raise ValueError(
                    "provider archive requires one regular binary"
                )
            mode = members[0].external_attr >> 16
            if mode & 0o170000 not in (0, 0o100000):
                raise ValueError("provider archive binary is not regular")
            data = source.read(members[0])
    else:
        expected = archive.name.removesuffix(".tar.gz")
        with tarfile.open(archive, "r:gz") as source:
            members = [
                item for item in source.getmembers() if item.name == expected
            ]
            if len(members) != 1 or not members[0].isreg():
                raise ValueError(
                    "provider archive requires one regular binary"
                )
            stream = source.extractfile(members[0])
            if stream is None:
                raise ValueError("provider archive binary is not regular")
            with stream:
                data = stream.read()
    fd = platform_io.open_private(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
    if sys.platform != "win32":
        target.chmod(0o700)


def main() -> None:
    """Download native pinned binaries and persist their CI PATH directory."""
    architecture = platform.machine().casefold()
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(
        architecture, architecture
    )
    suffix, cli_digest, server_digest = _ASSETS[sys.platform, architecture]
    root = Path(tempfile.mkdtemp(prefix="muninn-codex-01592-")).resolve()
    platform_io.ensure_private_dir(root)
    extension = ".exe" if sys.platform == "win32" else ""
    for name, digest in (
        ("codex", cli_digest),
        ("codex-app-server", server_digest),
    ):
        archive = root / f"{name}-{suffix}"
        with (
            urllib.request.urlopen(
                _BASE + archive.name, timeout=60
            ) as response,
            archive.open("xb") as output,
        ):
            shutil.copyfileobj(response, output)
        extract_binary(archive, digest, root / (name + extension))
        archive.unlink()
    github_path = os.environ.get("GITHUB_PATH")
    if github_path:
        with Path(github_path).open("a", encoding="utf-8") as output:
            output.write(str(root) + "\n")
    print(json.dumps({"codex_version": "0.159.2", "directory": str(root)}))


if __name__ == "__main__":
    main()
