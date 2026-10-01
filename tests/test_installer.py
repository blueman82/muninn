"""Rehearse install, upgrade and rollback in a temp HOME.

launchctl, ps, pctx, codex and the Codex hooks/list probe are fakes;
git and cp run for real, but only on temp dirs. Config files are
synthetic and carry a fake credential that must never be recorded.
"""

import contextlib
import dataclasses
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from install import configedit as ce
from install import installer as co
from install import rollback as rb

ROOT = Path(__file__).resolve().parent.parent
CRED = "sk-fake-" + "feedface" * 5
PINNED = (
    "bin/pctx",
    "integrations/claude/settings-hooks.json",
    "integrations/codex/.agents/plugins/marketplace.json",
    "integrations/codex/.codex-plugin/plugin.json",
    "integrations/codex/hooks/hooks.json",
    "launchd/com.provenance-context.plist",
)
# Real Codex 0.159.2 `hooks/list` currentHash values for the WU10
# hooks.json (literal @HOME@ commands), from an isolated CODEX_HOME probe.
REAL_CODEX = {
    "session_start:0:0": "sha256:6459866a30542db401b65bdccc80ab089242fa46"
    "3dabbabef559874101d755ac",
    "user_prompt_submit:0:0": "sha256:126cc326ec91499dba7a984b45602c9678c"
    "0079b20e8eb84e0751fe93bf99ae0",
}


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t")
    env.update(GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env=env,
    ).stdout


def done(stdout=b"", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, b"")


def dump(obj):
    return (json.dumps(obj, indent=2) + "\n").encode()


class Fake:
    """Stand-ins for launchctl, ps, pctx and codex over a temp HOME."""

    def __init__(self, home):
        self.home = home
        self.calls, self.clock = [], [time.time()]
        self.loaded, self.pid = None, 64653
        self.heartbeat, self.doctor = True, 0
        self.toml_writes, self.probe_mode = [], "ok"
        self.version = b"codex-cli 0.159.2\n"

    def now(self):
        return self.clock[0]

    def sleep(self, seconds):
        self.clock[0] += seconds

    def run(self, argv, env=None, input=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        name = Path(argv[0]).name
        return getattr(self, "_" + name)(argv[1:], env or {}, input)

    def _git(self, args, env, input):
        assert args[0] == "-C" and tempfile.gettempdir() in args[1], args
        r = subprocess.run(["git", *args], capture_output=True, env=env)
        return done(r.stdout, r.returncode)

    def _launchctl(self, args, env, input):
        if args[0] == "print":
            return (
                done(b"\tpid = %d\n" % self.pid)
                if self.loaded
                else done(rc=113)
            )
        if args[0] == "kickstart":
            self.pid += 1
            status = self.home / ".local/share/provenance-context/status.json"
            if self.heartbeat:
                status.write_text("{}")
                os.utime(status, (self.now(), self.now()))
        elif args[0] == "bootout":
            self.loaded = None
        elif args[0] == "bootstrap":
            prog = plistlib.loads(Path(args[2]).read_bytes())[
                "ProgramArguments"
            ]
            new = "/.local/lib/provenance-context/" in prog[0]
            self.loaded, self.pid = ("new" if new else None), self.pid + 1
            status = self.home / ".local/share/provenance-context/status.json"
            if new and self.heartbeat:
                status.write_text("{}")
                os.utime(status, (self.now(), self.now()))
        return done()

    def _ps(self, args, env, input):
        lib = self.home / ".local/lib/provenance-context"
        cmd = f"python3.13 -c x {lib}/0123abc serve" if self.loaded else ""
        return done(cmd.encode())

    def _pctx(self, args, env, input):
        assert "PCTX_ROOTS" not in env, "inherited trial env reached pctx"
        if args[0] == "hook":
            assert env.get("PCTX_HOOK_DISABLE") == "1" and input == b"{}"
            return done(b"{}")
        if args[0] == "ingest":
            (Path(env["PCTX_HOME"]) / "pctx.sqlite").write_bytes(b"store")
            return done()
        assert args == ["doctor"], args
        return done(rc=self.doctor)

    def _codex(self, args, env, input):
        if args == ["--version"]:
            return done(self.version)
        assert args[:2] == ["plugin", "add"], args
        assert env["CODEX_HOME"] == str(self.home / ".codex")
        config = self.home / ".codex/config.toml"
        text = config.read_text()
        keys = co.CODEX_KEYS[co.MARKETPLACE]
        src = Path(ce.parse_section(text, co.MARKETPLACE, keys)["source"])
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
        for stale in cache.iterdir():  # store.rs remove_old_plugin_versions
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

    def probe(self, ctx):
        """Shape of Codex app-server hooks/list entries for our plugin."""
        if self.probe_mode == "error":
            raise TimeoutError
        cache = self.home / ".codex/plugins/cache" / co.MKT_NAME
        (hooks_file,) = cache.glob("provenance-context/*/hooks/hooks.json")
        text = (self.home / ".codex/config.toml").read_text()
        out = []
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

    def __init__(self, tc):
        tmp = Path(tempfile.mkdtemp(prefix="inst-")).resolve()
        tc.addCleanup(shutil.rmtree, tmp)
        self.home, self.repo = tmp / "home", tmp / "repo"
        self.out = []
        for rel in PINNED:
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, self.repo / rel)
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
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

    def ctx(self, **kw):
        return co.Ctx(
            home=self.home,
            run=self.fake.run,
            ts="20261001T000000Z",
            uid=501,
            now=self.fake.now,
            sleep=self.fake.sleep,
            say=self.out.append,
            probe=self.fake.probe,
            **kw,
        )


