"""Observe native tool advertisement against a local rejecting HTTP fixture.

Ambient credentials are not supplied. Isolation remains unverified.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import threading
from http.server import HTTPServer
from pathlib import Path
from typing import cast

from muninn.review_codex_launch import verify_codex_executable, wire_schema
from muninn.review_launch_spec import codex_cli_argv
from muninn.review_models import RunnerConfig
from tools.learning_eval.codex_tool_probe_io import (
    CANARY,
    capture_handler,
    run_command,
    write_private,
)
from tools.learning_eval.mac_provider_models import canonical, file_hash

SOURCE_FILES = {
    "probe_source_sha256": Path(__file__),
    "io_source_sha256": Path(__file__).with_name("codex_tool_probe_io.py"),
    "recipe_source_sha256": (
        Path(__file__).parents[2] / "muninn/review_launch_spec.py"
    ),
}

CANDIDATE_DISABLED = (
    "shell_tool",
    "apps",
    "plugins",
    "multi_agent",
    "memories",
    "view_image",
    "image_generation",
    "browser_use",
    "computer_use",
    "in_app_browser",
    "code_mode_host",
    "code_mode_host_grpc",
    "code_mode",
    "code_mode_only",
    "unified_exec",
    "goals",
    "skill_search",
)


def _prepare(
    root: Path, poisoned: bool, cwd_control: bool = False
) -> tuple[Path, Path, Path, dict[str, str]]:
    """Create synthetic homes with no ambient credentials or provider state."""
    home, auth = root / "home", root / "auth"
    cwd = home / "review-runner-cwd"
    schema = home / "review-codex-schema.json"
    for directory in (home, auth, cwd, root / "tmp"):
        directory.mkdir(mode=0o700)
    write_private(schema, wire_schema())
    if poisoned:
        (auth / "AGENTS.md").write_text(CANARY + "_AUTH_AGENTS")
        (auth / "config.toml").write_text(
            f'developer_instructions="{CANARY}_CONFIG"\n'
        )
        rules = auth / "rules"
        rules.mkdir()
        (rules / "probe.rules").write_text(f"# {CANARY}_RULES\n")
    if cwd_control:
        (cwd / "AGENTS.md").write_text(CANARY + "_CWD_AGENTS")
    env = {
        "HOME": str(home),
        "CODEX_HOME": str(auth),
        "TMPDIR": str(root / "tmp"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "XDG_CONFIG_HOME": str(root / ".config"),
        "XDG_CACHE_HOME": str(root / ".cache"),
    }
    return auth, cwd, schema, env


def _variant(
    executable: Path,
    root: Path,
    candidate: bool,
    poisoned: bool,
    source_control: bool = False,
    cwd_control: bool = False,
    instructions_disabled: bool = False,
) -> dict[str, object]:
    """Observe the current recipe with fixed local-only transport overrides."""
    _, cwd, schema, env = _prepare(root, poisoned, cwd_control)
    records: list[dict[str, object]] = []
    with HTTPServer(
        ("127.0.0.1", 0), capture_handler(records, root)
    ) as server:
        server.timeout = 5
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        thread.start()
        requested = RunnerConfig(
            runner="codex_cli",
            executable=str(executable),
            version="any",
            model="gpt-6.1-sol",
            reasoning="low",
        )
        argv = list(codex_cli_argv(requested, cwd, schema)[:-1])
        if source_control:
            argv.remove("--ignore-user-config")
            argv.remove("--ignore-rules")
        provider = (
            '{name="Local rejecting fixture",base_url="http://127.0.0.1:'
            f'{server.server_port}",wire_api="responses",requires_openai_auth=false,'
            "request_max_retries=0,stream_max_retries=0}"
        )
        argv.extend(
            (
                "-c",
                'model_provider="local_probe"',
                "-c",
                "model_providers.local_probe=" + provider,
                "--disable",
                "enable_request_compression",
                "-c",
                'cli_auth_credentials_store="file"',
                "-c",
                "analytics.enabled=false",
            )
        )
        if candidate:
            for feature in CANDIDATE_DISABLED:
                argv.extend(("--disable", feature))
            argv.extend(("-c", 'web_search="disabled"'))
        if instructions_disabled:
            argv.extend(("-c", "project_doc_max_bytes=0"))
        try:
            result = run_command([*argv, "-"], env, cwd)
        finally:
            server.shutdown()
            thread.join(timeout=6)
        if thread.is_alive():
            raise ValueError("server_cleanup_incomplete")
    for name in ("stdout", "stderr"):
        raw = cast(bytes, result.pop(name))
        path = root / f"{name}.bin"
        write_private(path, raw)
        result[name + "_path"] = str(path)
    return {
        "candidate_restrictions": candidate,
        "poisoned_sources": poisoned,
        "local_requests": len(records),
        "requests": records,
        "process": result,
        "argv": [*argv, "-"],
        "environment": env,
        "source_control": source_control,
        "production_cwd_eligible": not cwd_control,
        "instructions_disabled": instructions_disabled,
    }


def _variants(executable: Path, artifacts: Path) -> list[dict[str, object]]:
    """Keep each local diagnostic and its source-control scope explicit."""
    variants: list[dict[str, object]] = []
    for (
        name,
        candidate,
        poisoned,
        source_control,
        cwd_control,
        instructions_disabled,
    ) in (
        ("clean", False, False, False, False, False),
        ("auth_poisoned", False, True, False, False, False),
        ("candidate", True, True, False, False, False),
        ("source_control", False, True, True, False, False),
        ("cwd_control", False, False, False, True, False),
        ("instructions_disabled", False, True, False, False, True),
    ):
        root = artifacts / name
        root.mkdir(mode=0o700)
        variants.append(
            {
                "name": name,
                **_variant(
                    executable,
                    root,
                    candidate,
                    poisoned,
                    source_control,
                    cwd_control,
                    instructions_disabled,
                ),
            }
        )
    return variants


def _source_digests() -> dict[str, str]:
    """Bind source bytes before and after native observations."""
    return {name: file_hash(path) for name, path in SOURCE_FILES.items()}


def run(executable: Path, output: Path) -> dict[str, object]:
    """Require signed native Codex and retain synthetic evidence privately.

    Args:
        executable: Explicit installed Codex path; no hash is assumed.
        output: New absolute report path under an existing canonical parent.

    Returns:
        Unqualified observations and exact private request artifact paths.

    Raises:
        ValueError: If platform, identity, path or cleanup verification fails.
    """
    if (
        os.name != "posix"
        or not output.is_absolute()
        or output.exists()
        or output.is_symlink()
        or output.parent.resolve() != output.parent
    ):
        raise ValueError("native_probe_boundary")
    sources = _source_digests()
    target, before = verify_codex_executable(executable)
    artifacts = output.with_suffix(".requests")
    artifacts.mkdir(mode=0o700)
    version_root = artifacts / "version"
    version_root.mkdir(mode=0o700)
    _, version_cwd, _, version_env = _prepare(version_root, False)
    version = (
        subprocess.run(
            (str(target), "--version"),
            env=version_env,
            cwd=version_cwd,
            capture_output=True,
            check=True,
            timeout=10,
        )
        .stdout.decode()
        .strip()
    )
    variants = _variants(target, artifacts)
    if file_hash(target) != before:
        raise ValueError("provider_changed")
    if _source_digests() != sources:
        raise ValueError("probe_source_changed")
    report: dict[str, object] = {
        "schema_version": 1,
        "qualification": "unverified",
        "executable": str(target),
        "executable_sha256": before,
        "version": version,
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "version": platform.platform(),
        },
        **sources,
        "variants": variants,
        "ambient_credentials_provided": False,
        "fixture_returns_model_output": False,
        "provider_billing": None,
        "production_native_auth": "unverified",
        "c6_assessment": "unverified",
        "limitations": [
            "local_http_fixture_only",
            "tool_advertisement_not_universal_isolation",
            "synthetic_source_canaries_only",
            "external_requests_not_measured",
            "credential_access_not_instrumented",
            "direct_argv_bypasses_runtime_capability_binding",
        ],
    }
    write_private(output, canonical(report) + b"\n")
    return report


def main() -> None:
    """Run only with explicit signed executable and a new private report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(canonical(run(args.codex, args.output)).decode())


if __name__ == "__main__":
    main()
