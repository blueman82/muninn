"""Check bounded synthetic request evidence without a real provider call."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
from typing import Any, cast
from unittest import mock

from tools.learning_eval import codex_tool_probe
from tools.learning_eval.codex_tool_probe_io import (
    capture_handler,
    request_evidence,
    run_command,
)


class CodexToolProbeTests(unittest.TestCase):
    """Refuse incomplete requests rather than claim absent tool authority."""

    @unittest.skipIf(sys.platform == "win32", "native probe is POSIX-only")
    def test_source_change_during_probe_refuses_report_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "probe-source.py"
            source.write_text("original fixture")
            output = root / "report.json"
            executable = Path(sys.executable).resolve()

            def change_source(
                target: Path, artifacts: Path
            ) -> list[dict[str, object]]:
                self.assertEqual(target, executable)
                self.assertTrue(artifacts.is_dir())
                source.write_text("changed fixture")
                return []

            with (
                mock.patch.object(
                    codex_tool_probe,
                    "SOURCE_FILES",
                    {"probe_source_sha256": source},
                    create=True,
                ),
                mock.patch.object(
                    codex_tool_probe,
                    "verify_codex_executable",
                    return_value=(
                        executable,
                        codex_tool_probe.file_hash(executable),
                    ),
                ),
                mock.patch.object(
                    codex_tool_probe, "_variants", side_effect=change_source
                ),
                self.assertRaisesRegex(ValueError, "probe_source_changed"),
            ):
                codex_tool_probe.run(executable, output)
            self.assertFalse(output.exists())

    def test_partial_request_headers_cannot_hold_server_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            records: list[dict[str, object]] = []
            accepted = threading.Event()
            stopped = threading.Event()

            class Handler(capture_handler(records, Path(temporary))):
                """Expose accepted connection timing without changing reads."""

                def setup(self) -> None:
                    super().setup()
                    accepted.set()

            with HTTPServer(("127.0.0.1", 0), Handler) as server:
                thread = threading.Thread(
                    target=server.serve_forever, daemon=True
                )
                thread.start()
                with socket.create_connection(
                    ("127.0.0.1", server.server_port)
                ) as client:
                    client.sendall(b"POST /responses HTTP/1.1\r\n")
                    self.assertTrue(accepted.wait(5))

                    def shutdown() -> None:
                        server.shutdown()
                        stopped.set()

                    stopper = threading.Thread(target=shutdown, daemon=True)
                    stopper.start()
                    try:
                        self.assertTrue(stopped.wait(7))
                        self.assertEqual(records, [])
                    finally:
                        client.close()
                        stopper.join(timeout=5)
                        thread.join(timeout=5)

    def test_trickled_headers_cannot_extend_absolute_request_deadline(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            accepted = threading.Event()
            stop_sending = threading.Event()
            stopped = threading.Event()

            class Handler(capture_handler([], Path(temporary))):
                """Shorten the total deadline without changing socket reads."""

                request_deadline = 0.3

                def setup(self) -> None:
                    super().setup()
                    accepted.set()

            with HTTPServer(("127.0.0.1", 0), Handler) as server:
                thread = threading.Thread(
                    target=server.serve_forever, daemon=True
                )
                thread.start()
                with socket.create_connection(
                    ("127.0.0.1", server.server_port)
                ) as client:
                    client.sendall(b"POST /responses HTTP/1.1\r\nX-Test: ")
                    self.assertTrue(accepted.wait(5))

                    def send_headers() -> None:
                        try:
                            while not stop_sending.wait(0.03):
                                client.sendall(b"x")
                        except OSError:
                            return

                    def shutdown() -> None:
                        server.shutdown()
                        stopped.set()

                    sender = threading.Thread(target=send_headers, daemon=True)
                    stopper = threading.Thread(target=shutdown, daemon=True)
                    sender.start()
                    stopper.start()
                    try:
                        self.assertTrue(stopped.wait(1.5))
                    finally:
                        stop_sending.set()
                        client.close()
                        sender.join(timeout=5)
                        stopper.join(timeout=5)
                        thread.join(timeout=5)

    def test_source_markers_remain_distinct_observations(self) -> None:
        evidence = request_evidence(
            b'{"tools":[],"input":[{"text":'
            b'"MUNINN_SYNTHETIC_SOURCE_CANARY_AUTH_AGENTS '
            b'MUNINN_SYNTHETIC_SOURCE_CANARY_CONFIG"}]}'
        )
        self.assertEqual(
            evidence["source_markers_seen"], ["CONFIG", "AUTH_AGENTS"]
        )
        self.assertEqual(evidence["qualification"], "unverified")

    def test_auth_poison_keeps_production_cwd_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auth, cwd, schema, env = codex_tool_probe._prepare(root, True)
            home = Path(env["HOME"])
            self.assertNotEqual(auth, home)
            self.assertNotIn(home, auth.parents)
            self.assertEqual(cwd, home / "review-runner-cwd")
            self.assertEqual(schema, home / "review-codex-schema.json")
            if os.name == "posix":
                self.assertEqual(schema.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(cwd.iterdir()), [])
            self.assertEqual(
                (auth / "AGENTS.md").read_text(),
                "MUNINN_SYNTHETIC_SOURCE_CANARY_AUTH_AGENTS",
            )
            self.assertEqual(env["HOME"], str(root / "home"))
            self.assertEqual(env["CODEX_HOME"], str(auth))
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("CODEX_THREAD_ID", env)

    def test_native_additional_tools_and_nested_namespaces_are_reported(
        self,
    ) -> None:
        request = {
            "model": "synthetic-model",
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "functions",
                            "tools": [
                                {
                                    "type": "custom",
                                    "name": "exec",
                                    "description": "exec_command is available",
                                }
                            ],
                        }
                    ],
                }
            ],
            "tools": [{"type": "web_search"}],
        }
        evidence = request_evidence(json.dumps(request).encode())
        self.assertEqual(
            evidence["tool_names"], ["functions.exec", "web_search"]
        )
        self.assertTrue(evidence["deferred_executor_mentioned"])
        self.assertFalse(evidence["source_canary_seen"])
        request["tools"] = []
        self.assertEqual(
            request_evidence(json.dumps(request).encode())["tool_names"],
            ["functions.exec"],
        )

    def test_unreported_and_malformed_tools_never_prove_no_tools(self) -> None:
        for request in (
            {},
            {"input": [], "tools": None},
            {"input": [], "tools": [False]},
            {"input": [], "tools": [{"type": "namespace", "name": "x"}]},
            {"input": [], "tools": [{"name": "x"}]},
        ):
            with self.subTest(request=request), self.assertRaises(ValueError):
                request_evidence(json.dumps(request).encode())

    def test_empty_explicit_tools_and_source_canary_are_observations(
        self,
    ) -> None:
        evidence = request_evidence(
            b'{"input":[{"text":"MUNINN_SYNTHETIC_SOURCE_CANARY"}],"tools":[]}'
        )
        self.assertEqual(evidence["tool_names"], [])
        self.assertTrue(evidence["source_canary_seen"])

    def test_duplicate_keys_nonfinite_values_and_size_limit_are_refused(
        self,
    ) -> None:
        for raw in (
            b'{"tools":[],"tools":[]}',
            b'{"tools":[],"input":NaN}',
            b"[]",
            b" " * (1024 * 1024 + 1),
        ):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                request_evidence(raw)

    def test_fixture_retains_exact_request_and_refuses_oversized_body(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records: list[dict[str, object]] = []
            with HTTPServer(
                ("127.0.0.1", 0), capture_handler(records, root)
            ) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                connection = HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=5
                )
                try:
                    raw = b'{"tools":[{"type":"web_search"}]}'
                    connection.request("POST", "/responses", raw)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    response.read()
                    self.assertEqual(records[0]["tool_names"], ["web_search"])
                    self.assertEqual(
                        (root / "request-1.json").read_bytes(), raw
                    )
                    if sys.platform != "win32":
                        self.assertEqual(
                            (root / "request-1.json").stat().st_mode & 0o777,
                            0o600,
                        )
                    connection.request(
                        "POST",
                        "/responses",
                        b"",
                        {"Content-Length": str(1024 * 1024 + 1)},
                    )
                    response = connection.getresponse()
                    self.assertEqual(response.status, 413)
                    response.read()
                    self.assertEqual(len(records), 1)
                finally:
                    connection.close()
                    server.shutdown()
                    thread.join(timeout=5)

    @unittest.skipIf(sys.platform == "win32", "POSIX process group contract")
    def test_early_stdin_close_still_collects_rejection_diagnostics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "stdin-closed"
            script = (
                "import os,sys,time; from pathlib import Path; os.close(0); "
                "print('unsupported fixture flag',"
                "file=sys.stderr,flush=True); "
                f"Path({str(marker)!r}).touch(); time.sleep(.2)"
            )
            real_popen = subprocess.Popen

            def ready(
                argv: list[str], **kwargs: Any
            ) -> subprocess.Popen[bytes]:
                child = real_popen(argv, text=False, **kwargs)
                until = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < until:
                    time.sleep(0.01)
                if not marker.exists():
                    if sys.platform != "win32":
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
                    raise AssertionError("child failed to close stdin")
                return cast(subprocess.Popen[bytes], child)

            with mock.patch.object(subprocess, "Popen", side_effect=ready):
                result = run_command(
                    [sys.executable, "-c", script],
                    {"HOME": str(root), "PATH": "/usr/bin:/bin"},
                    root,
                )
            self.assertEqual(result["error"], "process_stdin_closed")
            errors = result["stderr"]
            assert isinstance(errors, bytes)
            self.assertIn(b"unsupported fixture flag", errors)

    @unittest.skipIf(sys.platform == "win32", "POSIX process group contract")
    def test_failure_retains_bounded_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = (
                (
                    "import sys,time; print('diagnostics',flush=True); "
                    "time.sleep(60)",
                    2.0,
                    "process_timeout",
                ),
                (
                    "import sys; sys.stdout.buffer.write(b'z'*(1024*1024+1))",
                    30.0,
                    "process_output_limit",
                ),
            )
            for script, timeout, error in cases:
                with self.subTest(error=error):
                    result = run_command(
                        [sys.executable, "-c", script],
                        {"HOME": str(root), "PATH": "/usr/bin:/bin"},
                        root,
                        timeout=timeout,
                    )
                    self.assertEqual(result["error"], error)
                    output = result["stdout"]
                    errors = result["stderr"]
                    assert isinstance(output, bytes)
                    assert isinstance(errors, bytes)
                    self.assertGreater(len(output), 0)
                    self.assertLessEqual(
                        len(output) + len(errors), 1024 * 1024
                    )
                    self.assertFalse(result["output_complete"])

    @unittest.skipIf(sys.platform == "win32", "POSIX process group contract")
    def test_native_child_exit_and_deadline_are_reported_after_cleanup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {"HOME": str(root), "PATH": "/usr/bin:/bin"}
            success = run_command(
                [sys.executable, "-c", "print('synthetic')"], env, root
            )
            self.assertEqual(success["exit_code"], 0)
            self.assertIsNone(success["error"])
            deadline = run_command(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                env,
                root,
                timeout=0.1,
            )
            self.assertEqual(deadline["error"], "process_timeout")
            self.assertEqual(deadline["exit_code"], -9)