def codex_view(text):
    """(marketplace source, plugin enabled, number of our trust keys)."""
    src = ce.parse_section(text, co.MARKETPLACE, {"source_type", "source"})
    plugin = ce.parse_section(text, co.PLUGIN, {"enabled"})
    trust = [h for h in co.TRUST.values() if ce.get_section(text, h)]
    return src["source"], plugin["enabled"], len(trust)


class FreshInstallTest(unittest.TestCase):
    """--fresh: a new machine with plain Claude and Codex config."""

    def setUp(self):
        self.w = World(self)
        self.lib = self.w.home / ".local/lib/provenance-context"

    def install(self, **kw):
        return co.install(self.w.ctx(fresh=True, **kw), self.w.repo, self.w.sha)

    def test_install_record_and_rollback_leave_nothing_behind(self):
        w, h = self.w, self.w.home
        before = {
            r: (h / r).read_bytes()
            for r in (".claude/settings.json", ".codex/config.toml")
        }
        rec = self.install()
        self.assertEqual(w.fake.loaded, "new")
        self.assertEqual(os.readlink(self.lib / "python"), sys.executable)
        out = json.loads((self.lib / "install-record.json").read_text())
        self.assertEqual(
            (out["outcome"], out["fresh"], out["sha"], out["python"]["path"]),
            ("ok", True, w.sha, sys.executable),
        )
        self.assertIn("hooks.SessionStart", out["config_keys"])
        self.assertNotIn(CRED, json.dumps(out))
        self.assertEqual(
            (self.lib / "install-record.json").stat().st_mode & 0o777, 0o600
        )
        s = json.loads((h / ".claude/settings.json").read_bytes())
        (group,) = s["hooks"]["SessionStart"]
        self.assertEqual(
            group["hooks"][0]["command"],
            f"{h}/.local/bin/pctx hook session-start --provider claude",
        )
        text = (h / ".codex/config.toml").read_text()
        self.assertEqual(
            codex_view(text),
            (f"{self.lib}/current/integrations/codex", True, 2),
        )
        record = Path(rec["rdir"]) / "rollback-record.json"
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        rb.rollback(w.ctx(), co.load_record(record))
        for rel, data in before.items():
            self.assertEqual((h / rel).read_bytes(), data, rel)
        self.assertFalse((h / ".local/share/provenance-context").exists())
        self.assertFalse((h / co.PLIST).exists())
        self.assertFalse(os.path.lexists(self.lib / "python"))
        self.assertFalse(os.path.lexists(h / ".local/bin/pctx"))

    def test_trust_auto_only_for_verified_codex_versions(self):
        self.w.fake.version = b"codex-cli 9.9.9\n"
        self.assertEqual(self.install()["trust"], "owner")
        self.assertTrue(any("OWNER STEP" in x for x in self.w.out))

    def test_no_claude_or_codex_is_left_unconfigured(self):
        w, h = self.w, self.w.home
        (h / ".claude/settings.json").unlink()
        (h / ".codex/config.toml").unlink()
        rec = self.install()
        self.assertEqual((rec["has_claude"], rec["has_codex"]), (False, False))
        self.assertFalse((h / ".claude/settings.json").exists())
        self.assertTrue(any("left unconfigured" in x for x in w.out))
        out = json.loads((self.lib / "install-record.json").read_text())
        self.assertEqual(out["config_keys"], [])

    def test_refuses_when_already_installed(self):
        h = self.w.home
        (h / ".local/share/provenance-context").mkdir(parents=True)
        with self.assertRaises(co.StepFailed):
            self.install()
        self.assertFalse(self.lib.exists())

    def test_dry_run_changes_nothing_and_touches_no_service(self):
        h = self.w.home
        before = {
            r: (h / r).read_bytes()
            for r in (".claude/settings.json", ".codex/config.toml")
        }
        self.install(dry_run=True)
        for rel, data in before.items():
            self.assertEqual((h / rel).read_bytes(), data, rel)
        self.assertFalse((h / ".local/share/provenance-context").exists())
        self.assertFalse(
            any(c[0] == "launchctl" and c[1] != "print" for c in self.w.fake.calls)
        )

    def test_failed_start_rolls_back_and_records_it(self):
        w, h = self.w, self.w.home
        w.fake.heartbeat = False
        with self.assertRaises(co.StepFailed):
            self.install()
        out = json.loads((self.lib / "install-record.json").read_text())
        self.assertEqual(out["outcome"], "rolled_back")
        self.assertEqual(out["failed"]["step"], "start_new")
        self.assertFalse((h / ".local/share/provenance-context").exists())
        self.assertEqual(w.fake.loaded, None)

    def test_failed_doctor_rolls_back(self):
        self.w.fake.doctor = 1
        with self.assertRaises(co.StepFailed):
            self.install()
        self.assertFalse(
            (self.w.home / ".local/share/provenance-context").exists()
        )

    def test_wrong_codex_answer_rolls_back(self):
        self.w.fake.probe_mode = "wrong"
        with self.assertRaises(co.StepFailed):
            self.install()

    def test_silent_probe_leaves_the_owner_step(self):
        self.w.fake.probe_mode = "error"
        self.assertEqual(self.install()["trust"], "owner")

    def test_home_substituted_in_pinned_copy_never_in_repo(self):
        self.install()
        pinned = self.lib / "current/integrations/claude/settings-hooks.json"
        self.assertNotIn(b"@HOME@", pinned.read_bytes())
        repo = self.w.repo / "integrations/claude/settings-hooks.json"
        self.assertIn(b"@HOME@", repo.read_bytes())

    def test_inherited_pctx_env_never_reaches_pctx(self):
        with mock.patch.dict(os.environ, {"PCTX_ROOTS": "{}"}):
            self.install()  # Fake._pctx asserts PCTX_ROOTS is absent


