"""Run the real installer entry with only external host effects faked."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path

from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from tests.installer_support import ROOT, World, git

HARNESS = """import os
import sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv.pop(1))
from install import installer, learning_scope, provider_apps
from muninn import review_codex_policy
from tests.installer_support import Fake
fake = Fake(Path.home())
original = installer.Ctx

def context(*args, **kwargs):
    kwargs.update(platform="darwin", default_home=True)
    ctx = original(*args, **kwargs)
    ctx.run = fake.run
    ctx.probe = fake.probe
    if os.environ.get("V1_EXPLICIT") == "1":
        ctx.review_policy = Path.home() / "explicit-policy.json"
    return ctx

def forbidden(*args, **kwargs):
    raise AssertionError("preview reached a mutating migration boundary")

original_install = installer.install

def observed_install(*args, **kwargs):
    result = original_install(*args, **kwargs)
    assert result["schema_to"] == 2
    assert result["policy_input"]["digest"] == os.environ["V1_POLICY_DIGEST"]
    origin = result["policy_input"].get("policy_origin")
    if os.environ.get("V1_EXPLICIT") == "1":
        assert origin is None
    else:
        assert origin == "repository_default"
    print("POLICY_ROUTE_OK")
    return result

with (
    mock.patch.object(installer, "Ctx", context),
    mock.patch.object(installer, "run_real", fake.run),
    mock.patch.object(installer, "install", observed_install),
    mock.patch.object(provider_apps, "APPLICATIONS",
                      Path.home() / "no-system-apps"),
    mock.patch("platform.system", return_value="Darwin"),
    mock.patch("platform.machine", return_value="arm64"),
    mock.patch.object(review_codex_policy, "host_identity",
                      return_value=os.environ["V1_HOST_ID"]),
    mock.patch("muninn.review_codex_launch.selected_auth_home",
               return_value=Path.home() / ".codex"),
    mock.patch("muninn.review_codex_launch.verify_codex_executable",
               return_value=(Path("/bin/codex"), "a" * 64)),
    mock.patch("install.decision_migration.migrate_decisions", forbidden),
    mock.patch("muninn.platform_rebuild.publish_replacement", forbidden),
    mock.patch("install.steps_release.prepare_runtime", forbidden),

):
    if os.environ.get("V1_REAL_PREFLIGHT") == "1":
        ctx = context(Path.home(), fake.run, "real-preflight", upgrade=True)
        scope = None if os.environ["V1_SOURCE_SHAPE"] == "state" else "now"
        learning_scope.choose(ctx, scope, forbidden)
        result = installer.preflight(ctx, Path(sys.argv[2]), sys.argv[4])
        assert result["schema_to"] == 2
        expected = os.environ["V1_POLICY_DIGEST"]
        assert result["policy_input"]["digest"] == expected
        print("REAL_PREFLIGHT_OK")
    else:
        raise SystemExit(installer.main(sys.argv[1:]))
"""


def prepare_entry(world: World) -> tuple[Path, Path]:
    """Add a real shell wrapper and an isolated current-code entry harness."""
    script = world.repo / "bin/muninn-install"
    shutil.copyfile(ROOT / "bin/muninn-install", script)
    script.chmod(0o755)
    shutil.copyfile(
        ROOT / "requirements-dev.txt", world.repo / "requirements-dev.txt"
    )
    profile = json.loads(
        (world.repo / "tools/default_codex_policy.json").read_text()
    )
    profile.update(
        default_revision=2,
        approved_by="fixture-owner",
        approved_at="1970-01-01T00:00:01Z",
    )
    (world.repo / "tools/default_codex_policy.json").write_text(
        json.dumps(profile)
    )
    git(world.repo, "add", "-A")
    git(world.repo, "commit", "-qm", "preview wrapper fixture")
    world.sha = git(world.repo, "rev-parse", "HEAD").decode().strip()
    harness = world.home.parent / "entry.py"
    harness.write_text(HARNESS)
    interpreter = world.home.parent / "python-entry"
    python = shlex.quote(Path(sys.executable).as_posix())
    interpreter.write_text(
        "#!/bin/sh\n"
        'while [ "${1:-}" = -E ] || [ "${1:-}" = -s ] || '
        '[ "${1:-}" = -B ]; do shift; done\n'
        'if [ "${1:-}" = -m ] && [ "${2:-}" = install.installer ]; then\n'
        "shift 2\n"
        f"exec {python} -B {shlex.quote(harness.as_posix())} "
        f'{shlex.quote(ROOT.as_posix())} "$@"\nfi\n'
        f'exec {python} -B "$@"\n',
        newline="\n",
    )
    interpreter.chmod(0o755)
    return script, interpreter


def entry_environment(
    world: World,
    interpreter: Path,
    policy: ReviewPolicy,
    shape: str,
    explicit: bool,
) -> dict[str, str]:
    """Confine every provider and home override to the synthetic fixture."""
    home = world.home
    assert policy.experimental_codex is not None
    return dict(
        os.environ,
        HOME=str(home),
        USERPROFILE=str(home),
        CODEX_HOME=str(home / ".codex"),
        CLAUDE_CONFIG_DIR=str(home / ".claude"),
        MUNINN_HOME=str(home / ".local/share/muninn"),
        APPDATA=str(home / "AppData/Roaming"),
        LOCALAPPDATA=str(home / "AppData/Local"),
        MUNINN_PYTHON=interpreter.as_posix(),
        V1_SOURCE_SHAPE=shape,
        V1_POLICY_DIGEST=digest(policy),
        V1_EXPLICIT="1" if explicit else "0",
        V1_HOST_ID=policy.experimental_codex.host_identity,
    )
