"""Static provider-integration, service and config files for muninn."""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from install.context import Ctx, run_real
from install.provider_paths import render_pinned
from install.trust import codex_hooks

ROOT = Path(__file__).resolve().parent.parent
CODEX_PLUGIN = ROOT / "integrations/codex/.codex-plugin/plugin.json"
CODEX_HOOKS = ROOT / "integrations/codex/hooks/hooks.json"
CLAUDE_HOOKS = ROOT / "integrations/claude/settings-hooks.json"
CURSOR_HOOKS = ROOT / "integrations/cursor/hooks.json"
PLIST = ROOT / "launchd/com.muninn.plist"
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"
PYRIGHT = ROOT / "pyrightconfig.json"

HOME = "@HOME@"
SESSION_MATCHER = "startup|resume|clear|compact"
PYTHON = sys.executable
ENV_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def hook_command(event: str, provider: str) -> str:
    """Return the command a hook for ``event`` must run.

    Args:
        event: Hook event name, such as ``SessionStart``.
        provider: Provider passed to the hook command.

    Returns:
        The command line with the literal home placeholder.
    """
    verb = {
        "SessionStart": "session-start",
        "UserPromptSubmit": "prompt",
        "preCompact": "pre-compact",
    }
    return f"{HOME}/.local/bin/muninn hook {verb[event]} --provider {provider}"