class UpgradeTest(unittest.TestCase):
    """--upgrade: re-pin a newer commit and keep exactly one release."""

    def setUp(self):
        self.w = w = World(self)
        self.first = co.install(w.ctx(fresh=True), w.repo, w.sha)["sha"]
        (w.repo / "bin/note").write_text("v2\n")
        git(w.repo, "add", "-A")
        git(w.repo, "commit", "-qm", "v2")
        self.sha2 = git(w.repo, "rev-parse", "HEAD").decode().strip()
        self.lib = w.home / ".local/lib/provenance-context"
        self.ctx = dataclasses.replace(
            w.ctx(upgrade=True), ts="20261002T000000Z"
        )

    def releases(self):
        return sorted(
            p.name
            for p in self.lib.iterdir()
            if p.is_dir() and not p.is_symlink()
        )

    def test_one_release_remains_and_config_is_untouched(self):
        w, h = self.w, self.w.home
        self.assertEqual(self.releases(), [self.first])
        configs = {
            r: (h / r).read_bytes()
            for r in (".claude/settings.json", ".codex/config.toml")
        }
        shares = sorted(p.name for p in (h / ".local/share").iterdir())
        co.install(self.ctx, w.repo, self.sha2)
        self.assertEqual(self.releases(), [self.sha2])
        self.assertEqual(os.readlink(self.lib / "current"), self.sha2)
        kick = ["launchctl", "kickstart", "-k", self.ctx.target]
        self.assertIn(kick, w.fake.calls)
        for rel, data in configs.items():
            self.assertEqual((h / rel).read_bytes(), data, rel)
        out = json.loads((self.lib / "install-record.json").read_text())
        self.assertEqual(
            (out["outcome"], out["upgrade"], out["sha"]),
            ("ok", True, self.sha2),
        )
        self.assertEqual(
            sorted(p.name for p in (h / ".local/share").iterdir()),
            shares,  # no leftover record dir
        )

    def test_failed_upgrade_returns_to_the_old_release_and_keeps_running(self):
        w = self.w
        w.fake.heartbeat = False
        w.fake.clock[0] += 1000  # the first install's heartbeat is stale
        with self.assertRaises(co.StepFailed):
            co.install(self.ctx, w.repo, self.sha2)
        self.assertEqual(os.readlink(self.lib / "current"), self.first)
        self.assertTrue((self.lib / self.first).is_dir())
        self.assertEqual(w.fake.loaded, "new")  # the job was not left stopped
        out = json.loads((self.lib / "install-record.json").read_text())
        self.assertEqual(out["outcome"], "rolled_back")

    def test_refuses_when_nothing_is_installed(self):
        (self.lib / "current").unlink()
        with self.assertRaises(co.StepFailed):
            co.install(self.ctx, self.w.repo, self.sha2)


