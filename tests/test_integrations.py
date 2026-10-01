"""Static provider-integration, service and config files for pctx (WU10)."""

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODEX_PLUGIN = ROOT / "integrations/codex/.codex-plugin/plugin.json"
CODEX_HOOKS = ROOT / "integrations/codex/hooks/hooks.json"
CLAUDE_HOOKS = ROOT / "integrations/claude/settings-hooks.json"
PLIST = ROOT / "launchd/com.provenance-context.plist"
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"
PYRIGHT = ROOT / "pyrightconfig.json"

HOME = "@HOME@"
SESSION_MATCHER = "startup|resume|clear|compact"
PYTHON = "/opt/homebrew/bin/python3.13"
ENV_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
RETIRED = (
    "provenance-context-build",
    "hooks/codex.py",
    "claude-code/",
    "scripts/",
)


def hook_command(event, provider):
    verb = {"SessionStart": "session-start", "UserPromptSubmit": "prompt"}
    return f"{HOME}/.local/bin/pctx hook {verb[event]} --provider {provider}"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def handlers(doc):
    """Yield (event, matcher, handler) for each hook handler in a doc."""
    for event, entries in doc["hooks"].items():
        for entry in entries:
            for handler in entry["hooks"]:
                yield event, entry.get("matcher"), handler


def load_plist():
    return plistlib.loads(PLIST.read_bytes())


class JsonFilesTest(unittest.TestCase):
    def test_every_integration_json_parses(self):
        found = sorted((ROOT / "integrations").rglob("*.json"))
        expected = {CODEX_PLUGIN, CODEX_HOOKS, CLAUDE_HOOKS}
        self.assertLessEqual(expected, set(found))
        for path in found + [PYRIGHT]:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertIsInstance(load(path), dict)


class CodexPluginTest(unittest.TestCase):
    def test_manifest_is_new_version_without_mcp_or_skills(self):
        manifest = load(CODEX_PLUGIN)
        self.assertEqual(manifest["name"], "provenance-context")
        self.assertEqual(manifest["version"], "0.2.0")
        allowed = {"name", "version", "description", "author", "interface"}
        self.assertLessEqual(set(manifest), allowed)
        self.assertFalse({"mcpServers", "skills", "hooks"} & set(manifest))

    def test_interface_uses_only_fields_the_old_manifest_had(self):
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

    def test_hooks_are_session_start_and_prompt_only(self):
        hooks = load(CODEX_HOOKS)["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "UserPromptSubmit"})
        self.assertNotIn("PreToolUse", hooks)

    def test_hook_settings(self):
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
    def test_fragment_has_exactly_the_two_events(self):
        doc = load(CLAUDE_HOOKS)
        self.assertEqual(set(doc), {"hooks"})
        self.assertEqual(
            set(doc["hooks"]), {"SessionStart", "UserPromptSubmit"}
        )

    def test_hook_settings(self):
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


class PlistTest(unittest.TestCase):
    def test_required_keys_and_values(self):
        plist = load_plist()
        self.assertEqual(plist["Label"], "com.provenance-context")
        self.assertEqual(
            plist["ProgramArguments"],
            [
                f"{HOME}/.local/lib/provenance-context/current/bin/pctx",
                "serve",
                "--interval",
                "60",
            ],
        )
        self.assertIs(plist["KeepAlive"], True)
        self.assertIs(plist["RunAtLoad"], True)
        self.assertEqual(plist["ProcessType"], "Background")
        self.assertIs(plist["LowPriorityIO"], True)
        log = f"{HOME}/.local/share/provenance-context/poller.log"
        self.assertEqual(plist["StandardOutPath"], log)
        self.assertEqual(plist["StandardErrorPath"], log)

    def test_umask_is_an_octal_string(self):
        # launchd.plist(5): a string is parsed by strtoul; a leading 0 = octal.
        umask = load_plist()["Umask"]
        self.assertIsInstance(umask, str)
        self.assertTrue(umask.startswith("0"))
        self.assertEqual(int(umask, 8), 0o077)

    def test_environment_is_path_only(self):
        plist = load_plist()
        self.assertEqual(plist["EnvironmentVariables"], {"PATH": ENV_PATH})
        self.assertNotIn("MLX", PLIST.read_text(encoding="utf-8").upper())

    def test_no_unexpected_keys(self):
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
    def test_plutil_lint(self):
        proc = subprocess.run(
            ["plutil", "-lint", str(PLIST)], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


def all_commands():
    """Every command string the integration files ask a host to run."""
    for path in (CODEX_HOOKS, CLAUDE_HOOKS):
        for _, _, hook in handlers(load(path)):
            yield path.name, hook["command"]
    yield PLIST.name, load_plist()["ProgramArguments"][0]


class CommandContractTest(unittest.TestCase):
    def test_every_command_starts_at_home_or_homebrew_python(self):
        commands = list(all_commands())
        self.assertEqual(len(commands), 5)
        for name, command in commands:
            with self.subTest(file=name, command=command):
                self.assertTrue(
                    command.startswith(f"{HOME}/")
                    or command.startswith(PYTHON),
                    command,
                )

    def test_no_file_references_the_old_tree_or_retired_paths(self):
        files = (
            CODEX_PLUGIN,
            CODEX_HOOKS,
            CLAUDE_HOOKS,
            PLIST,
            README,
            PYPROJECT,
            PYRIGHT,
        )
        for path in files:
            text = path.read_text(encoding="utf-8")
            for retired in RETIRED:
                with self.subTest(file=path.name, retired=retired):
                    self.assertNotIn(retired, text)

    def test_readme_is_concise(self):
        lines = README.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 150)


class HomeSubstitutionTest(unittest.TestCase):
    def test_placeholder_substitutes_to_absolute_paths(self):
        plist = load_plist()
        strings = [c for _, c in all_commands()]
        strings += [plist["StandardOutPath"], plist["StandardErrorPath"]]
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            for text in strings:
                with self.subTest(text=text):
                    done = text.replace(HOME, home)
                    self.assertNotIn("@", done)
                    first = shlex.split(done)[0]
                    self.assertTrue(os.path.isabs(first), first)
                    self.assertTrue(first.startswith(home + os.sep), first)

    def test_hook_commands_run_from_a_temp_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            stub = Path(home, ".local/bin/pctx")
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

    def test_plist_program_runs_through_the_current_symlink(self):
        # `python -I` puts nothing on sys.path, so pctx/__main__.py cannot be
        # run as a file; the plist must call the bin/pctx launcher instead.
        plist = load_plist()
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            release = Path(home, ".local/lib/provenance-context/abc1234")
            shutil.copytree(ROOT / "bin", release / "bin")
            shutil.copytree(
                ROOT / "pctx",
                release / "pctx",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            (release.parent / "current").symlink_to(release)
            # the installer's resolved interpreter, as install.cutover pins it
            (release.parent / "python").symlink_to(PYTHON)
            program = plist["ProgramArguments"][0].replace(HOME, home)
            self.assertEqual(
                os.path.realpath(program), str(release / "bin" / "pctx")
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
    def test_pyproject_drops_retired_per_file_ignores(self):
        config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        self.assertNotIn("per-file-ignores", config["tool"]["ruff"]["lint"])

    def test_pyright_targets_the_new_layout(self):
        config = load(PYRIGHT)
        self.assertEqual(
            config["include"], ["pctx", "trial_harness", "install", "tests"]
        )
        self.assertNotIn("extraPaths", config)


if __name__ == "__main__":
    unittest.main()