def load(path: Path) -> Any:
    """Parse a JSON file.

    Args:
        path: File to read.

    Returns:
        The decoded document.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def handlers(doc: Any) -> Iterator[tuple[str, str | None, Any]]:
    """Yield (event, matcher, handler) for each hook handler in a doc.

    Args:
        doc: A hooks document with a top-level ``hooks`` mapping.

    Yields:
        The event name, the entry matcher or None, and the handler dict.
    """
    for event, entries in doc["hooks"].items():
        for entry in entries:
            for handler in entry["hooks"]:
                yield event, entry.get("matcher"), handler


def load_plist() -> Any:
    """Parse the launchd plist.

    Returns:
        The decoded plist.
    """
    return plistlib.loads(PLIST.read_bytes())


class JsonFilesTest(unittest.TestCase):
    """Every shipped JSON file parses."""

    def test_every_integration_json_parses(self) -> None:
        found = sorted((ROOT / "integrations").rglob("*.json"))
        expected = {CODEX_PLUGIN, CODEX_HOOKS, CLAUDE_HOOKS, CURSOR_HOOKS}
        self.assertLessEqual(expected, set(found))
        for path in [*found, PYRIGHT]:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertIsInstance(load(path), dict)


class CodexPluginTest(unittest.TestCase):
    """The Codex plugin manifest and hooks carry only what Codex needs."""

    def test_manifest_is_new_version_without_mcp_or_skills(self) -> None:
        manifest = load(CODEX_PLUGIN)
        self.assertEqual(manifest["name"], "muninn")
        self.assertEqual(manifest["version"], "0.2.0")
        allowed = {"name", "version", "description", "author", "interface"}
        self.assertLessEqual(set(manifest), allowed)
        self.assertFalse({"mcpServers", "skills", "hooks"} & set(manifest))

    def test_interface_uses_only_fields_the_old_manifest_had(self) -> None:
        interface = load(CODEX_PLUGIN)["interface"]
        allowed = {
            "displayName",
            "shortDescription",
            "longDescription",
            "developerName",
            "category",
            "capabilities",
        }
        self.assertLessEqual(set(interface), allowed)
        self.assertEqual(interface["capabilities"], ["Lifecycle hooks"])

    def test_hooks_are_session_start_and_prompt_only(self) -> None:
        hooks = load(CODEX_HOOKS)["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "UserPromptSubmit"})
        self.assertNotIn("PreToolUse", hooks)

    def test_hook_settings(self) -> None:
        limits = {"SessionStart": 1500, "UserPromptSubmit": 500}
        seen = set()
        for event, matcher, hook in handlers(load(CODEX_HOOKS)):
            seen.add(event)
            with self.subTest(event=event):
                self.assertEqual(hook["type"], "command")
                self.assertEqual(hook["timeout"], 5)
                self.assertEqual(hook["additionalContextLimit"], limits[event])
                self.assertEqual(hook["command"], hook_command(event, "codex"))
                # Codex ignores matchers on UserPromptSubmit; keep it absent.
                expected = SESSION_MATCHER if event == "SessionStart" else None
                self.assertEqual(matcher, expected)
        self.assertEqual(seen, set(limits))


class ClaudeFragmentTest(unittest.TestCase):
    """The Claude settings fragment wires exactly two hook events."""

    def test_fragment_has_exactly_the_two_events(self) -> None:
        doc = load(CLAUDE_HOOKS)
        self.assertEqual(set(doc), {"hooks"})
        self.assertEqual(
            set(doc["hooks"]), {"SessionStart", "UserPromptSubmit"}
        )

    def test_hook_settings(self) -> None:
        seen = set()
        for event, matcher, hook in handlers(load(CLAUDE_HOOKS)):
            seen.add(event)
            with self.subTest(event=event):
                self.assertEqual(
                    hook,
                    {
                        "type": "command",
                        "command": hook_command(event, "claude"),
                        "timeout": 5,
                    },
                )
                expected = SESSION_MATCHER if event == "SessionStart" else None
                self.assertEqual(matcher, expected)
        self.assertEqual(seen, {"SessionStart", "UserPromptSubmit"})


class CursorHooksTest(unittest.TestCase):
    """Cursor runs one native preCompact history refresh."""

    def test_precompact_hook_uses_cursor_provider(self) -> None:
        doc = load(CURSOR_HOOKS)
        self.assertEqual(doc["version"], 1)
        self.assertEqual(set(doc["hooks"]), {"preCompact"})
        [hook] = doc["hooks"]["preCompact"]
        self.assertEqual(
            hook,
            {
                "command": hook_command("preCompact", "cursor"),
                "timeout": 90,
            },
        )


class PlistTest(unittest.TestCase):
    """The launchd plist is complete, minimal and lint-clean."""

    def test_required_keys_and_values(self) -> None:
        plist = load_plist()
        self.assertEqual(plist["Label"], "com.muninn")
        self.assertEqual(
            plist["ProgramArguments"],
            [
                f"{HOME}/.local/lib/muninn/current/bin/muninn",
                "serve",
                "--interval",
                "60",
            ],
        )
        self.assertIs(plist["KeepAlive"], True)
        self.assertIs(plist["RunAtLoad"], True)
        self.assertEqual(plist["ProcessType"], "Background")
        self.assertIs(plist["LowPriorityIO"], True)
        # The poller writes poller.log itself (allowlisted, rotated); launchd
        # must not hold a second raw handle on it.
        self.assertEqual(plist["StandardOutPath"], "/dev/null")
        self.assertEqual(plist["StandardErrorPath"], "/dev/null")

    def test_umask_is_an_octal_string(self) -> None:
        # launchd.plist(5): a string is parsed by strtoul; a leading 0 = octal.
        umask = load_plist()["Umask"]
        self.assertIsInstance(umask, str)
        self.assertTrue(umask.startswith("0"))
        self.assertEqual(int(umask, 8), 0o077)

    def test_environment_is_path_only(self) -> None:
        plist = load_plist()
        self.assertEqual(plist["EnvironmentVariables"], {"PATH": ENV_PATH})
        self.assertNotIn("MLX", PLIST.read_text(encoding="utf-8").upper())

    def test_no_unexpected_keys(self) -> None:
        allowed = {
            "Label",
            "ProgramArguments",
            "KeepAlive",
            "RunAtLoad",
            "ProcessType",
            "LowPriorityIO",
            "Umask",
            "EnvironmentVariables",
            "StandardOutPath",
            "StandardErrorPath",
        }
        self.assertEqual(set(load_plist()), allowed)

    @unittest.skipUnless(shutil.which("plutil"), "plutil is macOS-only")
    def test_plutil_lint(self) -> None:
        proc = subprocess.run(
            ["plutil", "-lint", str(PLIST)], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


def _event_position(event: str) -> int:
    """Return the shipped hook's file-order position."""
    return 0 if event == "SessionStart" else 1


def all_commands() -> Iterator[tuple[str, str]]:
    """List every command the integration files ask a host to run.

    Yields:
        The file name and its command string.
    """
    for path in (CODEX_HOOKS, CLAUDE_HOOKS):
        for _, _, hook in handlers(load(path)):
            yield path.name, hook["command"]
    for hook in load(CURSOR_HOOKS)["hooks"]["preCompact"]:
        yield CURSOR_HOOKS.name, hook["command"]
    yield PLIST.name, load_plist()["ProgramArguments"][0]


class CommandContractTest(unittest.TestCase):
    """Every command the files run starts at a known location."""

    def test_every_command_starts_at_the_home_placeholder(self) -> None:
        commands = list(all_commands())
        self.assertEqual(len(commands), 6)
        for name, command in commands:
            with self.subTest(file=name, command=command):
                self.assertTrue(
                    command.startswith(f"{HOME}/"),
                    command,
                )

    def test_readme_is_concise(self) -> None:
        lines = README.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 150)