class InstallLogTest(unittest.TestCase):
    def test_log_has_status_and_stderr_but_never_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lib/install.log"

            def run(argv, env=None, input=None):
                return subprocess.CompletedProcess(
                    argv, 3, b"TRANSCRIPT-TEXT", b"boom"
                )

            with contextlib.redirect_stdout(io.StringIO()):
                say, logged = co.install_log(path, run)
                say("hello")
                logged(["launchctl", "bootstrap", "x"])
            text = path.read_text()
            self.assertIn("hello", text)
            self.assertIn("run launchctl bootstrap -> 3", text)
            self.assertIn("stderr: boom", text)
            self.assertNotIn("TRANSCRIPT-TEXT", text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class CliTest(unittest.TestCase):
    def test_a_mode_is_required_and_a_foreign_home_is_dry_run_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = ["--repo", tmp, "--sha", "0" * 40]
            for argv in (base, base + ["--fresh", "--home", tmp]):
                with self.subTest(argv=argv):
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as cm:
                            co.main(argv)
                    self.assertEqual(cm.exception.code, 2)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    rb.main(["--record", str(Path(tmp) / "none.json"), "--x"])


class CodexContractTest(unittest.TestCase):
    def test_hash_matches_real_codex_0_159_2(self):
        data = (ROOT / "integrations/codex/hooks/hooks.json").read_bytes()
        got = {h["suffix"]: h["hash"] for h in co.codex_hooks(data)}
        self.assertEqual(got, REAL_CODEX)

    def test_marketplace_catalog_names_plugin_0_2_0(self):
        root = ROOT / "integrations/codex"
        cat = json.loads(
            (root / ".agents/plugins/marketplace.json").read_text()
        )
        self.assertEqual(cat["name"], co.MKT_NAME)
        (entry,) = cat["plugins"]
        self.assertEqual(entry["name"], "provenance-context")
        self.assertEqual(entry["source"], {"source": "local", "path": "./"})
        manifest = root / entry["source"]["path"] / ".codex-plugin/plugin.json"
        got = json.loads(manifest.read_text())
        self.assertEqual(
            (got["name"], got["version"]), ("provenance-context", "0.2.0")
        )




if __name__ == "__main__":
    unittest.main()
