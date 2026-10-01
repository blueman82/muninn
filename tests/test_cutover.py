"""Rehearse the whole cutover and rollback in a temp HOME (WU11).

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
from install import cutover as co
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
OLD_HASH = {e: "sha256:" + c * 64 for e, c in zip(co.EVENTS, "abc")}
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


def codex_toml(old):
    trust = "".join(
        f'\n{co.TRUST[e]}\ntrusted_hash = "{OLD_HASH[e]}"\n' for e in co.EVENTS
    )
    return (
        f'model = "gpt-test"\n\n[model_providers.fake]\n'
        f'api_key = "{CRED}"\n\n{co.MARKETPLACE}\n'
        f'source_type = "local"\nsource = "{old}"\n\n'
        f'[marketplaces.other]\nsource_type = "local"\nsource = "/x"\n'
        f"{trust}\n{co.PLUGIN}\nenabled = false\n\n"
        f'[plugins."other@m"]\nenabled = true\n'
    )


def claude_settings(old):
    mkt = {"source": {"source": "directory", "path": str(old)}}
    return {
        "model": "opus",
        "env": {"TOKEN": CRED},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "s"}]}]},
        "enabledPlugins": {
            "a@a": True,
            "provenance-context@provenance-context-local": False,
            "coderails@coderails": True,
        },
        "extraKnownMarketplaces": {co.MKT_NAME: mkt, "coderails": {}},
    }


def dump(obj):
    return (json.dumps(obj, indent=2) + "\n").encode()


def snapshot(root):
    """{relative path: (kind, bytes or link target, mode)} for a tree."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(dirpath, name)
            rel = str(p.relative_to(root))
            if p.is_symlink():
                out[rel] = ("link", os.readlink(p), None)
            elif p.is_file():
                out[rel] = ("file", p.read_bytes(), p.stat().st_mode & 0o777)
            else:
                out[rel] = ("dir", None, p.stat().st_mode & 0o777)
    return out


