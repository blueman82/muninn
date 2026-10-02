"""Remove muninn from this machine.

  python3.13 -E -s -B -m install.uninstall [--dry-run] [--purge-data]

Stops the launchd job and removes its plist, takes our hooks out of Claude's
settings.json and our sections out of Codex's config.toml, deletes our Codex
plugin cache, and removes the pinned releases and the ``muninn`` command
link. Nothing else in either provider config is touched: each edit is proved
to change only our own keys, as the install does.

Everything that can refuse (a config we cannot edit safely, a name already
taken for the data) is checked first, in a dry run too, so a refusal leaves
the machine as it was. After that the steps run one at a time and each looks
at launchd and the filesystem first, so a second run finishes an interrupted
one, and a run on a machine that never had muninn says so.

The data dir holds the index and the cited knowledge ledger, which cannot be
rebuilt from transcripts. It is moved aside to ``muninn-removed-<ts>`` next
to it, never deleted, unless ``--purge-data`` says so.
"""

from __future__ import annotations

import argparse
import functools
import os
import shutil
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from install import configedit as ce
from install.constants import (
    CLAUDE_EVENTS,
    CODEX_KEYS,
    PRIVATE_DIR_MODE,
    PRIVATE_UMASK,
    TS_FORMAT,
)
from install.context import Ctx, StepFailedError, job, link_text, run_real
from install.errors import RacedError, RefusedError
from install.rollback import bootout
from install.transforms import claude_paths, codex_check, ours


def _act(ctx: Ctx, text: str, fn: Callable[[], object]) -> bool:
    """Announce one uninstall action and run it unless this is a dry run.

    Args:
        ctx: The run context.
        text: What the action does.
        fn: The action.

    Returns:
        True, so a caller can count what it did or would have done.
    """
    ctx.say(f"{'DRY-RUN ' if ctx.dry_run else ''}uninstall: {text}")
    if not ctx.dry_run:
        fn()
    return True


def drop_hooks(data: bytes) -> bytes:
    """Take our hook groups out of settings.json.

    Args:
        data: The current settings.json bytes.

    Returns:
        The new bytes. Other hook groups and keys are left as they were; an
        event or the ``hooks`` table that we leave empty is removed. When no
        group of ours is present, the bytes come back untouched, so the
        owner's formatting and an empty ``hooks`` table of their own survive.

    Raises:
        RefusedError: If an event's hooks value is not a list.
    """
    obj = ce.load_json(data)
    touched = False
    for event in CLAUDE_EVENTS:
        path = ("hooks", event)
        present, current, _ = ce.jget(obj, path)
        if not present:
            continue
        if not isinstance(current, list):
            raise RefusedError(f"settings.json hooks.{event} is not a list")
        groups = cast(list[dict[str, Any]], current)
        keep = [g for g in groups if not ours(g)]
        if len(keep) == len(groups):
            continue
        touched = True
        if keep:
            ce.jset(obj, path, keep)
        else:
            ce.jdel(obj, path)
    if not touched:
        return data
    if ce.jget(obj, ("hooks",))[1] == {}:
        ce.jdel(obj, ("hooks",))
    return ce.dump_like(data, obj)


def drop_sections(text: str) -> str:
    """Take our sections out of config.toml.

    Args:
        text: The whole config.toml.

    Returns:
        The text without our marketplace, plugin and trust sections.
    """
    for header in CODEX_KEYS:
        text = ce.put_section(text, header, None)
    return text


def _drop_sections_bytes(data: bytes) -> bytes:
    """Apply ``drop_sections`` to the bytes of config.toml."""
    return drop_sections(data.decode()).encode()


def _settings_check(before: bytes, after: bytes) -> None:
    """Prove an edit of settings.json touched only our hook events."""
    ce.json_check(before, after, claude_paths(ce.load_json(before)))


def _preflight(ctx: Ctx, purge: bool) -> None:
    """Refuse before any change when a later step would have to refuse.

    Args:
        ctx: The run context.
        purge: Whether the data dir is to be deleted rather than moved.

    Raises:
        RefusedError: If a provider config cannot be edited safely.
        StepFailedError: If the dir for the moved data already exists.
    """
    if ctx.settings.exists():
        before = ctx.settings.read_bytes()
        _settings_check(before, drop_hooks(before))
    if ctx.config.exists():
        before = ctx.config.read_bytes()
        codex_check(before, _drop_sections_bytes(before))
    if not purge and ctx.data.exists() and ctx.removed.exists():
        raise StepFailedError(
            f"{ctx.removed} already exists; move it away and run again"
        )


def _unhook_claude(ctx: Ctx) -> bool:
    """Remove our hooks from Claude's settings.json, if it holds any."""
    if not ctx.settings.exists():
        return False
    before = ctx.settings.read_bytes()
    if drop_hooks(before) == before:
        return False
    return _act(
        ctx,
        f"remove the muninn hooks from {ctx.settings}",
        lambda: ce.edit_file(ctx.settings, drop_hooks, _settings_check),
    )


