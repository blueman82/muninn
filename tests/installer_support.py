"""Fakes and fixtures shared by the installer test modules.

launchctl, ps, muninn, codex and the Codex hooks/list probe are fakes; git and
cp run for real, but only on temp dirs. Config files are synthetic and carry
a fake credential that must never be recorded.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from install import configedit as ce
from install import installer as co
from install.context import Ctx
from muninn import store

ROOT = Path(__file__).resolve().parent.parent
CRED = "sk-fake-" + "feedface" * 5
PINNED = (
    "bin/muninn",
    "integrations/claude/settings-hooks.json",
    "integrations/codex/.agents/plugins/marketplace.json",
    "integrations/codex/.codex-plugin/plugin.json",
    "integrations/codex/hooks/hooks.json",
    "launchd/com.muninn.plist",
    "muninn/store_schema.py",
)
# Real Codex 0.160.0 `hooks/list` currentHash values for the shipped
# hooks.json (literal @HOME@ commands), from an isolated CODEX_HOME probe
# (app-server over stdio, temp HOME and CODEX_HOME).
REAL_CODEX = {
    "session_start:0:0": "sha256:da17ae7f21bc6153ecf3d200a479af2c6d5253f6"
    "ecbd0e151073971f998baaad",
    "user_prompt_submit:0:0": "sha256:0f70ed084cf82ab3f801aca70bbf2a5bda6b6ec9"
    "3d5b72bbcf264a74d7b3f80d",
}
CONFIG_FILES = (".claude/settings.json", ".codex/config.toml")


def git(cwd: Path, *args: str) -> bytes:
    """Run a real git command in ``cwd`` under a fixed identity.

    Args:
        cwd: Repository directory.
        *args: Arguments after ``git -C <cwd>``.

    Returns:
        The command's stdout.
    """
    env = dict(co.GIT_ENV, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t")
    env.update(GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env=env,
    ).stdout


def done(
    stdout: bytes = b"", rc: int = 0
) -> subprocess.CompletedProcess[bytes]:
    """Build a finished process with the given stdout and exit code.

    Args:
        stdout: Captured standard output.
        rc: Exit status.

    Returns:
        A completed process with empty stderr.
    """
    return subprocess.CompletedProcess([], rc, stdout, b"")


def make_store(data: Path, version: int, marker: str = "row") -> Path:
    """Write a small real SQLite store with a schema version and one row.

    Args:
        data: The data directory.
        version: The ``user_version`` to stamp.
        marker: Text of the one row, so a test can tell stores apart.

    Returns:
        The store path.
    """
    path = data / "muninn.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS t(x TEXT)")
    conn.execute("DELETE FROM t")
    conn.execute("INSERT INTO t VALUES (?)", (marker,))
    conn.commit()
    conn.execute(f"PRAGMA user_version={version}")
    conn.close()
    path.chmod(0o600)
    return path


def dump(obj: object) -> bytes:
    """Serialise ``obj`` as two-space-indented JSON with a final newline.

    Args:
        obj: A JSON-serialisable value.

    Returns:
        The encoded document.
    """
    return (json.dumps(obj, indent=2) + "\n").encode()


def snapshot(home: Path) -> dict[str, bytes]:
    """Read both provider config files under ``home``.

    Args:
        home: The temp HOME.

    Returns:
        File contents keyed by path relative to ``home``.
    """
    return {rel: (home / rel).read_bytes() for rel in CONFIG_FILES}


class Fake:
    """Stand-ins for launchctl, ps, muninn and codex over a temp HOME."""

    def __init__(self, home: Path) -> None:
        """Start with a healthy, not-yet-loaded service.

        Args:
            home: The temp HOME the fakes operate on.
        """
        self.home = home
        self.calls: list[list[str]] = []
        self.clock = [time.time()]
        self.loaded: str | None = None
        self.pid = 64653
        self.heartbeat = True
        self.doctor = 0
        self.toml_writes: list[str] = []
        self.probe_mode = "ok"
        self.store_version = store.SCHEMA_VERSION
        self.migrates: str | None = None  # the release that migrates
        self.version = b"codex-cli 0.159.2\n"

    def now(self) -> float:
        """Return the fake clock's current time."""
        return self.clock[0]

    def sleep(self, seconds: float) -> None:
        """Advance the fake clock instead of waiting.

        Args:
            seconds: How far to advance.
        """
        self.clock[0] += seconds

    def run(
        self,
        argv: Sequence[str | Path],
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Dispatch a command to the fake named by its program.

        Args:
            argv: Program and arguments.
            env: Environment the command would receive.
            input: Standard input the command would receive.

        Returns:
            The fake's result.
        """
        args = [str(a) for a in argv]
        self.calls.append(args)
        name = Path(args[0]).name
        return getattr(self, "_" + name)(args[1:], env or {}, input)

    def _git(
        self, args: list[str], env: Mapping[str, str], input: bytes | None
    ) -> subprocess.CompletedProcess[bytes]:
        """Run real git, refusing any repository outside the temp dir."""
        assert args[0] == "-C" and Path(args[1]).resolve(
            strict=True
        ).is_relative_to(self.home.parent.resolve(strict=True)), args
        r = subprocess.run(["git", *args], capture_output=True, env=env)
        return done(r.stdout, r.returncode)

    def _heartbeat(self) -> None:
        """Write a fresh status file, as a healthy poller would."""
        status = self.home / ".local/share/muninn/status.json"
        status.write_text("{}")
        os.utime(status, (self.now(), self.now()))

    def _launchctl(
        self, args: list[str], env: Mapping[str, str], input: bytes | None
    ) -> subprocess.CompletedProcess[bytes]:
        """Model print, kickstart, bootout and bootstrap."""
        if args[0] == "print":
            if self.loaded:
                return done(b"\tpid = %d\n" % self.pid)
            return done(rc=113)
        if args[0] in ("kickstart", "bootstrap") and self._runs_migrator():
            self._migrate()
        if args[0] == "kickstart":
            self.pid += 1
            if self.heartbeat:
                self._heartbeat()
        elif args[0] == "bootout":
            if self.loaded:
                self.loaded = None
        elif args[0] == "bootstrap":
            self.loaded, self.pid = "new", self.pid + 1
            if self.heartbeat:
                self._heartbeat()
        return done()

    def _runs_migrator(self) -> bool:
        """Say whether ``current`` is the release that migrates the store."""
        link = self.home / ".local/lib/muninn/current"
        return bool(
            self.migrates
            and link.is_symlink()
            and str(link.readlink()) == self.migrates
        )

    def _migrate(self) -> None:
        """Model the new poller migrating the store to the newest schema."""
        path = self.home / ".local/share/muninn/muninn.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE IF NOT EXISTS migrated(x)")
        conn.commit()
        conn.execute(f"PRAGMA user_version={store.SCHEMA_VERSION}")
        conn.close()

    def _ps(
        self, args: list[str], env: Mapping[str, str], input: bytes | None
    ) -> subprocess.CompletedProcess[bytes]:
        """Report the pinned release as the running command line."""
        lib = self.home / ".local/lib/muninn"
        cmd = f"python3.13 -c x {lib}/0123abc serve" if self.loaded else ""
        return done(cmd.encode())

    def _muninn(
        self, args: list[str], env: Mapping[str, str], input: bytes | None
    ) -> subprocess.CompletedProcess[bytes]:
        """Model hook, ingest and doctor, rejecting a leaked MUNINN_ROOTS."""
        assert (
            "MUNINN_ROOTS" not in env
        ), "inherited MUNINN_ROOTS reached muninn"
        if args[0] == "hook":
            assert env.get("MUNINN_HOOK_DISABLE") == "1" and input == b"{}"
            return done(b"{}")
        if args[0] == "ingest":
            assert env.get("HOME") == str(self.home), "ingest saw another HOME"
            assert args == ["ingest", "--full"], args
            make_store(Path(env["MUNINN_HOME"]), self.store_version)
            return done()
        if args == ["stats"]:
            assert env.get("HOME") == str(self.home), "stats saw another HOME"
            return done(
                b'{"events_by_provider":{"claude":2,"codex":3,"cursor":1}}'
            )
        assert args == ["doctor"], args
        return done(rc=self.doctor)

    def _codex(
        self, args: list[str], env: Mapping[str, str], input: bytes | None
    ) -> subprocess.CompletedProcess[bytes]:
        """Model `codex --version` and `codex plugin add`."""
        if args == ["--version"]:
            return done(self.version)
        assert args[:2] == ["plugin", "add"], args
        assert env["CODEX_HOME"] == str(self.home / ".codex")
        config = self.home / ".codex/config.toml"
        text = config.read_text()
        keys = co.CODEX_KEYS[co.MARKETPLACE]
        parsed = ce.parse_section(text, co.MARKETPLACE, keys)
        assert parsed is not None
        src = Path(parsed["source"])
        catalog = json.loads(
            (src / ".agents/plugins/marketplace.json").read_text()
        )
        (entry,) = catalog["plugins"]
        plugin = src / entry["source"]["path"]
        manifest = json.loads(
            (plugin / ".codex-plugin/plugin.json").read_text()
        )
        cache = (
            self.home
            / ".codex/plugins/cache"
            / catalog["name"]
            / entry["name"]
        )
        cache.mkdir(parents=True, exist_ok=True)
        # Codex drops every older cached version when it installs a new one.
        for stale in cache.iterdir():
            shutil.rmtree(stale)
        shutil.copytree(plugin, cache / manifest["version"], symlinks=True)
        off, on = (
            f"{co.PLUGIN}\nenabled = false",
            f"{co.PLUGIN}\nenabled = true",
        )
        config.write_text(text.replace(off, on))
        self.toml_writes.append(config.read_text())
        out = {
            "pluginId": co.PLUGIN_ID,
            "version": manifest["version"],
            "installedPath": str(cache / manifest["version"]),
        }
        return done(json.dumps(out).encode())

    def probe(self, ctx: Ctx) -> list[dict[str, Any]]:
        """Shape Codex app-server hooks/list entries for our plugin.

        Args:
            ctx: The install context; unused, the fake reads its own HOME.

        Returns:
            One entry per hook, bent by ``probe_mode``.

        Raises:
            TimeoutError: If ``probe_mode`` is ``error``.
        """
        if self.probe_mode == "error":
            raise TimeoutError
        cache = self.home / ".codex/plugins/cache" / co.MKT_NAME
        (hooks_file,) = cache.glob("muninn/*/hooks/hooks.json")
        text = (self.home / ".codex/config.toml").read_text()
        out: list[dict[str, Any]] = []
        for hook in co.codex_hooks(hooks_file.read_bytes()):
            state = ce.parse_section(
                text, co.TRUST[hook["event"]], {"trusted_hash"}
            )
            status = "untrusted" if state is None else "modified"
            if state and state["trusted_hash"] == hook["hash"]:
                status = "trusted"
            if self.probe_mode == "untrusted":
                status = "untrusted"
            if self.probe_mode == "wrong":
                hook["command"] = "/elsewhere/hooks/codex.py"
            stale = str(hooks_file).replace("0.2.0", "0.1.5+codex.1")
            out.append(
                {
                    "key": f"{co.PLUGIN_ID}:hooks/hooks.json:{hook['suffix']}",
                    "pluginId": co.PLUGIN_ID,
                    "command": hook["command"],
                    "currentHash": hook["hash"],
                    "sourcePath": (
                        stale
                        if self.probe_mode == "stale"
                        else str(hooks_file)
                    ),
                    "trustStatus": status,
                }
            )
        return out


class World:
    """A temp HOME with plain provider configs and a git repo to install."""

    def __init__(self, tc: unittest.TestCase) -> None:
        """Build the HOME and repo, registering their cleanup on ``tc``.

        Args:
            tc: The running test; owns the temp dir's lifetime.
        """
        identity = mock.patch(
            "install.steps_config.codex_identity",
            return_value=("synthetic-codex-candidate",),
        )
        identity.start()
        tc.addCleanup(identity.stop)
        temporary = tempfile.TemporaryDirectory(prefix="inst-")
        tc.addCleanup(temporary.cleanup)
        tmp = Path(temporary.name).resolve()
        self.home, self.repo = tmp / "home", tmp / "repo"
        self.out: list[str] = []
        for rel in PINNED:
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, self.repo / rel)
            if rel == "bin/muninn":
                (self.repo / rel).chmod(0o755)
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "update-index", "--chmod=+x", "bin/muninn")
        git(self.repo, "commit", "-qm", "new")
        self.sha = git(self.repo, "rev-parse", "HEAD").decode().strip()
        h = self.home
        (h / ".claude").mkdir(parents=True)
        (h / ".claude/settings.json").write_bytes(
            dump({"theme": "dark", "env": {"TOKEN": CRED}})
        )
        (h / ".codex").mkdir()
        (h / ".codex/config.toml").write_text(
            f'model = "gpt"\n\n[model_providers.fake]\napi_key = "{CRED}"\n'
        )
        (h / ".codex/config.toml").chmod(0o600)
        (h / ".local/bin").mkdir(parents=True)
        self.fake = Fake(h)

    def ctx(self, **kw: Any) -> Ctx:
        """Build an install context wired to the fakes.

        Args:
            **kw: Extra ``Ctx`` fields such as ``fresh`` or ``dry_run``.

        Returns:
            The context.
        """
        return Ctx(
            home=self.home,
            run=self.fake.run,
            ts="20261001T000000Z",
            uid=501,
            platform="darwin",
            now=self.fake.now,
            sleep=self.fake.sleep,
            say=self.out.append,
            probe=self.fake.probe,
            **kw,
        )


def codex_view(text: str) -> tuple[str, bool, int]:
    """Summarise the Codex config sections the installer owns.

    Args:
        text: Contents of ``config.toml``.

    Returns:
        The marketplace source, whether the plugin is enabled, and how many
        of our trust sections exist.
    """
    src = ce.parse_section(text, co.MARKETPLACE, {"source_type", "source"})
    plugin = ce.parse_section(text, co.PLUGIN, {"enabled"})
    assert src is not None and plugin is not None
    trust = [h for h in co.TRUST.values() if ce.get_section(text, h)]
    return src["source"], plugin["enabled"], len(trust)
