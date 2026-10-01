"""Live cutover from the legacy provenance-context daemon to pctx (§6.3).

Run from the new repo root, never from the old tree:
  python3.13 -E -s -B -m install.cutover \\
      --repo DIR --sha SHA [--expect-old-tree-hash H] [--dry-run]
Any failure after the record is written runs install.rollback. Every
external effect goes through ctx.run (launchctl, ps, git, cp, pctx,
codex), so tests rehearse the whole sequence in a temp HOME.
"""

import argparse
import dataclasses
import hashlib
import io
import json
import os
import plistlib
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

from install import configedit as ce

LABEL = "com.provenance-context"
# The legacy daemon tree to retire; PCTX_OLD_TREE names it. Unset, it is a
# path that never exists, so fresh installs and upgrades never match it.
OLD_TREE = Path(os.environ.get("PCTX_OLD_TREE") or "/nonexistent/old-tree")
MKT_NAME = "provenance-context-local"
PLUGIN_ID = f"provenance-context@{MKT_NAME}"
MARKETPLACE = f"[marketplaces.{MKT_NAME}]"
PLUGIN = f'[plugins."{PLUGIN_ID}"]'
EVENTS = ("pre_tool_use", "session_start", "user_prompt_submit")
TRUST = {
    e: f'[hooks.state."{PLUGIN_ID}:hooks/hooks.json:{e}:0:0"]' for e in EVENTS
}
CODEX_KEYS = {
    MARKETPLACE: {"source_type", "source", "last_updated", "last_revision"},
    PLUGIN: {"enabled"},
    **{h: {"enabled", "trusted_hash"} for h in TRUST.values()},
}
MARKERS = (MKT_NAME, "provenance-context@")
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit")
# trust-hash port checked: 0.159.2 source+binary, 0.159.3 live currentHash
CODEX_VERIFIED = ("codex-cli 0.159.2", "codex-cli 0.159.3")
PLIST = "Library/LaunchAgents/com.provenance-context.plist"
HEARTBEAT_S = 120
PINNED = (
    "integrations/claude/settings-hooks.json",
    "integrations/codex/.agents/plugins/marketplace.json",
    "integrations/codex/.codex-plugin/plugin.json",
    "integrations/codex/hooks/hooks.json",
    "launchd/com.provenance-context.plist",
)
OWNER_STEP = (
    "OWNER STEP: start a new Codex session, run /hooks and trust the two "
    "provenance-context hooks (session_start, user_prompt_submit)."
)
GIT_ENV = dict(os.environ, GIT_OPTIONAL_LOCKS="0")


class StepFailed(Exception):
    """A cutover step could not complete; rollback follows."""


@dataclasses.dataclass
class Ctx:
    home: Path
    run: object  # run(argv, env=None, input=None) -> CompletedProcess
    ts: str
    old_tree: Path = OLD_TREE
    uid: int = os.getuid()
    dry_run: bool = False
    now: object = time.time
    sleep: object = time.sleep
    say: object = print
    probe: object = None  # probe(ctx) -> Codex hooks/list entries
    fresh: bool = False  # a machine with no legacy daemon, data or tree
    upgrade: bool = False  # already on pctx: re-pin, restart, prune

    def __post_init__(self):
        h, share = self.home, self.home / ".local/share"
        self.data = share / "provenance-context"
        self.new_data = share / "provenance-context.new"
        self.legacy = share / f"provenance-context-legacy-{self.ts}"
        self.failed = share / f"provenance-context-failed-{self.ts}"
        self.lib = h / ".local/lib/provenance-context"
        self.pctx = h / ".local/bin/pctx"
        self.plist = h / PLIST
        self.settings = h / ".claude/settings.json"
        self.known = h / ".claude/plugins/known_marketplaces.json"
        self.codex_home = h / ".codex"
        self.config = h / ".codex/config.toml"
        self.cache = (
            h / ".codex/plugins/cache" / MKT_NAME / "provenance-context"
        )
        self.target = f"gui/{self.uid}/{LABEL}"


def run_real(argv, env=None, input=None):
    argv = [str(a) for a in argv]
    return subprocess.run(
        argv, env=env, input=input, capture_output=True, timeout=600
    )


def install_log(path, run=run_real):
    """(say, run) that also append to `path` (0600, timestamped): messages,
    each command's name and exit status, a failed command's stderr tail.
    Never stdout (it can carry transcript paths or text)."""

    def say(text):
        print(text)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(f"{stamp} {text}\n")

    def logged(argv, env=None, input=None):
        r = run(argv, env=env, input=input)
        name = " ".join(str(a) for a in argv[:2])
        say(f"run {name} -> {r.returncode}")
        if r.returncode:
            say("  stderr: " + r.stderr.decode(errors="replace")[-300:])
        return r

    return say, logged


