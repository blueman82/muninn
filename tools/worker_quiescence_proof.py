"""Retain source-bound native worker regression evidence."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BASELINE = "d944d14eed887e913a01923f245cd5c0bfb26667"
TEST = "tests/test_review_group_quiescence.py"
CASE = (
    "tests.test_review_group_quiescence.GroupQuiescenceTests."
    "test_delayed_signal_keeps_outcome_pending_before_reap"
)


def expected_red(code: int, output: bytes) -> bool:
    """Accept the precise containment assertion as the old failure."""
    lines = output.decode("utf-8", errors="replace").splitlines()
    return (
        code == 1
        and any(
            line.startswith(
                "FAIL: test_delayed_signal_keeps_outcome_pending_before_reap "
            )
            for line in lines
        )
        and (
            "AssertionError: True is not false : "
            "premature outcome before group quiescence"
        )
        in lines
        and "FAILED (failures=1)" in lines
        and any(line.startswith("Ran 1 test in ") for line in lines)
        and not any(line.startswith("ERROR:") for line in lines)
        and sum(line.startswith("FAIL:") for line in lines) == 1
    )


def _git(root: Path, *arguments: str) -> bytes:
    """Read immutable repository objects with a finite subprocess deadline."""
    return subprocess.check_output(["git", *arguments], cwd=root, timeout=30)


def _run(root: Path, output: Path, label: str) -> tuple[int, bytes]:
    """Run the same test bytes with private HOME and retained logs."""
    with tempfile.TemporaryDirectory(prefix="muninn-proof-home-") as home:
        environment = os.environ | {
            "HOME": home,
            "MUNINN_HOME": str(Path(home) / "muninn"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        command = [sys.executable, "-B", "-m", "unittest", CASE]
        try:
            result = subprocess.run(
                command,
                cwd=root,
                env=environment,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout if isinstance(error.stdout, bytes) else b""
            stderr = error.stderr if isinstance(error.stderr, bytes) else b""
            result = subprocess.CompletedProcess(
                command, 124, stdout, stderr + b"\nPROOF_TIMEOUT\n"
            )
    (output / f"{label}.stdout").write_bytes(result.stdout)
    (output / f"{label}.stderr").write_bytes(result.stderr)
    (output / f"{label}.exit").write_text(str(result.returncode) + "\n")
    return result.returncode, result.stdout + result.stderr


def prove(root: Path, output: Path) -> bool:
    """Bind native RED/GREEN to commits and identical test bytes."""
    output.mkdir(parents=True, exist_ok=True)
    head = _git(root, "rev-parse", "HEAD").decode().strip()
    test = (root / TEST).read_bytes()
    metadata: dict[str, object] = {
        "baseline_sha": BASELINE,
        "head_sha": head,
        "head_tree": _git(root, "rev-parse", "HEAD^{tree}").decode().strip(),
        "test_sha256": hashlib.sha256(test).hexdigest(),
        "platform": platform.platform(),
        "python": sys.version,
        "red_expected": False,
        "green_passed": False,
    }
    try:
        pr_head = os.environ.get("MUNINN_PROOF_PR_HEAD", "")
        if pr_head:
            if len(pr_head) != 40 or any(
                char not in "0123456789abcdef" for char in pr_head
            ):
                raise ValueError("invalid_pr_head")
            _git(root, "merge-base", "--is-ancestor", pr_head, head)
            metadata["pr_head_sha"] = pr_head
        if sys.platform != "linux":
            raise OSError("native_linux_required")
        if _git(root, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("proof_requires_clean_committed_tree")
        if test != _git(root, "show", f"{head}:{TEST}"):
            raise ValueError("proof_test_not_bound_to_head")
        metadata["baseline_runtime_blob"] = (
            _git(root, "rev-parse", f"{BASELINE}:muninn/review_process.py")
            .decode()
            .strip()
        )
        with tempfile.TemporaryDirectory(prefix="muninn-proof-old-") as old:
            baseline = Path(old)
            archive = _git(root, "archive", "--format=tar", BASELINE)
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                source.extractall(baseline, filter="data")
            (baseline / TEST).write_bytes(test)
            code, raw = _run(baseline, output, "old")
            metadata["old_exit"] = code
            metadata["red_expected"] = expected_red(code, raw)
        code, raw = _run(root, output, "new")
        metadata["new_exit"] = code
        metadata["green_passed"] = (
            code == 0
            and b"\nOK\n" in raw
            and b"Ran 1 test in " in raw
            and b"skipped=" not in raw
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        metadata["proof_error"] = type(error).__name__ + ": " + str(error)
    finally:
        (output / "proof.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(metadata, sort_keys=True))
    return (
        metadata["red_expected"] is True and metadata["green_passed"] is True
    )


def main() -> int:
    """Run the dedicated native proof into the requested artifact directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    return 0 if prove(root, args.output) else 1


if __name__ == "__main__":
    raise SystemExit(main())