class HomeSubstitutionTest(unittest.TestCase):
    """The home placeholder resolves to commands that really run."""

    def test_placeholder_substitutes_to_absolute_paths(self) -> None:
        plist = load_plist()
        strings = [c for _, c in all_commands()]
        self.assertEqual(plist["StandardOutPath"], "/dev/null")
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            for text in strings:
                with self.subTest(text=text):
                    done = text.replace(HOME, home)
                    self.assertNotIn("@", done)
                    first = (
                        done.split(" hook ")[0]
                        if sys.platform == "win32"
                        else shlex.split(done)[0]
                    )
                    self.assertTrue(Path(first).is_absolute(), first)
                    self.assertTrue(
                        Path(first).is_relative_to(Path(home)), first
                    )

    def test_hook_commands_run_from_a_temp_home(self) -> None:
        if sys.platform == "win32":
            self.native_hook_arguments()
            return
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            stub = Path(home, ".local/bin/muninn")
            stub.parent.mkdir(parents=True)
            stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            stub.chmod(0o755)
            for path, provider in (
                (CLAUDE_HOOKS, "claude"),
                (CODEX_HOOKS, "codex"),
            ):
                for event, _, hook in handlers(load(path)):
                    with self.subTest(provider=provider, event=event):
                        proc = subprocess.run(
                            hook["command"].replace(HOME, home),
                            shell=True,
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        words = hook_command(event, provider).split()[1:]
                        self.assertEqual(proc.stdout.split(), words)
            for hook in load(CURSOR_HOOKS)["hooks"]["preCompact"]:
                proc = subprocess.run(
                    hook["command"].replace(HOME, home),
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                words = hook_command("preCompact", "cursor").split()[1:]
                self.assertEqual(proc.stdout.split(), words)

    def native_hook_arguments(self) -> None:
        """Run rendered Windows provider commands against a synthetic stub."""
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(
                Path(tmp).resolve() / "space café owner's % home",
                run_real,
                "test",
                platform="win32",
            )
            stub = ctx.muninn.with_suffix(".ps1")
            stub.parent.mkdir(parents=True)
            stub.write_text(
                "[Console]::Out.WriteLine(($args -join '|'))\n",
                encoding="utf-8",
            )
            for relative, provider in (
                ("integrations/claude/settings-hooks.json", "claude"),
                ("integrations/codex/hooks/hooks.json", "codex"),
            ):
                raw = render_pinned(
                    ctx, relative, (ROOT / relative).read_bytes()
                )
                for event, _, handler in handlers(json.loads(raw)):
                    if provider == "claude":
                        command: str | list[str] = [
                            handler["command"],
                            *handler["args"],
                        ]
                    else:
                        command = codex_hooks(raw, platform="win32")[
                            _event_position(event)
                        ]["command"]
                    result = subprocess.run(
                        command,
                        shell=provider == "codex",
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    verb = (
                        "session-start"
                        if event == "SessionStart"
                        else "prompt"
                    )
                    self.assertEqual(
                        result.stdout.strip(),
                        f"hook|{verb}|--provider|{provider}",
                    )

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX launchd links; native bootstrap has mandatory hosted proof",
    )
    def test_plist_program_runs_through_the_current_symlink(self) -> None:
        # `python -I` puts nothing on sys.path, so muninn/__main__.py cannot be
        # run as a file; the plist must call the bin/muninn launcher instead.
        plist = load_plist()
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            release = Path(home, ".local/lib/muninn/abc1234")
            shutil.copytree(ROOT / "bin", release / "bin")
            shutil.copytree(
                ROOT / "muninn",
                release / "muninn",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            (release.parent / "current").symlink_to(release)
            # The installer pins its resolved interpreter at this path.
            (release.parent / "python").symlink_to(PYTHON)
            program = plist["ProgramArguments"][0].replace(HOME, home)
            self.assertEqual(
                os.path.realpath(program), str(release / "bin" / "muninn")
            )
            self.assertTrue(os.access(program, os.X_OK), program)
            proc = subprocess.run(
                [program, "--version"],
                cwd="/",
                env=plist["EnvironmentVariables"] | {"HOME": home},
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip())


class ConfigCleanupTest(unittest.TestCase):
    """The lint and type configs keep the standards the checks rely on."""

    def test_ruff_enforces_google_docstrings_and_limits(self) -> None:
        lint = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        lint = lint["tool"]["ruff"]["lint"]
        self.assertEqual(lint["pydocstyle"]["convention"], "google")
        self.assertTrue({"D", "DOC", "ANN", "N", "PTH"} <= set(lint["select"]))
        self.assertLessEqual(lint["mccabe"]["max-complexity"], 10)
        self.assertLessEqual(lint["pylint"]["max-statements"], 50)

    def test_pyright_is_strict_for_runtime_code(self) -> None:
        config = load(PYRIGHT)
        self.assertEqual(
            config["include"], ["muninn", "install", "tools", "tests"]
        )
        self.assertEqual(config["strict"], ["muninn", "install", "tools"])
        self.assertNotIn("extraPaths", config)


if __name__ == "__main__":
    unittest.main()