def must(ctx, argv, env=None, input=None, quiet=False):
    """Run and require exit 0; `quiet` keeps stderr out of the error."""
    r = ctx.run(argv, env=env, input=input)
    if r.returncode:
        tail = "" if quiet else r.stderr.decode(errors="replace")[-300:]
        name = f"{Path(str(argv[0])).name} {argv[1]}"
        raise StepFailed(f"{name} exited {r.returncode} {tail}".strip())
    return r


def wait(ctx, cond, seconds, what):
    deadline = ctx.now() + seconds
    while not cond():
        if ctx.now() >= deadline:
            raise StepFailed(what)
        ctx.sleep(1)


def job(ctx):
    """None when the label is not loaded, else {'pid', 'cmd'}."""
    r = ctx.run(["launchctl", "print", ctx.target])
    if r.returncode:
        return None
    m = re.search(rb"^\s*pid = (\d+)\s*$", r.stdout, re.M)
    if not m:
        return {"pid": None, "cmd": ""}
    ps = ctx.run(["ps", "-ww", "-o", "command=", "-p", m[1].decode()])
    return {
        "pid": int(m[1]),
        "cmd": ps.stdout.decode(errors="replace").strip(),
    }


def is_new(ctx, j):
    return bool(j and j["pid"] and f"{ctx.lib}/" in j["cmd"])


def old_procs(ctx):
    """Pids of processes whose argv runs old-tree code."""
    out = must(ctx, ["ps", "-ww", "-axo", "pid=,command="]).stdout
    code = [
        f"{ctx.old_tree}/{d}/" for d in ("scripts", "hooks", "claude-code")
    ]
    bad = []
    for line in out.decode(errors="replace").splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid != str(os.getpid()) and any(c in cmd for c in code):
            bad.append(pid)
    return bad


def tree_hash(ctx):
    """evals.json E1's hash of the old tree, computed read-only."""

    def git(*args):
        argv = ["git", "-C", ctx.old_tree, *args]
        return must(ctx, argv, env=GIT_ENV).stdout

    h = hashlib.sha256(
        git("symbolic-ref", "--short", "HEAD")
        + git("rev-parse", "HEAD")
        + git("diff", "HEAD", "--binary")
    )
    for name in git("ls-files", "-o", "--exclude-standard", "-z").split(b"\0"):
        if name and b"__pycache__" not in name and not name.endswith(b"/"):
            with open(ctx.old_tree / os.fsdecode(name), "rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            h.update(digest.encode() + b" *" + name + b"\n")
    return h.hexdigest()


LABELS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "user_prompt_submit",
    "PreToolUse": "pre_tool_use",
}