class Fake:
    """Stand-ins for launchctl, ps, pctx and codex over a temp HOME."""

    def __init__(self, home, old):
        self.home, self.old = home, old
        self.calls, self.clock = [], [time.time()]
        self.loaded, self.pid = "old", 64653
        self.heartbeat, self.doctor, self.cutover_doctor = True, 0, 0
        self.toml_writes, self.probe_mode, self.linger = [], "ok", False
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

    def _cp(self, args, env, input):
        assert all(tempfile.gettempdir() in a for a in args[2:]), args
        return done(rc=subprocess.run(["cp", *args]).returncode)

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
            self.loaded, self.pid = ("new" if new else "old"), self.pid + 1
            status = self.home / ".local/share/provenance-context/status.json"
            if new and self.heartbeat:
                status.write_text("{}")
                os.utime(status, (self.now(), self.now()))
        return done()

    def _ps(self, args, env, input):
        lib = self.home / ".local/lib/provenance-context"
        cmd = {
            "old": f"python {self.old}/scripts/context.py serve",
            "new": f"python3.13 -c x {lib}/0123abc serve",
        }.get(self.loaded, "")
        if "-p" in args:
            return done(cmd.encode())
        if self.linger:  # an old-tree process that survives bootout
            cmd += f"\n99 python {self.old}/scripts/context.py serve"
        return done(f"  1 /sbin/launchd\n{self.pid} {cmd}\n".encode())

    def _pctx(self, args, env, input):
        assert "PCTX_ROOTS" not in env, "inherited trial env reached pctx"
        if args[0] == "hook":
            assert env.get("PCTX_HOOK_DISABLE") == "1" and input == b"{}"
            return done(b"{}")
        if args[0] == "ingest":
            (Path(env["PCTX_HOME"]) / "pctx.sqlite").write_bytes(b"new store")
            return done()
        assert args[0] == "doctor", args
        return done(rc=self.cutover_doctor if args[1:] else self.doctor)

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
                hook["command"] = f"{self.old}/hooks/codex.py"
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
    """Temp HOME with synthetic provider configs, old tree and new repo."""

    def __init__(self, tc):
        tmp = Path(tempfile.mkdtemp(prefix="wu11-")).resolve()
        tc.addCleanup(shutil.rmtree, tmp)
        self.home, self.old, self.repo = (
            tmp / "home",
            tmp / "old",
            tmp / "repo",
        )
        self.out = []
        (self.old / "scripts").mkdir(parents=True)
        (self.old / "scripts/context.py").write_text("old\n")
        git(self.old, "init", "-q", "-b", "main")
        git(self.old, "add", "-A")
        git(self.old, "commit", "-qm", "old")
        (self.old / "notes.txt").write_text("untracked\n")
        for rel in PINNED:
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, self.repo / rel)
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "new")
        self.sha = git(self.repo, "rev-parse", "HEAD").decode().strip()
        h, old = self.home, str(self.old)
        mkt = {"source": {"source": "directory", "path": old}}
        known = {co.MKT_NAME: dict(mkt, installLocation=old), "coderails": {}}
        prog = [f"{h}/.local/share/provenance-context/runtime/bin/python"]
        prog += [f"{old}/scripts/context.py", "serve"]
        pcache = ".codex/plugins/cache/provenance-context-local/"
        files = {
            ".claude/settings.json": dump(claude_settings(old)),
            ".claude/plugins/known_marketplaces.json": dump(known),
            ".codex/config.toml": codex_toml(old).encode(),
            co.PLIST: plistlib.dumps(
                {"Label": co.LABEL, "ProgramArguments": prog}
            ),
            ".local/share/provenance-context/context.sqlite": b"old db",
            ".local/share/provenance-context/context.sqlite-wal": b"old wal",
            pcache
            + "provenance-context/0.1.5+codex.1/hooks/hooks.json": b"{}",
            pcache + "plugin-backup-AAAA/provenance-context/x": b"x",
            ".claude/plugins/cache/provenance-context-local/p/0.1.10/x": b"x",
        }
        for rel, data in files.items():
            (h / rel).parent.mkdir(parents=True, exist_ok=True)
            (h / rel).write_bytes(data)
        (h / ".codex/config.toml").chmod(0o600)
        (h / ".local/bin").mkdir(parents=True)
        runtime = h / ".local/share/provenance-context/runtime/bin"
        runtime.mkdir(parents=True)
        (runtime / "python").symlink_to("/opt/homebrew/bin/python3.13")
        self.configs = [
            r for r in files if not r.startswith((".local", pcache))
        ]
        self.configs.remove(
            ".claude/plugins/cache/provenance-context-local/p/0.1.10/x"
        )
        self.before = {r: (h / r).read_bytes() for r in self.configs}
        self.data_before = snapshot(h / ".local/share/provenance-context")
        self.cache_before = snapshot(h / pcache)
        self.fake = Fake(h, self.old)

    def ctx(self, dry_run=False):
        return co.Ctx(
            home=self.home,
            run=self.fake.run,
            ts="20261001T000000Z",
            old_tree=self.old,
            uid=501,
            dry_run=dry_run,
            now=self.fake.now,
            sleep=self.fake.sleep,
            say=self.out.append,
            probe=self.fake.probe,
        )

    def record(self):
        (path,) = self.home.glob(".local/share/*-legacy-*/cutover-record.json")
        return json.loads(path.read_text())


def codex_view(text):
    """(marketplace source, plugin enabled, number of our trust keys)."""
    src = ce.parse_section(text, co.MARKETPLACE, {"source_type", "source"})
    plugin = ce.parse_section(text, co.PLUGIN, {"enabled"})
    trust = [h for h in co.TRUST.values() if ce.get_section(text, h)]
    return src["source"], plugin["enabled"], len(trust)


