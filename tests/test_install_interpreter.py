"""Native Linux interpreter refusal precedes installer lifecycle effects."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from install import lifecycle
from install.context import Ctx, StepFailedError, run_real
from muninn import obs_linux_service


class InterpreterGuardTests(unittest.TestCase):
    """Use exact mode metadata and distinguish refusal from probe execution."""

    def test_regular_executable_cannot_be_group_or_other_writable(
        self,
    ) -> None:
        for mode in (0o755, 0o775, 0o757, stat.S_IFDIR | 0o755):
            info = os.stat_result(
                (stat.S_IFREG | mode, 1, 1, 1, 0, 0, 0, 0, 0, 0)
            )
            with (
                self.subTest(mode=mode),
                patch.object(Path, "stat", return_value=info),
            ):
                if mode == 0o755:
                    obs_linux_service.require_interpreter(
                        Path("/synthetic/python")
                    )
                else:
                    with self.assertRaisesRegex(
                        ValueError, "recorded_interpreter_unsafe"
                    ):
                        obs_linux_service.require_interpreter(
                            Path("/synthetic/python")
                        )

    def test_preflight_refuses_current_interpreter_before_any_runner_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = Ctx(Path(temporary), run_real, "linux", platform="linux")
            with (
                patch.object(lifecycle.sys, "platform", "linux"),
                patch.object(
                    lifecycle,
                    "require_interpreter",
                    side_effect=ValueError("recorded_interpreter_unsafe"),
                ) as guard,
                patch.object(lifecycle, "must") as run,
                self.assertRaises(StepFailedError),
            ):
                lifecycle.preflight_service(ctx)
            guard.assert_called_once()
            run.assert_not_called()
            self.assertFalse(ctx.lib.exists())
            self.assertFalse(ctx.data.exists())

    def test_start_refuses_before_service_metadata_or_unit_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = Ctx(Path(temporary), run_real, "linux", platform="linux")
            python = Path("/synthetic/python")
            with (
                patch.object(lifecycle.sys, "platform", "linux"),
                patch.object(
                    lifecycle,
                    "require_interpreter",
                    side_effect=ValueError("recorded_interpreter_unsafe"),
                ) as guard,
                patch.object(lifecycle, "write_private") as write,
                patch.object(lifecycle, "must") as run,
                self.assertRaises(StepFailedError),
            ):
                lifecycle.start(ctx, python, Path("/synthetic/release"))
            guard.assert_called_once_with(python)
            write.assert_not_called()
            run.assert_not_called()
            self.assertFalse(ctx.lib.exists())
            self.assertFalse(ctx.plist.exists())
