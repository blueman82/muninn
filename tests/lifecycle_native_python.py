"""Provision only a job-local copy when Linux CI's interpreter is writable."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from muninn import platform_io


def prepare(prefix: Path, executable: Path, target: Path, uid: int) -> Path:
    """Keep the trusted CI runtime unchanged and relocate only its copy.

    Args:
        prefix: Known setup-python prefix containing standard runtime files.
        executable: Actual running executable beneath that prefix.
        target: New directory beneath the private native proof scratch.
        uid: Current CI user id; source owner must be that user or root.

    Returns:
        Original safe executable or relocated private executable.

    Raises:
        OSError: If source identity, runtime layout or copied state is unsafe.
        FileExistsError: If a previous destination already exists.
    """
    source = executable.resolve(strict=True)
    base = prefix.resolve(strict=True)
    metadata = source.stat()
    codes = {
        "code": "native_interpreter_source",
        "regular": int(stat.S_ISREG(metadata.st_mode)),
        "owner_trusted": int(metadata.st_uid in (uid, 0)),
        "write_mask": stat.S_IMODE(metadata.st_mode) & 0o022,
        "parent_write_mask": stat.S_IMODE(source.parent.stat().st_mode)
        & 0o022,
    }
    print(json.dumps(codes), flush=True)
    if (
        not codes["regular"]
        or not codes["owner_trusted"]
        or not source.is_relative_to(base)
    ):
        raise OSError("unrecognized CI interpreter source")
    if not codes["write_mask"] and not codes["parent_write_mask"]:
        return executable
    if os.path.lexists(target):
        raise FileExistsError("unknown CI interpreter copy retained")
    if not platform_io.is_private(target.parent, directory=True):
        raise OSError("CI interpreter copy parent is not private")
    markers = (Path("lib/python3.13/os.py"), Path("lib/libpython3.13.so.1.0"))
    if not all((base / marker).is_file() for marker in markers):
        raise OSError("unrecognized CI interpreter runtime layout")
    identity = _identity(metadata)
    digest = hashlib.sha256(source.read_bytes()).digest()
    shutil.copytree(base, target, symlinks=False)
    _private_copy(target)
    copied = target / source.relative_to(base)
    if (
        identity != _identity(source.stat())
        or digest != hashlib.sha256(source.read_bytes()).digest()
        or digest != hashlib.sha256(copied.read_bytes()).digest()
    ):
        raise OSError("CI interpreter source changed during copy")
    if any(
        (target / marker).read_bytes() != (base / marker).read_bytes()
        for marker in markers
    ):
        raise OSError("CI interpreter runtime copy differs")
    return copied


def _identity(info: os.stat_result) -> tuple[int, ...]:
    """Compare identity and mutation fields while ignoring access time."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _private_copy(target: Path) -> None:
    """Narrow permissions only on ordinary objects in the new copied prefix.

    Raises:
        OSError: If any copy object has an unsupported type.
    """
    for path in [target, *target.rglob("*")]:
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            path.chmod(0o700)
        elif stat.S_ISREG(mode):
            path.chmod(
                0o700 if path.relative_to(target).parts[0] == "bin" else 0o600
            )
        else:
            raise OSError("CI interpreter copy is not ordinary")


def readiness_command(selected: Path, expected: Path) -> list[str]:
    """Prove prefix, stdlib and extension relocation without path output."""
    script = (
        "import encodings,sqlite3,sys,_sqlite3;from pathlib import Path;"
        "root=Path(sys.argv[1]).resolve();"
        "assert Path(sys.prefix).resolve()==root;"
        "assert Path(encodings.__file__).resolve().is_relative_to(root);"
        "assert Path(_sqlite3.__file__).resolve().is_relative_to(root);"
        "assert sys.version_info[:2]==(3,13);"
        "assert sqlite3.sqlite_version_info==(3,50,4);"
        'print("native_private_python_ready")'
    )
    return [str(selected), "-I", "-c", script, str(expected)]


def main() -> int:
    """Publish a verified synthetic prefix only to this job's PATH."""
    if sys.platform != "linux":
        raise OSError("native_linux_required")
    target = Path(os.environ["MUNINN_NATIVE_SCRATCH"]) / "python"
    selected = prepare(
        Path(sys.base_prefix), Path(sys.executable), target, os.getuid()
    )
    expected = target if selected != Path(sys.executable) else Path(sys.prefix)
    result = subprocess.run(
        readiness_command(selected, expected),
        capture_output=True,
        check=True,
        timeout=30,
    )
    if (
        result.stdout.strip() != b"native_private_python_ready"
        or result.stderr
    ):
        raise OSError("CI interpreter readiness unknown")
    with Path(os.environ["GITHUB_OUTPUT"]).open(
        "a", encoding="utf-8"
    ) as output:
        output.write("executable=" + str(selected) + "\n")
    if selected != Path(sys.executable):
        with Path(os.environ["GITHUB_PATH"]).open(
            "a", encoding="utf-8"
        ) as output:
            output.write(str(selected.parent) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