def _drop_cache(ctx: Ctx) -> None:
    """Delete our cached plugin and its marketplace dir once it is empty."""
    shutil.rmtree(ctx.cache)
    if not any(ctx.cache.parent.iterdir()):
        ctx.cache.parent.rmdir()


def _unconfigure_codex(ctx: Ctx) -> bool:
    """Remove our sections from Codex's config.toml and our plugin cache."""
    done = False
    if ctx.config.exists():
        before = ctx.config.read_bytes()
        if _drop_sections_bytes(before) != before:
            done = _act(
                ctx,
                f"remove the muninn sections from {ctx.config}",
                lambda: ce.edit_file(
                    ctx.config, _drop_sections_bytes, codex_check
                ),
            )
    if ctx.cache.exists():
        done = _act(
            ctx,
            f"remove the plugin cache {ctx.cache}",
            lambda: _drop_cache(ctx),
        )
    return done


def _stop_job(ctx: Ctx) -> bool:
    """Unload the launchd job and remove its plist."""
    done = False
    j = job(ctx)
    if j:
        pid = f" (pid {j['pid']})" if j["pid"] else " (not running)"
        done = _act(ctx, f"stop the poller{pid}", lambda: bootout(ctx))
    if ctx.plist.exists():
        done = _act(ctx, f"remove {ctx.plist}", ctx.plist.unlink)
    return done


def _is_ours(ctx: Ctx) -> bool:
    """Say whether the ``muninn`` command link points into our release dir."""
    return ctx.muninn.is_symlink() and Path(
        link_text(ctx.muninn)
    ).is_relative_to(ctx.lib)


def _remove_release(ctx: Ctx) -> bool:
    """Remove the ``muninn`` link if it is ours, then the release dir."""
    done = False
    if _is_ours(ctx):
        done = _act(ctx, f"remove {ctx.muninn}", ctx.muninn.unlink)
    if ctx.lib.exists():
        done = _act(ctx, f"remove {ctx.lib}", lambda: shutil.rmtree(ctx.lib))
    return done


def _move_data(ctx: Ctx) -> None:
    """Move the data dir into a new private dir, undoing it on failure."""
    ctx.removed.mkdir(mode=PRIVATE_DIR_MODE)
    try:
        ctx.data.rename(ctx.removed / ctx.data.name)
    except OSError:
        ctx.removed.rmdir()
        raise


def _remove_data(ctx: Ctx, purge: bool) -> bool:
    """Move the data dir aside, or delete it when ``purge`` is set."""
    if not ctx.data.exists():
        return False
    if purge:
        return _act(
            ctx,
            f"delete {ctx.data} (index and knowledge ledger)",
            lambda: shutil.rmtree(ctx.data),
        )
    return _act(
        ctx,
        f"move {ctx.data} to {ctx.removed / ctx.data.name} "
        "(delete it yourself when sure)",
        lambda: _move_data(ctx),
    )


def uninstall(ctx: Ctx, purge: bool = False) -> bool:
    """Remove muninn from this machine.

    Args:
        ctx: The run context.
        purge: Delete the data dir instead of moving it aside.

    Returns:
        True when anything was removed (or would be, in a dry run), False
        when muninn was not installed.

    Raises:
        RefusedError: If a provider config cannot be edited safely; nothing
            has been changed.
        StepFailedError: If the data dir's new name is taken (nothing has
            been changed) or launchd will not unload the job.
    """
    _preflight(ctx, purge)
    steps: tuple[Callable[[Ctx], bool], ...] = (
        _stop_job,
        _unhook_claude,
        _unconfigure_codex,
        _remove_release,
        functools.partial(_remove_data, purge=purge),
    )
    # Every step runs even after one has acted, so collect before any().
    results = [step(ctx) for step in steps]
    done = any(results)
    if not done:
        ctx.say("muninn is not installed here")
    elif ctx.dry_run:
        ctx.say("dry run only: nothing was removed")
    else:
        ctx.say("uninstall done")
    return done


def main(argv: Sequence[str] | None = None) -> int:
    """Run the uninstall from the command line.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv``.

    Returns:
        0 on success, including when there was nothing to remove; 1 when a
        refusal or a failed step stopped it.
    """
    ap = argparse.ArgumentParser(prog="install.uninstall")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--purge-data",
        action="store_true",
        help="delete the index and knowledge ledger instead of moving them"
        " aside",
    )
    args = ap.parse_args(argv)
    # Files we create must not be readable by other users.
    os.umask(PRIVATE_UMASK)
    ts = time.strftime(TS_FORMAT, time.gmtime())
    ctx = Ctx(Path.home(), run_real, ts, dry_run=args.dry_run)
    try:
        uninstall(ctx, args.purge_data)
    except (
        StepFailedError,
        RefusedError,
        RacedError,
        OSError,
        ValueError,
    ) as e:
        # ValueError covers a settings.json or config.toml that does not parse.
        ctx.say(f"FAILED: {e}")
        ctx.say("fix that and run again: steps already done are skipped")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
