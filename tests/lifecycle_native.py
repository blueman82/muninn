"""Actual native installer and service smoke in synthetic isolated homes."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

from install import lifecycle
from install.context import Ctx, run_real
from muninn import obs, obs_status, platform_io, tombstone_key
from muninn.obs_service import parse_process, process_command, service_status
from tests import (
    lifecycle_native_doctor,
    lifecycle_native_entry,
    lifecycle_native_linux,
    lifecycle_native_records,
    lifecycle_native_rollback,
    lifecycle_native_windows,
    lifecycle_native_writer,
)
from tests.native_diagnostics import write

ROOT = Path(__file__).resolve().parent.parent


def assert_service(ctx: Ctx, sha: str) -> None:
    """Require a real Linux service identity and its immutable loaded SHA."""
    if sys.platform == "linux":
        status = obs_status.read_status(ctx.data)
        library = Path(os.environ["LD_LIBRARY_PATH"]) / "libsqlite3.so.0"
        maps = Path(f"/proc/{status['pid']}/maps").read_text()
        assert str(library.resolve()) in maps, "writer SQLite environment"
        assert status.get("writer_install_sha") == sha
        assert service_status(ctx.data, {"HOME": str(ctx.home)}, obs.run) == (
            True,
            status["pid"],
        )


def exercise(parent: Path) -> dict[str, object]:
    """Run real fresh, graceful stop and upgrade with no provider configs."""
    home = parent / "home"
    platform_io.ensure_private_dir(home)
    sha = (
        subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    messages: list[str] = []

    ctx = Ctx(
        home,
        lifecycle_native_doctor.run,
        "native-fresh",
        fresh=True,
        say=messages.append,
    )
    started = time.monotonic()
    write("lifecycle", "fresh_install")
    lifecycle_native_entry.install(ctx, ROOT, sha)
    first_job = lifecycle.job(ctx)
    assert first_job is not None
    assert isinstance(first_job["pid"], int) and first_job["pid"] > 0
    first = obs_status.read_status(ctx.data)
    assert first["pid"] == first_job["pid"]
    assert isinstance(first.get("pid"), int), first
    assert_service(ctx, sha)
    key = tombstone_key.load_key(ctx.data)
    write("lifecycle", "fresh_stop")
    lifecycle.stop(ctx)
    stopped = lifecycle.job(ctx)
    assert stopped is None or not stopped["pid"]
    if sys.platform == "linux":
        lifecycle_native_writer.busy_stop(ctx)
    upgraded = dataclasses.replace(
        ctx, ts="native-upgrade", fresh=False, upgrade=True
    )
    upgraded_started = time.time()
    write("lifecycle", "upgrade_install")
    lifecycle_native_entry.install(upgraded, ROOT, sha)
    after_job = lifecycle.job(upgraded)
    assert after_job is not None
    assert isinstance(after_job["pid"], int) and after_job["pid"] > 0
    after = obs_status.read_status(ctx.data)
    assert after["pid"] == after_job["pid"]
    assert after["pid"] != first["pid"], (first["pid"], after["pid"])
    assert_service(upgraded, sha)
    if sys.platform == "win32":
        assert after["stop_generation"] != first["stop_generation"]
    assert tombstone_key.load_key(ctx.data, create=False) == key
    write("lifecycle", "upgrade_stop")
    if sys.platform == "linux":
        lifecycle_native_writer.crash_restart(upgraded, upgraded_started)
        lifecycle_native_rollback.rollback(upgraded, ROOT)
        lifecycle_native_entry.maintenance_checks(upgraded, ROOT)
    else:
        lifecycle.stop(upgraded)
    result = {
        "sha": sha,
        "platform": sys.platform,
        "fresh": True,
        "upgrade": True,
        "graceful_stop": True,
        "key_retained": True,
        "managed_busy_commit": sys.platform == "linux",
        "managed_crash_restart": sys.platform == "linux",
        "migration_rollback": sys.platform == "linux",
        "source_wrappers": sys.platform == "linux",
        "native_seconds": round(time.monotonic() - started, 3),
    }
    cleanup_backend(upgraded)
    return result


def cleanup_backend(ctx: Ctx) -> None:
    """Remove only owned synthetic service resources after graceful stop."""
    if sys.platform == "win32":
        deleted = run_real(
            ["schtasks.exe", "/Delete", "/TN", ctx.target, "/F"]
        )
        assert deleted.returncode == 0
    else:
        ctx.plist.unlink()
        run_real(["systemctl", "--user", "daemon-reload"])


def refused_main(parent: Path) -> None:
    """Prove the real CLI refuses unsafe preflight without state writes."""
    write("lifecycle", "refused_cli")
    sha = (
        subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    home = parent / "refused-home"
    env = dict(os.environ)
    if sys.platform == "linux":
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(
            parent / "missing-bus"
        )
        env["XDG_RUNTIME_DIR"] = str(parent / "missing-runtime")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "install.installer",
            "--repo",
            str(ROOT),
            "--sha",
            sha,
            "--fresh",
            "--home",
            str(home),
        ],
        env=env,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 1, result.returncode
    expected = (
        b"systemctl --user exited"
        if sys.platform == "linux"
        else b"run the installer as an ordinary Windows user"
    )
    assert expected in result.stdout, "CLI refused at an unexpected preflight"
    assert not home.exists(), "refused CLI created state"


def main() -> int:
    """Run under a normal token and a private manager, never uninstall CLI."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=Path)
    args = parser.parse_args()
    if args.child is not None:
        result: dict[str, object]
        mismatches = lifecycle_native_records.record_task_mismatch()
        bindings = lifecycle_native_records.record_writer_mismatch()
        jobs = lifecycle_native_records.record_new_job()
        doctor = lifecycle_native_records.record_doctor_output()
        try:
            codes = lifecycle_native_windows.token_codes()
            write("lifecycle", "ordinary_child_identity", metrics=codes)
            elevated = codes.get("token_elevated") == 1
            if elevated:
                lifecycle._identity = lifecycle_native_windows.hosted_identity
            ordinary = lifecycle._identity(
                Ctx(args.child, run_real, "identity")
            )
            identity = parse_process(
                run_real(process_command(os.getpid())).stdout
            )
            result = exercise(args.child)
            result["child_pid"] = identity["pid"]
            result["child_created"] = identity["created"]
            result["ordinary_user_sid"] = ordinary
            result["elevated"] = elevated
        except Exception as exc:
            failed = {
                "task_owned_mismatch": min(mismatches, default=0),
                "writer_binding_mismatch": min(bindings, default=0),
                "new_job_flags": jobs[0],
                "doctor_output_length": doctor[0],
                "doctor_output_first_byte": doctor[1],
                "doctor_exit_status": doctor[2],
                "doctor_error_length": doctor[3],
                "step_failure_id": zlib.crc32(str(exc).encode()) & 0x7FFFFFFF,
            }
            write("lifecycle", None, error=exc, metrics=failed)
            result = {
                "ok": False,
                "error": type(exc).__name__,
                "winerror": getattr(exc, "winerror", None),
            }
        else:
            result["ok"] = True
        (args.child / "result.json").write_text(
            json.dumps(result), encoding="utf-8"
        )
        return 0 if result["ok"] else 1
    temporary = tempfile.TemporaryDirectory(
        prefix="muninn-native-life-", delete=False
    )
    parent = Path(temporary.name)
    try:
        refused_main(parent)
        if sys.platform == "win32":
            write("lifecycle", "ordinary_child_start")
            result = lifecycle_native_windows.run_child(parent, ROOT)
        elif sys.platform == "linux":
            write("lifecycle", "manager_start")
            with lifecycle_native_linux.manager(parent):
                result = exercise(parent)
        else:
            raise RuntimeError("native lifecycle requires Windows or Linux")
        temporary.cleanup()
    except Exception as exc:
        write("lifecycle", None, error=exc)
        print(json.dumps({"retained_state": str(parent), "pid": os.getpid()}))
        raise
    write("lifecycle", "complete", completed=True)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