def codex_hooks(data: bytes) -> list:
    """Each command hook in a hooks.json with its Codex trust hash.

    Port of rust-v0.159.2 hooks/src/engine/discovery.rs:505-566 (handler
    normalisation; timeout default :762), :769-792 (hook_hash) and
    config/src/fingerprint.rs:54-84 (sha256 of key-sorted compact
    JSON). None fields are dropped
    (toml 0.9.11 table.rs:385-401); UserPromptSubmit has no matcher
    (hooks/src/events/common.rs:112-128). Checked against real Codex
    0.159.2 hooks/list output in tests.
    """
    out = []
    for event, groups in json.loads(data)["hooks"].items():
        for gi, group in enumerate(groups):
            for hi, h in enumerate(group["hooks"]):
                if h.get("type") != "command" or event not in LABELS:
                    raise StepFailed(f"unsupported Codex hook in {event}")
                handler = {
                    "type": "command",
                    "command": h["command"],
                    "timeout": max(h.get("timeout", 600), 1),
                    "async": h.get("async", False),
                }
                if h.get("statusMessage") is not None:
                    handler["statusMessage"] = h["statusMessage"]
                if h.get("additionalContextLimit") not in (None, 2500):
                    handler["additionalContextLimit"] = h[
                        "additionalContextLimit"
                    ]
                ident = {"event_name": LABELS[event], "hooks": [handler]}
                matcher = group.get("matcher")
                if event != "UserPromptSubmit" and matcher is not None:
                    ident["matcher"] = matcher
                blob = json.dumps(
                    ident,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                digest = hashlib.sha256(blob.encode()).hexdigest()
                out.append(
                    {
                        "event": LABELS[event],
                        "suffix": f"{LABELS[event]}:{gi}:{hi}",
                        "command": h["command"],
                        "hash": f"sha256:{digest}",
                    }
                )
    return out


# ---- config transforms: pure, bytes/text in and out ---------------------


def ours(group, old):
    """A settings hook group that runs pctx or old-tree code."""
    cmds = " ".join(h.get("command", "") for h in group.get("hooks", []))
    return "/.local/bin/pctx hook" in cmds or str(old) in cmds


def claude_paths(obj):
    """The only settings.json keys the cutover may change."""
    plugins = obj.get("enabledPlugins")
    plugins = plugins if isinstance(plugins, dict) else {}
    mine = [k for k in plugins if k.split("@")[0] == "provenance-context"]
    return (
        [("hooks", e) for e in CLAUDE_EVENTS]
        + [("extraKnownMarketplaces", MKT_NAME)]
        + [("enabledPlugins", k) for k in mine]
    )


def edit_settings(data, fragment, old):
    obj = ce.load_json(data)
    for event in CLAUDE_EVENTS:
        current = ce.jget(obj, ("hooks", event))[1] or []
        if not isinstance(current, list):
            raise ce.Refused(f"settings.json hooks.{event} is not a list")
        keep = [g for g in current if not ours(g, old)]
        ce.jset(obj, ("hooks", event), keep + fragment[event])
    for path in claude_paths(obj)[2:]:
        value = ce.jget(obj, path)[1]
        if path[0] == "enabledPlugins" or str(old) in json.dumps(value):
            ce.jdel(obj, path)
    if str(old) in json.dumps(obj.get("extraKnownMarketplaces")):
        raise ce.Refused(
            "settings.json: another marketplace names the old tree"
        )
    return ce.dump_like(data, obj)


def edit_known(data, old):
    obj = ce.load_json(data)
    if str(old) in json.dumps(ce.jget(obj, (MKT_NAME,))[1]):
        ce.jdel(obj, (MKT_NAME,))
    if str(old) in json.dumps(obj):
        raise ce.Refused(
            "known_marketplaces.json: an entry still names the old tree"
        )
    return ce.dump_like(data, obj)


def codex_scan(text):
    return ce.scan_named(text, CODEX_KEYS, MARKERS)


def codex_check(before, after):
    ce.toml_check(before.decode(), after.decode(), CODEX_KEYS, MARKERS)
    codex_scan(after.decode())


def set_line(text, header, key, value):
    """Set one `key = value` line in a named section (added if absent)."""
    codex_scan(text)
    raw, line = ce.get_section(text, header), f"{key} = {value}\n"
    if raw is None:
        return ce.put_section(text, header, f"\n{header}\n{line}")
    lines = raw.splitlines(keepends=True)
    hits = [i for i, ln in enumerate(lines) if re.match(rf"\s*{key}\s*=", ln)]
    if hits:
        lines[hits[0]] = line
    else:
        head = next(i for i, ln in enumerate(lines) if ln.strip() == header)
        lines.insert(head + 1, line)
    return ce.put_section(text, header, "".join(lines))


def drop_trust(text):
    codex_scan(text)
    for header in TRUST.values():
        text = ce.put_section(text, header, None)
    return text


def repoint(text, source):
    mkt = codex_scan(text)[MARKETPLACE]
    if mkt is None:
        text = set_line(text, MARKETPLACE, "source_type", '"local"')
    elif mkt.get("source_type") != "local":
        raise ce.Refused(f"{MARKETPLACE}: source_type is not local")
    return set_line(text, MARKETPLACE, "source", json.dumps(source))


def enable(text):
    return set_line(text, PLUGIN, "enabled", "true")


def write_trust(text, hooks):
    codex_scan(text)
    for hook in hooks:
        if not hook["suffix"].endswith(":0:0"):
            raise ce.Refused(f"no trust key for hook {hook['suffix']}")
        header = TRUST[hook["event"]]
        block = f'\n{header}\ntrusted_hash = "{hook["hash"]}"\n'
        text = ce.put_section(text, header, block)
    return text


# ---- record ---------------------------------------------------------------


def json_entry(obj, path):
    present, value, index = ce.jget(obj, path)
    entry = {"path": list(path), "present": present}
    entry["parent_present"] = ce.jget(obj, path[:-1])[0] if path[:-1] else True
    if present:
        entry.update(value=value, index=index)
    return entry


def codex_record(text):
    """Raw before-blocks of our named sections only, in file order.

    `at` is a table index, so no other section's name is recorded.
    """
    codex_scan(text)
    out = [
        {
            "header": h,
            "raw": ce.get_section(text, h),
            "at": ce.index_of(text, h),
        }
        for h in CODEX_KEYS
    ]
    return sorted(out, key=lambda e: len(text) if e["at"] is None else e["at"])


def save(ctx, rec):
    data = json.dumps(rec, indent=1).encode()
    ce.atomic_write(ctx.legacy / "cutover-record.json", data, 0o600)


def install_record(ctx, rec, outcome):
    """lib/install-record.json: what was installed and which config keys it
    touched (names only, no values), for rollback and support."""
    if ctx.dry_run:
        return
    ctx.lib.mkdir(parents=True, exist_ok=True)
    keys = [
        ".".join(e["path"])
        for e in rec["claude"]["settings"]
        if rec["has_claude"]
    ]
    keys += [e["header"] for e in rec["codex"] if rec["has_codex"]]
    out = {
        k: rec.get(k)
        for k in (
            "fresh",
            "upgrade",
            "ts",
            "sha",
            "repo",
            "python",
            "steps",
            "failed",
        )
    }
    out |= {
        "outcome": outcome,
        "config_keys": keys,
        "record": (
            None
            if rec.get("record_removed")
            else str(ctx.legacy / "cutover-record.json")
        ),
    }
    ce.atomic_write(
        ctx.lib / "install-record.json",
        json.dumps(out, indent=1).encode(),
        0o600,
    )


def load_record(path):
    return json.loads(Path(path).read_text())


def preflight(ctx, repo, sha, expect_tree):
    """Check everything and dry-apply every config edit; change nothing."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise StepFailed("--sha must be a full 40-hex commit id")
    if repo.resolve() == ctx.old_tree.resolve():
        raise StepFailed("--repo is the old tree")

    def rgit(*args):
        return must(ctx, ["git", "-C", repo, *args], env=GIT_ENV).stdout

    if rgit("rev-parse", "HEAD").decode().strip() != sha:
        raise StepFailed("repo HEAD is not --sha")
    if rgit("status", "--porcelain", "--untracked-files=all"):
        raise StepFailed("repo worktree is not clean")
    if not rgit("ls-tree", sha, "bin/pctx").startswith(b"100755 "):
        raise StepFailed("bin/pctx is not an executable file at --sha")
    home = str(ctx.home).encode()
    files = {
        r: rgit("show", f"{sha}:{r}").replace(b"@HOME@", home) for r in PINNED
    }
    fragment = json.loads(files[PINNED[0]])["hooks"]
    if set(fragment) != set(CLAUDE_EVENTS):
        raise StepFailed(
            "Claude hook fragment must be SessionStart+UserPromptSubmit"
        )
    tree = None if ctx.fresh or ctx.upgrade else tree_hash(ctx)
    if ctx.upgrade:
        if not (ctx.data.is_dir() and ctx.plist.exists()):
            raise StepFailed("nothing to upgrade: no data dir or plist")
        if not (ctx.lib / "current").is_symlink():
            raise StepFailed("nothing to upgrade: no current release")
    if ctx.fresh:
        if sys.version_info < (3, 13):
            raise StepFailed("the installer needs Python 3.13+")
        for path in (ctx.data, ctx.plist):
            if os.path.lexists(path):
                raise StepFailed(f"{path} exists: already installed")
    if expect_tree and tree != expect_tree:
        raise StepFailed("old tree hash differs from the expected baseline")
    for path in (ctx.legacy, ctx.new_data, ctx.failed):
        if os.path.lexists(path):
            raise StepFailed(f"{path} already exists")
    if ctx.pctx.exists() and not ctx.pctx.is_symlink():
        raise StepFailed(f"{ctx.pctx} is not a symlink")
    touch = not ctx.upgrade  # an upgrade leaves provider config alone
    if touch and ctx.settings.exists():
        edit_settings(ctx.settings.read_bytes(), fragment, ctx.old_tree)
    if touch and ctx.known.exists():
        edit_known(ctx.known.read_bytes(), ctx.old_tree)
    if touch and ctx.config.exists():
        text = ctx.config.read_text()
        hooks = codex_hooks(files["integrations/codex/hooks/hooks.json"])
        text = drop_trust(text)
        text = enable(repoint(text, f"{ctx.lib}/current/integrations/codex"))
        write_trust(text, hooks)
    return {
        "fresh": ctx.fresh,
        "upgrade": ctx.upgrade,
        "has_claude": touch and ctx.settings.exists(),
        "has_codex": touch and ctx.config.exists(),
        "schema": 1,
        "ts": ctx.ts,
        "home": str(ctx.home),
        "repo": str(repo),
        "sha": sha,
        "old_tree": str(ctx.old_tree),
        "old_tree_hash": tree,
        "python": {"path": sys.executable, "version": sys.version.split()[0]},
        "legacy": str(ctx.legacy),
        "steps": [],
    }


# ---- steps ------------------------------------------------------------------


def dry(ctx, text):
    if ctx.dry_run:
        ctx.say(f"DRY-RUN {text}")
    return ctx.dry_run


def record(ctx, rec):
    """Step 1: launchd state, old plist hash, before-values of our keys."""
    settings = (
        ce.load_json(ctx.settings.read_bytes()) if rec["has_claude"] else {}
    )
    known = ce.load_json(ctx.known.read_bytes()) if ctx.known.exists() else {}
    rec["claude"] = {
        "settings": [json_entry(settings, p) for p in claude_paths(settings)],
        "known": (
            [json_entry(known, (MKT_NAME,))] if ctx.known.exists() else []
        ),
    }
    rec["codex"] = (
        codex_record(ctx.config.read_text()) if rec["has_codex"] else []
    )
    links = (ctx.lib / "current", ctx.lib / "python", ctx.pctx)
    rec["links"] = {
        str(p): os.readlink(p) if p.is_symlink() else None for p in links
    }
    plist = ctx.plist.read_bytes() if ctx.plist.exists() else None
    rec["old_plist"] = plist and hashlib.sha256(plist).hexdigest()
    keys = [".".join(e["path"]) for e in rec["claude"]["settings"]]
    keys += [e["header"] for e in rec["codex"]]
    if dry(ctx, f"record launchd {ctx.target}, plist sha256, keys {keys}"):
        return
    rec["launchd"] = job(ctx)
    try:
        version = ctx.run(["codex", "--version"]).stdout.decode().strip()
    except OSError:  # no codex on this machine
        version = ""
    rec["trust"] = "auto" if version in CODEX_VERIFIED else "owner"
    ctx.legacy.mkdir(mode=0o700)
    if plist is not None:
        ce.atomic_write(
            ctx.legacy / "com.provenance-context.plist", plist, 0o600
        )
    save(ctx, rec)


def relink(link, target, ts):
    """Point `link` at `target` with one atomic rename."""
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.{ts}")
    os.symlink(target, tmp)
    os.replace(tmp, link)


def pin(ctx, rec):
    """Step 2: git archive into lib/<sha>, substitute @HOME@, relink."""
    sha, dest = rec["sha"], ctx.lib / rec["sha"]
    if dry(ctx, f"pin {sha} -> {dest}; current, python, pctx relinked"):
        return
    argv = ["git", "-C", rec["repo"], "archive", "--format=tar", sha]
    tar = must(ctx, argv, env=GIT_ENV).stdout
    tmp = ctx.lib / f".{sha}.tmp-{ctx.ts}"
    tmp.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
        tf.extractall(tmp, filter="data")
    for sub in ("integrations", "launchd"):  # never in the repo itself
        for path in (tmp / sub).rglob("*"):
            data = (
                b""
                if path.is_symlink() or not path.is_file()
                else path.read_bytes()
            )
            if b"@HOME@" in data:
                path.write_bytes(
                    data.replace(b"@HOME@", str(ctx.home).encode())
                )
    if dest.exists():
        os.rename(dest, ctx.lib / f"{sha}.superseded-{ctx.ts}")
    os.rename(tmp, dest)
    relink(ctx.lib / "current", sha, ctx.ts)
    relink(ctx.pctx, ctx.lib / "current/bin/pctx", ctx.ts)
    relink(ctx.lib / "python", sys.executable, ctx.ts)  # bin/pctx reads it


def pctx_env(home, **extra):
    """Environment for pctx: no inherited PCTX_* (e.g. trial PCTX_ROOTS)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PCTX_")}
    return dict(env, PCTX_HOME=str(home), **extra)


def prebuild(ctx, rec):
    """Step 3: full ingest into <data>.new, then doctor."""
    if dry(ctx, f"PCTX_HOME={ctx.new_data} pctx ingest --full; pctx doctor"):
        return
    ctx.new_data.mkdir(mode=0o700)
    must(ctx, [ctx.pctx, "ingest", "--full"], env=pctx_env(ctx.new_data))
    must(ctx, [ctx.pctx, "doctor"], env=pctx_env(ctx.new_data))


def ingest_fresh(ctx, rec):
    """Fresh install: create the data dir and index what already exists.
    (doctor runs in verify, once the launchd job is up.)"""
    if dry(ctx, f"PCTX_HOME={ctx.data} pctx ingest --full"):
        return
    ctx.data.mkdir(parents=True, mode=0o700)
    must(ctx, [ctx.pctx, "ingest", "--full"], env=pctx_env(ctx.data))


def restart(ctx, rec):
    """Upgrade: kickstart the running job onto the re-pinned release."""
    if dry(ctx, f"launchctl kickstart -k {ctx.target}"):
        return
    started = ctx.now()
    must(ctx, ["launchctl", "kickstart", "-k", ctx.target])
    wait(ctx, lambda: is_new(ctx, job(ctx)), 30, "job has no live PID")
    wait(ctx, lambda: fresh(ctx, started), HEARTBEAT_S, "heartbeat not fresh")


def prune(ctx, rec):
    """One pinned release: delete every release dir but current's target.
    Runs last, so a failed install can still roll back to the old one."""
    link = ctx.lib / "current"
    keep = os.readlink(link) if link.is_symlink() else None
    old = [
        p
        for p in sorted(ctx.lib.iterdir() if ctx.lib.is_dir() else [])
        if p.is_dir() and not p.is_symlink() and p.name != keep
    ]
    if dry(ctx, f"prune {[p.name for p in old]}"):
        return
    for path in old:
        ctx.say(f"prune {path.name}")
        shutil.rmtree(path)


def snapshot(ctx, rec):
    """A8: APFS clone of the legacy data dir while the old job still runs."""
    dest = ctx.legacy / "snapshot"
    if dry(ctx, f"cp -c -R {ctx.data} {dest}") or not ctx.data.exists():
        return
    must(ctx, ["cp", "-c", "-R", ctx.data, dest])


def stop_old(ctx, rec):
    """Step 4: bootout; wait for the label and old-tree code to be gone."""
    if dry(ctx, f"launchctl bootout {ctx.target}"):
        return
    if job(ctx):
        ctx.run(["launchctl", "bootout", ctx.target])
    wait(ctx, lambda: job(ctx) is None, 30, "old job still loaded")
    wait(ctx, lambda: not old_procs(ctx), 30, "old-tree code still running")


def swap(ctx, rec):
    """Step 5: legacy data aside (kept), pre-built store in, catch up."""
    if dry(
        ctx, f"mv {ctx.data} {ctx.legacy}/data; mv {ctx.new_data} {ctx.data}"
    ):
        return
    if ctx.data.exists():
        os.rename(ctx.data, ctx.legacy / "data")
    os.rename(ctx.new_data, ctx.data)
    (ctx.legacy / "RETENTION.txt").write_text(
        f"Legacy provenance-context data from the {ctx.ts} cutover. Keep "
        "until the owner accepts the cutover (default 7 days); only the "
        "owner deletes it. Rollback: python3.13 -E -s -B -m "
        f"install.rollback --record {ctx.legacy}/cutover-record.json\n"
    )
    must(ctx, [ctx.pctx, "ingest"], env=pctx_env(ctx.data))


def fresh(ctx, since):
    try:
        mtime = (ctx.data / "status.json").stat().st_mtime
    except FileNotFoundError:
        return False
    return mtime >= since - 1 and ctx.now() - mtime < HEARTBEAT_S


def start_new(ctx, rec):
    """Step 6: install the pinned plist, bootstrap, live PID + heartbeat."""
    src = ctx.lib / "current/launchd/com.provenance-context.plist"
    if dry(ctx, f"install {src} as {ctx.plist}; launchctl bootstrap"):
        return
    data = src.read_bytes()
    prog = f"{ctx.lib}/current/bin/pctx"
    plist = plistlib.loads(data)
    if b"@HOME@" in data or plist.get("Label") != LABEL:
        raise StepFailed(
            "pinned plist is not substituted or has another label"
        )
    if plist["ProgramArguments"][0] != prog or not os.access(prog, os.X_OK):
        raise StepFailed("pinned plist does not run current/bin/pctx")
    ctx.plist.parent.mkdir(parents=True, exist_ok=True)
    ce.atomic_write(ctx.plist, data, 0o644)
    started = ctx.now()
    must(ctx, ["launchctl", "bootstrap", f"gui/{ctx.uid}", ctx.plist])
    wait(ctx, lambda: is_new(ctx, job(ctx)), 30, "new job has no live PID")
    wait(
        ctx,
        lambda: fresh(ctx, started),
        HEARTBEAT_S,
        "heartbeat not fresh in 120 s",
    )


def claude(ctx, rec):
    """Step 7: merge our two hooks; drop old-tree marketplace entries."""
    if dry(ctx, f"merge SessionStart+UserPromptSubmit into {ctx.settings}"):
        return
    if not rec["has_claude"]:
        ctx.say(f"{ctx.settings} not found: Claude Code left unconfigured")
        return
    frag = ctx.lib / "current/integrations/claude/settings-hooks.json"
    fragment = json.loads(frag.read_bytes())["hooks"]

    def check(before, after):
        ce.json_check(before, after, claude_paths(ce.load_json(before)))

    edit = lambda b: edit_settings(b, fragment, ctx.old_tree)  # noqa: E731
    ce.edit_file(ctx.settings, edit, check)
    if ctx.known.exists():
        ce.edit_file(
            ctx.known,
            lambda b: edit_known(b, ctx.old_tree),
            lambda a, b: ce.json_check(a, b, [(MKT_NAME,)]),
        )


def codex(ctx, rec):
    """Step 8 in A6 order: untrust, retire stale cache, install, trust."""
    source = f"{ctx.lib}/current/integrations/codex"
    if dry(ctx, f"codex: drop old trust keys, move {ctx.cache}, add plugin"):
        return
    if not rec["has_codex"]:
        ctx.say(f"{ctx.config} not found: Codex left unconfigured")
        return

    def edit(transform):
        ce.edit_file(
            ctx.config, lambda b: transform(b.decode()).encode(), codex_check
        )

    edit(drop_trust)
    # A Codex install deletes other cached versions (store.rs:689-718):
    # keep the old copy as residue for the owner instead.
    if ctx.cache.exists():
        (ctx.legacy / "codex-plugin-cache").mkdir()
        stale = ctx.legacy / "codex-plugin-cache" / ctx.cache.name
        os.rename(ctx.cache, stale)
    edit(lambda text: repoint(text, source))
    before = ctx.config.read_bytes()
    env = dict(os.environ, CODEX_HOME=str(ctx.codex_home))
    argv = ["codex", "plugin", "add", PLUGIN_ID, "--json"]
    out = json.loads(must(ctx, argv, env=env, quiet=True).stdout)
    codex_check(before, ctx.config.read_bytes())  # Codex wrote only our keys
    pinned = (
        ctx.lib / "current/integrations/codex/hooks/hooks.json"
    ).read_bytes()
    rec["codex_plugin_version"] = out["version"]
    installed = Path(out["installedPath"]).resolve()
    if installed != (ctx.cache / out["version"]).resolve():
        raise StepFailed("codex installed the plugin somewhere unexpected")
    if (installed / "hooks/hooks.json").read_bytes() != pinned:
        raise StepFailed("codex cache hooks.json differs from the pinned copy")
    edit(enable)
    if rec["trust"] == "auto":
        edit(lambda text: write_trust(text, codex_hooks(pinned)))
    else:
        ctx.say(OWNER_STEP)


def old_refs(ctx):
    """Config files or plists that still reference old-tree code."""
    code = [
        f"{ctx.old_tree}/{d}".encode()
        for d in ("scripts", "hooks", "claude-code")
    ]
    files = [ctx.settings, ctx.known, ctx.config]
    files += sorted(ctx.plist.parent.glob("*.plist"))
    bad = [
        f.name
        for f in files
        if f.exists() and any(c in f.read_bytes() for c in code)
    ]
    if ctx.config.exists():
        mkt = ce.parse_section(
            ctx.config.read_text(), MARKETPLACE, CODEX_KEYS[MARKETPLACE]
        )
        if str(ctx.old_tree) in json.dumps(mkt):
            bad.append("config.toml marketplace")
    return bad


def hook_commands(ctx):
    exists = ctx.settings.exists()
    settings = ce.load_json(ctx.settings.read_bytes()) if exists else {}
    cmds = [
        h["command"]
        for event in CLAUDE_EVENTS
        for group in (ce.jget(settings, ("hooks", event))[1] or [])
        if ours(group, ctx.old_tree)
        for h in group["hooks"]
    ]
    for path in sorted(ctx.cache.glob("*/hooks/hooks.json")):
        cmds += [h["command"] for h in codex_hooks(path.read_bytes())]
    return cmds


def verify(ctx, rec):
    """Step 9: machine-readable checks; any failure triggers rollback."""
    if dry(ctx, "verify: refs, processes, PID, heartbeat, hooks, doctor"):
        return
    checks = []

    def check(name, ok, detail=""):
        ok = None if ok is None else bool(ok)  # None: could not verify
        checks.append({"check": name, "ok": ok, "detail": detail})

    if not (ctx.fresh or ctx.upgrade):
        check("old_tree_unchanged", tree_hash(ctx) == rec["old_tree_hash"])
    refs = old_refs(ctx)
    check("no_old_tree_references", not refs, ", ".join(refs))
    check("old_process_gone", not old_procs(ctx))
    j = job(ctx)
    check("new_pid_alive", is_new(ctx, j), f"pid {j and j['pid']}")
    check("heartbeat_fresh", fresh(ctx, 0))
    env = pctx_env(ctx.data, PCTX_HOOK_DISABLE="1")
    for cmd in hook_commands(ctx):
        r = ctx.run(shlex.split(cmd), env=env, input=b"{}")
        check(
            "hook_runs", r.returncode == 0 and r.stdout.strip() == b"{}", cmd
        )
    if ctx.probe and rec["has_codex"]:
        codex_checks(ctx, rec, check)
    r = ctx.run([ctx.pctx, "doctor", "--cutover"], env=pctx_env(ctx.data))
    check("doctor_cutover", r.returncode == 0)
    report = json.dumps({"checks": checks}, indent=1).encode()
    ce.atomic_write(ctx.legacy / "verify.json", report, 0o600)
    failed = [c["check"] for c in checks if c["ok"] is False]
    if failed:
        raise StepFailed("verify failed: " + ", ".join(failed))


def codex_checks(ctx, rec, check):
    """Ask Codex which hooks it resolves. A wrong answer fails verify;
    no answer or an untrusted hook only leaves the owner step."""
    hooks = ctx.lib / "current/integrations/codex/hooks/hooks.json"
    key = f"{PLUGIN_ID}:hooks/hooks.json:"
    want = {key + h["suffix"]: h for h in codex_hooks(hooks.read_bytes())}
    try:
        entries = ctx.probe(ctx)
    except Exception as exc:  # no answer is not a wrong answer
        check("codex_resolves_new_hooks", None, f"probe: {type(exc).__name__}")
        rec["trust"] = "owner"
        ctx.say(f"WARNING: Codex hooks/list probe failed; {OWNER_STEP}")
        return
    got = {e["key"]: e for e in entries if e.get("pluginId") == PLUGIN_ID}
    active = ctx.cache / rec["codex_plugin_version"] / "hooks/hooks.json"
    same = set(got) == set(want) and all(
        got[k]["command"] == want[k]["command"]
        and Path(got[k]["sourcePath"]).resolve() == active.resolve()
        for k in want
    )
    check("codex_resolves_new_hooks", same, ", ".join(sorted(got)))
    rec["codex_hooks"] = {
        k: {
            "trust": e["trustStatus"],
            "hash_match": e.get("currentHash") == want.get(k, {}).get("hash"),
        }
        for k, e in got.items()
    }
    if not all(v["trust"] == "trusted" for v in rec["codex_hooks"].values()):
        rec["trust"] = "owner"
        ctx.say(OWNER_STEP)


STEPS = (
    pin,
    prebuild,
    snapshot,
    stop_old,
    swap,
    start_new,
    claude,
    codex,
    verify,
    prune,
)
FRESH_STEPS = (pin, ingest_fresh, start_new, claude, codex, verify, prune)
UPGRADE_STEPS = (pin, restart, verify, prune)


def steps(ctx):
    if ctx.upgrade:
        return UPGRADE_STEPS
    return FRESH_STEPS if ctx.fresh else STEPS


def cutover(ctx, repo, sha, expect_tree=None):
    rec = preflight(ctx, Path(repo), sha, expect_tree)
    record(ctx, rec)
    step = record
    try:
        for step in steps(ctx):
            ctx.say(f"step {step.__name__}")
            step(ctx, rec)
            if not ctx.dry_run:
                rec["steps"].append(step.__name__)
                save(ctx, rec)
    except Exception as exc:
        if ctx.dry_run:
            raise
        rec["failed"] = {
            "step": step.__name__,
            "error": f"{type(exc).__name__}: {exc}",
        }
        save(ctx, rec)
        ctx.say(f"cutover failed at {step.__name__}: {exc}; rolling back")
        from install.rollback import rollback

        for problem in rollback(ctx, rec):
            ctx.say(f"PROBLEM: {problem}")
        install_record(ctx, rec, "rolled_back")
        raise
    if ctx.upgrade and not ctx.dry_run:  # no rollback once the old is gone
        shutil.rmtree(ctx.legacy)
        rec["record_removed"] = True
    install_record(ctx, rec, "ok")
    done = "planned" if ctx.dry_run else "done"
    where = "install-record.json" if rec.get("record_removed") else ctx.legacy
    ctx.say(f"cutover {done}; record in {where}")
    return rec


def codex_probe(ctx, timeout=60):
    """Ask the real Codex app-server which hooks it resolves (read-only)."""
    env = dict(os.environ, CODEX_HOME=str(ctx.codex_home))
    p = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=ctx.home,
    )
    lines = queue.Queue()
    threading.Thread(
        target=lambda: [lines.put(x) for x in p.stdout], daemon=True
    ).start()

    def call(msg_id, method, params):
        p.stdin.write(
            json.dumps(
                {"id": msg_id, "method": method, "params": params}
            ).encode()
            + b"\n"
        )
        p.stdin.flush()
        while True:
            try:
                msg = json.loads(lines.get(timeout=timeout))
            except ValueError:
                continue
            if msg.get("id") == msg_id:
                return msg

    try:
        call(
            1,
            "initialize",
            {"clientInfo": {"name": "pctx-cutover", "version": "1"}},
        )
        p.stdin.write(b'{"method": "initialized"}\n')
        result = call(2, "hooks/list", {"cwds": [str(ctx.home)]})["result"]
    finally:
        p.terminate()
        p.wait(timeout=10)
    return [h for entry in result["data"] for h in entry["hooks"]]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="install.cutover")
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--expect-old-tree-hash")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--upgrade",
        action="store_true",
        help="already on pctx: re-pin this commit, restart, keep one release",
    )
    ap.add_argument(
        "--fresh",
        action="store_true",
        help="no legacy daemon or data (a colleague's machine)",
    )
    ap.add_argument("--home", type=Path, default=Path.home())
    args = ap.parse_args(argv)
    if args.fresh and args.upgrade:
        ap.error("--fresh and --upgrade are exclusive")
    if args.home.resolve() != Path.home().resolve() and not args.dry_run:
        ap.error(
            "--home is dry-run only: launchctl acts on the real gui domain"
        )
    os.umask(0o077)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    say, run = install_log(
        args.home / ".local/lib/provenance-context/install.log"
    )
    ctx = Ctx(
        args.home,
        run,
        ts,
        dry_run=args.dry_run,
        probe=codex_probe,
        say=say,
        fresh=args.fresh,
        upgrade=args.upgrade,
    )
    try:
        cutover(ctx, args.repo.resolve(), args.sha, args.expect_old_tree_hash)
    except (StepFailed, ce.Refused, ce.Raced) as exc:
        say(f"FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
