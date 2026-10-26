"""Require complete HTTP bodies and observed native child completion."""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

from tools.learning_eval.codex_tool_probe_io import (
    capture_handler,
    run_command,
)


class ProbeCompletionTests(unittest.TestCase):
    """Exercise EOF boundaries with real sockets and owned children."""

    def test_windows_command_is_refused_before_child_spawn(self) -> None:
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch(
                "subprocess.Popen",
                side_effect=AssertionError("spawn_before_refusal"),
            ) as spawn,
            self.assertRaisesRegex(ValueError, "native_probe_posix_only"),
        ):
            run_command([], {}, Path())
        spawn.assert_not_called()

    @unittest.skipIf(sys.platform == "win32", "POSIX process group contract")
    def test_pipe_eof_waits_for_actual_exit_or_reports_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for delay, timeout, error, code in (
                (0.1, 5.0, None, 7),
                (60, 0.2, "process_timeout", -9),
            ):
                with self.subTest(delay=delay):
                    script = (
                        "import os,time; os.read(0,4096); "
                        "os.close(1); os.close(2); "
                        f"time.sleep({delay}); os._exit(7)"
                    )
                    result = run_command(
                        [sys.executable, "-c", script],
                        {"HOME": str(root), "PATH": os.defpath},
                        root,
                        timeout=timeout,
                    )
                    self.assertEqual(result["error"], error)
                    self.assertEqual(result["exit_code"], code)
                    self.assertEqual(result["output_complete"], error is None)
                    self.assertEqual(
                        result["cleanup"],
                        "group_signal_attempted_before_leader_reap",
                    )
                    self.assertEqual(
                        result["descendant_quiescence"], "unverified"
                    )

    def test_short_declared_body_is_not_valid_request_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records: list[dict[str, object]] = []
            with HTTPServer(
                ("127.0.0.1", 0), capture_handler(records, root)
            ) as server:
                worker = threading.Thread(target=server.serve_forever)
                worker.start()
                try:
                    with socket.create_connection(
                        ("127.0.0.1", server.server_port), timeout=5
                    ) as client:
                        raw = b'{"tools":[]}'
                        headers = (
                            "POST /responses HTTP/1.1\r\nHost: localhost\r\n"
                            f"Content-Length: {len(raw) + 10}\r\n\r\n"
                        ).encode()
                        client.sendall(headers + raw)
                        client.shutdown(socket.SHUT_WR)
                        while client.recv(4096):
                            pass
                    self.assertEqual(len(records), 1)
                    self.assertEqual(
                        records[0].get("error"), "request_body_incomplete"
                    )
                    self.assertNotIn("tool_names", records[0])
                    self.assertEqual(
                        (root / "request-1.json").read_bytes(), raw
                    )
                finally:
                    server.shutdown()
                    worker.join(timeout=5)