class RehearsalTest(unittest.TestCase):
    def setUp(self):
        self.w = World(self)

    def assert_restored(self):
        w = self.w
        for rel, data in w.before.items():
            self.assertEqual((w.home / rel).read_bytes(), data, rel)
        self.assertEqual(w.fake.loaded, "old")
        data = w.home / ".local/share/provenance-context"
        self.assertEqual(snapshot(data), w.data_before)
        cache = w.home / ".codex/plugins/cache/provenance-context-local"
        self.assertEqual(snapshot(cache), w.cache_before)
        self.assertFalse(os.path.lexists(w.home / ".local/bin/pctx"))
        self.assertEqual(co.tree_hash(w.ctx()), w.record()["old_tree_hash"])

    def test_full_cutover_then_rollback_restores_every_byte(self):
        w, h = self.w, self.w.home
        rec = co.cutover(w.ctx(), w.repo, w.sha)
        legacy = Path(rec["legacy"])
        self.assertEqual(w.fake.loaded, "new")
        plist = plistlib.loads((h / co.PLIST).read_bytes())
        lib = f"{h}/.local/lib/provenance-context/current"
        self.assertEqual(plist["ProgramArguments"][0], f"{lib}/bin/pctx")
        store = h / ".local/share/provenance-context/pctx.sqlite"
        self.assertEqual(store.read_bytes(), b"new store")
        self.assertEqual(
            (legacy / "data/context.sqlite").read_bytes(), b"old db"
        )
        self.assertTrue((legacy / "snapshot/context.sqlite").is_file())
        record = legacy / "cutover-record.json"
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        s = json.loads((h / ".claude/settings.json").read_bytes())
        start = [
            x["command"]
            for g in s["hooks"]["SessionStart"]
            for x in g["hooks"]
        ]
        self.assertEqual(
            start,
            [f"{h}/.local/bin/pctx hook session-start --provider claude"],
        )
        self.assertEqual(
            s["hooks"]["Stop"], claude_settings(w.old)["hooks"]["Stop"]
        )
        self.assertEqual(list(s["extraKnownMarketplaces"]), ["coderails"])
        want = {"a@a": True, "coderails@coderails": True}
        self.assertEqual(s["enabledPlugins"], want)
        text = (h / ".codex/config.toml").read_text()
        self.assertEqual(
            codex_view(text), (f"{lib}/integrations/codex", True, 2)
        )
        self.assertIsNone(ce.get_section(text, co.TRUST["pre_tool_use"]))
        cache = h / ".codex/plugins/cache" / co.MKT_NAME / "provenance-context"
        self.assertEqual([p.name for p in cache.iterdir()], ["0.2.0"])
        moved = legacy / "codex-plugin-cache/provenance-context/0.1.5+codex.1"
        self.assertTrue(moved.is_dir())
        checks = json.loads((legacy / "verify.json").read_text())["checks"]
        self.assertTrue(checks and all(c["ok"] for c in checks), checks)
        self.assertEqual(rec["trust"], "auto")
        rb.rollback(w.ctx(), co.load_record(record))
        self.assert_restored()

    def test_trust_auto_only_for_verified_codex_versions(self):
        # 0.159.3: live hooks/list currentHash == codex_hooks() (2026-10-01)
        for version, trust in (("0.159.3", "auto"), ("0.160.0", "owner")):
            with self.subTest(version=version):
                w = self.w = World(self)
                w.fake.version = f"codex-cli {version}\n".encode()
                self.assertEqual(
                    co.cutover(w.ctx(), w.repo, w.sha)["trust"], trust
                )

    def test_second_rollback_is_a_no_op(self):
        w = self.w
        rec = co.cutover(w.ctx(), w.repo, w.sha)
        record = co.load_record(Path(rec["legacy"]) / "cutover-record.json")
        self.assertEqual(rb.rollback(w.ctx(), record), [])
        state, calls = snapshot(w.home), len(w.fake.calls)
        self.assertEqual(rb.rollback(w.ctx(), record), [])
        self.assertEqual(snapshot(w.home), state)
        verbs = {c[1] for c in w.fake.calls[calls:] if "launchctl" in c[0]}
        self.assertEqual(verbs, {"print"})
        self.assert_restored()

    def test_record_holds_only_the_named_keys(self):
        w = self.w
        rec = co.cutover(w.ctx(), w.repo, w.sha)
        raw = (Path(rec["legacy"]) / "cutover-record.json").read_text()
        for secret in (
            CRED,
            "gpt-test",
            "model_providers",
            "coderails",
            "opus",
        ):
            self.assertNotIn(secret, raw)
        self.assertNotIn(CRED, "\n".join(w.out))
        saved = json.loads(raw)
        paths = {tuple(e["path"]) for e in saved["claude"]["settings"]}
        self.assertEqual(
            paths,
            {
                ("hooks", "SessionStart"),
                ("hooks", "UserPromptSubmit"),
                ("extraKnownMarketplaces", co.MKT_NAME),
                ("enabledPlugins", co.PLUGIN_ID),
            },
        )
        known = [tuple(e["path"]) for e in saved["claude"]["known"]]
        self.assertEqual(known, [(co.MKT_NAME,)])
        headers = {e["header"] for e in saved["codex"]}
        self.assertEqual(
            headers, {co.MARKETPLACE, co.PLUGIN, *co.TRUST.values()}
        )

    def test_dry_run_changes_nothing_and_touches_no_service(self):
        w = self.w
        before = snapshot(w.home)
        co.cutover(w.ctx(dry_run=True), w.repo, w.sha)
        self.assertEqual(snapshot(w.home), before)
        self.assertEqual({Path(c[0]).name for c in w.fake.calls}, {"git"})
        self.assertTrue(any(line.startswith("DRY-RUN") for line in w.out))
        self.assertNotIn(CRED, "\n".join(w.out))

    def test_stale_heartbeat_rolls_back(self):
        self.w.fake.heartbeat = False
        with self.assertRaises(co.StepFailed):
            co.cutover(self.w.ctx(), self.w.repo, self.w.sha)
        self.assertEqual(self.w.record()["failed"]["step"], "start_new")
        self.assert_restored()

    def test_old_process_surviving_bootout_rolls_back(self):
        self.w.fake.linger = True
        with self.assertRaises(co.StepFailed):
            co.cutover(self.w.ctx(), self.w.repo, self.w.sha)
        self.assertEqual(self.w.record()["failed"]["step"], "stop_old")
        self.assert_restored()

    def test_prebuild_doctor_failure_rolls_back(self):
        self.w.fake.doctor = 1
        with self.assertRaises(co.StepFailed):
            co.cutover(self.w.ctx(), self.w.repo, self.w.sha)
        self.assertEqual(self.w.record()["failed"]["step"], "prebuild")
        self.assert_restored()

    def test_cutover_doctor_failure_rolls_back_everything(self):
        self.w.fake.cutover_doctor = 1
        with self.assertRaises(co.StepFailed):
            co.cutover(self.w.ctx(), self.w.repo, self.w.sha)
        self.assertEqual(self.w.record()["failed"]["step"], "verify")
        self.assert_restored()

    def test_unexpected_toml_layout_refuses_before_any_change(self):
        w = self.w
        cfg = w.home / ".codex/config.toml"
        cfg.write_text(
            cfg.read_text().replace('source_type = "local"', "x = 1")
        )
        before = snapshot(w.home)
        with self.assertRaises(ce.Refused):
            co.cutover(w.ctx(), w.repo, w.sha)
        self.assertEqual(snapshot(w.home), before)
        self.assertEqual({Path(c[0]).name for c in w.fake.calls}, {"git"})

    def test_codex_edits_follow_a6_order(self):
        w, real = self.w, ce.edit_file

        def spy(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if path.name == "config.toml":
                w.fake.toml_writes.append(path.read_text())
            return result

        with mock.patch.object(ce, "edit_file", spy):
            co.cutover(w.ctx(), w.repo, w.sha)
        new = f"{w.home}/.local/lib/provenance-context/current"
        new += "/integrations/codex"
        order = [codex_view(t) for t in w.fake.toml_writes]
        expected = [(str(w.old), False, 0), (new, False, 0), (new, True, 0)]
        expected += [(new, True, 0), (new, True, 2)]
        self.assertEqual(order, expected)

    def test_concurrent_settings_edit_survives_cutover_and_rollback(self):
        w, real, hit = self.w, ce._read, []
        settings = w.home / ".claude/settings.json"

        def racing(path):
            data = real(path)
            if path == settings and not hit:
                hit.append(1)
                path.write_bytes(dump(dict(json.loads(data), theme="dark")))
            return data

        with mock.patch.object(ce, "_read", racing):
            rec = co.cutover(w.ctx(), w.repo, w.sha)
        after = json.loads(settings.read_bytes())
        self.assertEqual(after["theme"], "dark")
        self.assertIn("SessionStart", after["hooks"])
        rb.rollback(
            w.ctx(),
            co.load_record(Path(rec["legacy"]) / "cutover-record.json"),
        )
        expected = dict(claude_settings(str(w.old)), theme="dark")
        self.assertEqual(json.loads(settings.read_bytes()), expected)

    def test_home_substituted_in_pinned_copy_never_in_repo(self):
        w = self.w
        co.cutover(w.ctx(), w.repo, w.sha)
        pin = w.home / ".local/lib/provenance-context/current"
        for rel in PINNED[1:]:
            self.assertNotIn(b"@HOME@", (pin / rel).read_bytes(), rel)
        self.assertIn(b"@HOME@", (w.repo / PINNED[-1]).read_bytes())
        self.assertEqual(git(w.repo, "status", "--porcelain"), b"")
        plist = plistlib.loads((pin / PINNED[-1]).read_bytes())
        log = f"{w.home}/.local/share/provenance-context/poller.log"
        self.assertEqual(plist["StandardOutPath"], log)

    def test_residue_is_listed_never_deleted(self):
        w = self.w
        co.cutover(w.ctx(), w.repo, w.sha)
        before = snapshot(w.home)
        listed = {Path(i["path"]).name for i in rb.residue(w.home)}
        self.assertEqual(snapshot(w.home), before)
        expected = {"provenance-context-legacy-20261001T000000Z", "snapshot"}
        expected |= {"data", "codex-plugin-cache", "provenance-context-local"}
        self.assertLessEqual(expected, listed)

    def test_untrusted_or_silent_probe_leaves_owner_step(self):
        for mode in ("untrusted", "error"):
            with self.subTest(mode):
                w = World(self)
                w.fake.probe_mode = mode
                rec = co.cutover(w.ctx(), w.repo, w.sha)
                self.assertEqual(
                    (rec["trust"], w.fake.loaded), ("owner", "new")
                )
                self.assertTrue(any("OWNER STEP" in line for line in w.out))

    def test_wrong_codex_answer_rolls_back(self):
        for mode in ("wrong", "stale"):
            with self.subTest(mode):
                self.w = World(self)
                self.w.fake.probe_mode = mode
                with self.assertRaises(co.StepFailed):
                    co.cutover(self.w.ctx(), self.w.repo, self.w.sha)
                self.assert_restored()

    def test_inherited_pctx_env_never_reaches_pctx(self):
        trial = {"PCTX_ROOTS": '{"codex": "/trial"}', "PCTX_HOME": "/trial"}
        with mock.patch.dict(os.environ, trial):
            co.cutover(self.w.ctx(), self.w.repo, self.w.sha)
        homes = {c[0] for c in self.w.fake.calls if c[0].endswith("pctx")}
        self.assertTrue(homes)


class CliTest(unittest.TestCase):
    def test_foreign_home_is_dry_run_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["--repo", tmp, "--sha", "0" * 40, "--home", tmp]
            with self.assertRaises(SystemExit) as cm:
                co.main(argv)
            self.assertEqual(cm.exception.code, 2)
            with self.assertRaises(SystemExit):
                rb.main(
                    ["--record", str(Path(tmp) / "none.json"), "--residue"]
                )


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


class FreshInstallTest(unittest.TestCase):
    """--fresh: a machine with no legacy daemon, data or Claude/Codex setup
    beyond whatever plain config the colleague already has."""

    def setUp(self):
        self.w = w = World(self)
        h = w.home
        shutil.rmtree(h / ".local/share/provenance-context")
        for gone in (co.PLIST, ".claude/plugins/known_marketplaces.json"):
            (h / gone).unlink()
        shutil.rmtree(h / ".codex/plugins")
        (h / ".claude/settings.json").write_bytes(dump({"theme": "dark"}))
        (h / ".codex/config.toml").write_text('model = "gpt"\n')
        (h / ".codex/config.toml").chmod(0o600)
        w.fake.loaded = None
        self.ctx = lambda **kw: dataclasses.replace(
            w.ctx(**kw), fresh=True, old_tree=w.old
        )

    def test_install_record_and_rollback_leave_nothing_behind(self):
        w, h = self.w, self.w.home
        before = {
            r: (h / r).read_bytes()
            for r in (".claude/settings.json", ".codex/config.toml")
        }
        rec = co.cutover(self.ctx(), w.repo, w.sha)
        self.assertEqual(w.fake.loaded, "new")
        self.assertIsNone(rec["old_tree_hash"])
        lib = h / ".local/lib/provenance-context"
        self.assertEqual(os.readlink(lib / "python"), sys.executable)
        out = json.loads((lib / "install-record.json").read_text())
        self.assertEqual(
            (out["outcome"], out["fresh"], out["sha"], out["python"]["path"]),
            ("ok", True, w.sha, sys.executable),
        )
        self.assertIn("hooks.SessionStart", out["config_keys"])
        self.assertNotIn(CRED, json.dumps(out))
        self.assertEqual((lib / "install-record.json").stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(
            (h / ".claude/settings.json").read_bytes(),
            before[".claude/settings.json"],
        )
        rb.rollback(w.ctx(), co.load_record(Path(rec["legacy"]) / "cutover-record.json"))
        for rel, data in before.items():
            self.assertEqual((h / rel).read_bytes(), data, rel)
        self.assertFalse((h / ".local/share/provenance-context").exists())
        self.assertFalse((h / co.PLIST).exists())
        self.assertFalse(os.path.lexists(lib / "python"))
        self.assertFalse(os.path.lexists(h / ".local/bin/pctx"))

    def test_no_claude_or_codex_is_left_unconfigured(self):
        w, h = self.w, self.w.home
        (h / ".claude/settings.json").unlink()
        (h / ".codex/config.toml").unlink()
        rec = co.cutover(self.ctx(), w.repo, w.sha)
        self.assertEqual((rec["has_claude"], rec["has_codex"]), (False, False))
        self.assertFalse((h / ".claude/settings.json").exists())
        self.assertTrue(any("left unconfigured" in x for x in w.out))
        out = json.loads(
            (h / ".local/lib/provenance-context/install-record.json").read_text()
        )
        self.assertEqual(out["config_keys"], [])

    def test_refuses_when_already_installed(self):
        w, h = self.w, self.w.home
        (h / ".local/share/provenance-context").mkdir(parents=True)
        with self.assertRaises(co.StepFailed):
            co.cutover(self.ctx(), w.repo, w.sha)
        self.assertFalse((h / ".local/lib/provenance-context").exists())

    def test_failed_verify_rolls_back_and_records_it(self):
        w, h = self.w, self.w.home
        w.fake.heartbeat = False
        with self.assertRaises(co.StepFailed):
            co.cutover(self.ctx(), w.repo, w.sha)
        out = json.loads(
            (h / ".local/lib/provenance-context/install-record.json").read_text()
        )
        self.assertEqual(out["outcome"], "rolled_back")
        self.assertEqual(out["failed"]["step"], "start_new")
        self.assertFalse((h / ".local/share/provenance-context").exists())


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


class UpgradeTest(unittest.TestCase):
    """--upgrade: re-pin a newer commit and keep exactly one release."""

    def setUp(self):
        self.w = w = World(self)
        self.first = co.cutover(w.ctx(), w.repo, w.sha)["sha"]
        (w.repo / "bin/note").write_text("v2\n")
        git(w.repo, "add", "-A")
        git(w.repo, "commit", "-qm", "v2")
        self.sha2 = git(w.repo, "rev-parse", "HEAD").decode().strip()
        self.lib = w.home / ".local/lib/provenance-context"
        self.ctx = dataclasses.replace(
            w.ctx(), upgrade=True, ts="20261002T000000Z"
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
        co.cutover(self.ctx, w.repo, self.sha2)
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

    def test_failed_upgrade_returns_to_the_old_release(self):
        w = self.w
        w.fake.heartbeat = False
        w.fake.clock[0] += 1000  # the first install's heartbeat is stale
        with self.assertRaises(co.StepFailed):
            co.cutover(self.ctx, w.repo, self.sha2)
        self.assertEqual(os.readlink(self.lib / "current"), self.first)
        self.assertTrue((self.lib / self.first).is_dir())
        out = json.loads((self.lib / "install-record.json").read_text())
        self.assertEqual(out["outcome"], "rolled_back")

    def test_refuses_when_nothing_is_installed(self):
        w = self.w
        (self.lib / "current").unlink()
        with self.assertRaises(co.StepFailed):
            co.cutover(self.ctx, w.repo, self.sha2)
