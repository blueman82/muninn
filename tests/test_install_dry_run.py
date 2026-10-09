"""A dry run says plainly what a real run would do, and writes nothing."""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from install import installer as co
from tests.installer_support import World, git


def _main(*argv: str) -> tuple[int, str]:
    """Run the installer's ``main``, capturing what it prints.

    Args:
        *argv: Command-line arguments.

    Returns:
        The exit status and the printed text.
    """
    out = io.StringIO()
    with (
        contextlib.redirect_stdout(out),
        mock.patch.object(
            co, "Ctx", functools.partial(co.Ctx, platform="darwin")
        ),
    ):
        status = co.main(list(argv))
    return status, out.getvalue()


class UpgradePlanTest(unittest.TestCase):
    """--upgrade --dry-run on a machine that already runs muninn."""

    def setUp(self) -> None:
        self.w = w = World(self)
        self.first = co.install(w.ctx(fresh=True), w.repo, w.sha)["sha"]
        (w.repo / "bin/note").write_text("v2\n")
        git(w.repo, "add", "-A")
        git(w.repo, "commit", "-qm", "v2")
        self.sha2 = git(w.repo, "rev-parse", "HEAD").decode().strip()
        w.out.clear()
        ctx = dataclasses.replace(
            w.ctx(upgrade=True, dry_run=True), ts="20261002T000000Z"
        )
        co.install(ctx, w.repo, self.sha2)
        self.said = "\n".join(w.out)

    def test_names_the_release_a_real_upgrade_deletes(self) -> None:
        self.assertIn(
            f"would move the previous release {self.first} aside, then delete",
            self.said,
        )
        self.assertIn("no way back", self.said)

    def test_says_provider_config_is_left_alone(self) -> None:
        self.assertNotIn("config keys", self.said)
        self.assertIn("would not touch provider settings", self.said)

    def test_explains_history_import_would_be_skipped(self) -> None:
        self.assertIn(
            "History import: would be skipped during upgrade; existing index "
            "would be retained.",
            self.said,
        )
        self.assertIn(
            "Claude and Codex polling would resume after a successful "
            "upgrade; Cursor would not be re-imported.",
            self.said,
        )

    def test_does_not_name_a_record_directory_it_never_creates(self) -> None:
        self.assertNotIn("record in", self.said)
        self.assertIn("nothing was written", self.said)


class FreshPlanTest(unittest.TestCase):
    """--fresh --dry-run on a machine without the provider configs."""

    def test_skips_providers_that_are_not_configured(self) -> None:
        w = World(self)
        (w.home / ".claude/settings.json").unlink()
        (w.home / ".codex/config.toml").unlink()
        co.install(w.ctx(fresh=True, dry_run=True), w.repo, w.sha)
        said = "\n".join(w.out)
        self.assertIn("Codex left unconfigured", said)
        self.assertIn("Claude Code left unconfigured", said)
        self.assertNotIn("add the Codex plugin", said)
        self.assertNotIn("add SessionStart", said)

    def test_history_detection_reports_without_writing(self) -> None:
        w = World(self)
        history = w.home / ".claude/projects/project/session.jsonl"
        cursor = w.home / "synthetic-cursor.sqlite"
        history.parent.mkdir(parents=True)
        cursor.parent.mkdir(parents=True, exist_ok=True)
        history.touch()
        cursor.touch()

        with mock.patch(
            "install.steps_release.default_database", return_value=cursor
        ):
            co.install(w.ctx(fresh=True, dry_run=True), w.repo, w.sha)

        said = "\n".join(w.out)
        self.assertIn("Claude: found; would index", said)
        self.assertIn("Codex: no history found", said)
        self.assertIn("Cursor: found; would index", said)
        self.assertFalse((w.home / ".local/share/muninn").exists())
        self.assertFalse(
            any(Path(call[0]).name == "muninn" for call in w.fake.calls)
        )


class DryRunWritesNothingTest(unittest.TestCase):
    """The command line's dry run leaves no log and no directory."""

    def test_cli_uses_the_same_darwin_backend_as_world(self) -> None:
        w = World(self)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(co, "Ctx", wraps=co.Ctx) as context,
        ):
            status, _ = _main(
                "--repo",
                str(w.repo),
                "--sha",
                w.sha,
                "--fresh",
                "--dry-run",
                "--home",
                tmp,
            )
            self.assertEqual(status, 0)
            self.assertEqual(context.call_args.kwargs["platform"], "darwin")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_fresh_dry_run_into_an_empty_home_creates_nothing(self) -> None:
        w = World(self)
        with tempfile.TemporaryDirectory() as tmp:
            status, _ = _main(
                *("--repo", str(w.repo), "--sha", w.sha, "--fresh"),
                *("--dry-run", "--home", tmp),
            )
            self.assertEqual(status, 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_a_refused_dry_run_does_not_append_to_the_log(self) -> None:
        w = World(self)
        co.install(w.ctx(fresh=True), w.repo, w.sha)
        log = w.home / ".local/lib/muninn/install.log"
        status, said = _main(
            *("--repo", str(w.repo), "--sha", w.sha, "--fresh"),
            *("--dry-run", "--home", str(w.home)),
        )
        self.assertEqual(status, 1)
        self.assertIn("already installed", said)
        self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
