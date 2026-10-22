"""Disposable provider proof contracts without invoking a native provider."""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from install.constants import PLUGIN_ID
from install.context import Ctx, run_real
from install.trust import codex_hooks
from tools.learning_eval import mac_provider_models as models
from tools.learning_eval import mac_provider_probe as probe

ROOT = Path(__file__).resolve().parents[1]


class MacProviderProbeTests(unittest.TestCase):
    """Reject false proof and exercise private hooks through public CLI."""

    def test_app_server_rejects_oversized_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx, env = self.fake_server(Path(tmp), "oversized")
            with (
                models.provider_environment(env),
                self.assertRaisesRegex(ValueError, "output_limit"),
            ):
                probe.codex_probe(ctx, timeout=2)

    def test_app_server_bounds_stderr_and_silent_timeout(self) -> None:
        if sys.platform == "win32":
            self.skipTest("requires POSIX ownership or process APIs")
        for mode, error in (
            ("stderr", "output_limit"),
            ("silent", "process_timeout"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                ctx, env = self.fake_server(Path(tmp), mode)
                started = time.monotonic()
                with (
                    models.provider_environment(env),
                    self.assertRaisesRegex(ValueError, error),
                ):
                    probe.codex_probe(ctx, timeout=2)
                self.assertLess(time.monotonic() - started, 4)
                pid = int((ctx.home / "server.pid").read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)

    def test_app_server_kills_and_reaps_term_resistant_server(self) -> None:
        if sys.platform == "win32":
            self.skipTest("requires POSIX ownership or process APIs")
        with tempfile.TemporaryDirectory() as tmp:
            ctx, env = self.fake_server(Path(tmp), "resistant")
            failure: Exception | None = None
            started = time.monotonic()
            try:
                with models.provider_environment(env):
                    self.assertEqual(probe.codex_probe(ctx, timeout=2), [])
            except Exception as exc:
                failure = exc
            pid = int((ctx.home / "server.pid").read_text())
            alive = True
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
            finally:
                if alive:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            self.assertIsNone(failure)
            self.assertFalse(alive)
            self.assertLess(time.monotonic() - started, 4)
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)

    def prepare(self, ctx: Ctx) -> tuple[dict[str, str], bytes]:
        """Keep the Linux test interpreter's provisioned SQLite available."""
        if sys.platform == "win32":
            self.skipTest("requires the POSIX provider launcher")
        env = models.environment(ctx, Path(sys.executable))
        if sys.platform == "linux" and (
            library := os.environ.get("LD_LIBRARY_PATH")
        ):
            env["LD_LIBRARY_PATH"] = library
        with mock.patch.object(probe, "environment", return_value=env):
            return probe.prepare(ctx, ROOT, Path(sys.executable))

    def fake_server(self, root: Path, mode: str) -> tuple[Ctx, dict[str, str]]:
        """Create a synthetic local protocol server without provider access."""
        if sys.platform == "win32":
            self.skipTest("requires a POSIX executable script")
        ctx = Ctx(root.resolve(), run_real, "test", platform="darwin")
        fake = ctx.home / "provider-bin/codex"
        fake.parent.mkdir(mode=0o700)
        script = f"""#!{sys.executable}
import json,os,signal,sys,time
if {mode!r} in ('resistant', 'silent'):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open('server.pid', 'w') as output: output.write(str(os.getpid()))
if {mode!r} == 'silent':
    while True: time.sleep(1)
if {mode!r} == 'stderr':
    sys.stderr.write('x' * (2*1024*1024)); sys.stderr.flush()
for line in sys.stdin:
    message = json.loads(line)
    if message.get('id') == 1:
        print(json.dumps({{'id':1,'result':{{}}}}), flush=True)
    if message.get('id') == 2:
        hooks = []
        if {mode!r} == 'oversized': hooks = [{{'padding':'x'*(2*1024*1024)}}]
        response = {{'id':2,'result':{{'data':[{{'hooks':hooks}}]}}}}
        print(json.dumps(response), flush=True)
while True: time.sleep(1)
"""
        fake.write_text(script)
        fake.chmod(0o700)
        return ctx, models.environment(ctx, Path(sys.executable))

    def test_environment_discards_credentials_and_provider_overrides(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(Path(tmp), run_real, "test", platform="darwin")
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}):
                env = models.environment(ctx, Path(sys.executable))
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertEqual(env["CODEX_HOME"], str(ctx.codex_home))
            self.assertEqual(env["HOME"], str(ctx.home))
            self.assertNotIn("MUNINN_HOOK_DISABLE", env)
            self.assertEqual(
                json.loads(env["MUNINN_ROOTS"]),
                {"codex-sessions": str(ctx.home / "transcripts")},
            )

    def test_hook_evidence_rejects_duplicates_wrong_hash_and_escape(
        self,
    ) -> None:
        rendered = (ROOT / "integrations/codex/hooks/hooks.json").read_bytes()
        wanted = codex_hooks(rendered, platform="darwin")
        cached = Path("/private/disposable/hooks/hooks.json")
        entries = [
            {
                "pluginId": PLUGIN_ID,
                "key": f"{PLUGIN_ID}:hooks/hooks.json:{h['suffix']}",
                "command": h["command"],
                "currentHash": h["hash"],
                "sourcePath": str(cached),
                "trustStatus": "untrusted",
            }
            for h in wanted
        ]
        self.assertEqual(len(models.check_hooks(entries, wanted, cached)), 4)
        with self.assertRaisesRegex(ValueError, "expected_routes_mismatch"):
            models.check_hooks(entries[:3], wanted[:3], cached)
        variants = [[*entries, entries[0]]]
        for field, value in (
            ("currentHash", "wrong"),
            ("sourcePath", "/outside"),
            ("command", "wrong"),
        ):
            changed = [dict(entry) for entry in entries]
            changed[0][field] = value
            variants.append(changed)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                models.check_hooks(variant, wanted, cached)

    def test_provider_hash_mismatch_never_writes_synthetic_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(Path(tmp).resolve(), run_real, "test", platform="darwin")
            (ctx.home / "provider-bin").mkdir()
            ctx.codex_home.mkdir()
            ctx.config.write_text("# private config\n")
            rendered = (
                ROOT / "integrations/codex/hooks/hooks.json"
            ).read_bytes()
            cached = ctx.cache / "test/hooks/hooks.json"
            cached.parent.mkdir(parents=True)
            cached.write_bytes(rendered)
            entries = [
                {
                    "pluginId": PLUGIN_ID,
                    "key": f"{PLUGIN_ID}:hooks/hooks.json:{h['suffix']}",
                    "command": h["command"],
                    "currentHash": "mismatch",
                    "sourcePath": str(cached),
                    "trustStatus": "untrusted",
                }
                for h in codex_hooks(rendered, platform="darwin")
            ]
            replies = [
                models.CommandResult(
                    0, (models.PROVIDER_VERSION + "\n").encode(), b"", 1
                ),
                models.CommandResult(
                    0,
                    models.canonical(
                        {
                            "installedPath": str(cached.parent.parent),
                            "version": "test",
                        }
                    ),
                    b"",
                    1,
                ),
            ]
            original = ctx.config.read_bytes()
            with (
                mock.patch.object(probe, "command", side_effect=replies),
                mock.patch.object(probe, "codex_probe", return_value=entries),
                self.assertRaisesRegex(ValueError, "hook_identity_mismatch"),
            ):
                probe.provider(
                    ctx,
                    Path("/synthetic/codex"),
                    models.environment(ctx, Path(sys.executable)),
                    rendered,
                )
            self.assertEqual(ctx.config.read_bytes(), original)
            self.assertEqual(
                json.loads(
                    (ctx.home / "hooks-before-trust.json").read_bytes()
                ),
                entries,
            )

    def test_candidate_mismatch_refuses_before_creating_root(self) -> None:
        if sys.platform == "win32":
            self.skipTest(
                "macOS absolute path validation requires POSIX paths"
            )
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp).resolve() / "new"
            with (
                mock.patch.object(probe.sys, "platform", "darwin"),
                self.assertRaisesRegex(
                    ValueError, "candidate_digest_mismatch"
                ),
            ):
                probe.run(private, Path("/synthetic/codex"), "0" * 64)
            self.assertFalse(private.exists())

    def test_bounded_command_rejects_timeout_and_output_overflow(self) -> None:
        if sys.platform == "win32":
            self.skipTest("macOS process proof requires POSIX pipes")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for code, timeout in (
                ("import time; time.sleep(5)", 0.05),
                ("print('x' * 100000)", 5.0),
            ):
                with self.subTest(code=code), self.assertRaises(ValueError):
                    models.command(
                        [sys.executable, "-I", "-c", code],
                        {},
                        root,
                        timeout=timeout,
                        limit=4096,
                    )
            result = models.command(
                [sys.executable, "-I", "-c", "print('{}')"], {}, root
            )
            self.assertEqual(result.stdout, b"{}\n")
            self.assertEqual(result.returncode, 0)

    def test_private_runtime_ingests_and_direct_hooks_keep_admission_disabled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(
                Path(tmp) / "private", run_real, "test", platform="darwin"
            )
            ctx.home.mkdir(mode=0o700)
            env, rendered = self.prepare(ctx)
            results = probe.direct_hooks(ctx, env, rendered)
            self.assertEqual(len(results), 10)
            self.assertTrue(all(row["returncode"] == 0 for row in results))
            self.assertEqual(probe.learning_state(ctx)["admission"], 0)
            self.assertEqual(probe.learning_state(ctx)["calls"], 0)
            self.assertEqual(probe.learning_state(ctx)["requests"], 0)
            self.assertEqual(
                {row["expectation"] for row in results},
                {"disabled_notice", "empty", "suppressed", "archive_context"},
            )
            for path in ctx.home.rglob("*"):
                if not path.is_symlink():
                    self.assertEqual(path.stat().st_mode & 0o077, 0, str(path))

    def test_direct_hooks_use_rendered_per_event_deadlines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(
                Path(tmp) / "private", run_real, "test", platform="darwin"
            )
            ctx.home.mkdir(mode=0o700)
            env, rendered = self.prepare(ctx)
            with mock.patch.object(
                probe, "command", wraps=probe.command
            ) as run:
                results = probe.direct_hooks(ctx, env, rendered)
            for result, call in zip(results, run.call_args_list, strict=True):
                expected = 3 if result["event"] == "session_end" else 5
                with self.subTest(event=result["event"]):
                    self.assertEqual(result["deadline_ms"], expected * 1000)
                    self.assertEqual(call.kwargs.get("timeout", 5), expected)

    def test_manifest_detects_changes_and_rejects_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "muninn").mkdir()
            source = root / "muninn/example.py"
            source.write_text("first")
            first = models.manifest(root)
            source.write_text("second")
            self.assertNotEqual(first, models.manifest(root))
            source.unlink()
            source.symlink_to(Path(sys.executable))
            with self.assertRaises(ValueError):
                models.manifest(root)


if __name__ == "__main__":
    unittest.main()
