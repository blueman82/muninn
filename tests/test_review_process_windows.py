"""Check atomic Windows Job attributes with a deterministic native seam."""

from __future__ import annotations

import ctypes
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from muninn import review_process_windows as windows


class ReviewProcessWindowsTests(unittest.TestCase):
    """Never create an uncontained process and assign it afterward."""

    def test_atomic_job_assignment_has_no_orphan_window(self) -> None:
        kernel = mock.Mock()
        kernel.InitializeProcThreadAttributeList.return_value = True
        kernel.UpdateProcThreadAttribute.return_value = True
        calls: list[int] = []

        def update(*args: Any) -> bool:
            calls.append(int(args[2]))
            return True

        kernel.UpdateProcThreadAttribute.side_effect = update
        with windows.attributes(kernel, 123, (1, 2, 3)) as pointer:
            self.assertIsNotNone(pointer)
            self.assertEqual(calls, [0x2000D, 0x20002])
        kernel.DeleteProcThreadAttributeList.assert_called_once()
        kernel.AssignProcessToJobObject.assert_not_called()

    def test_attribute_failure_prevents_process_creation(self) -> None:
        kernel = mock.Mock()
        kernel.InitializeProcThreadAttributeList.return_value = True
        kernel.UpdateProcThreadAttribute.return_value = False
        with (
            self.assertRaisesRegex(OSError, "atomic_job_unavailable"),
            windows.attributes(kernel, 123, (1, 2, 3)),
        ):
            kernel.CreateProcessW()
        kernel.CreateProcessW.assert_not_called()

    def test_job_uses_kill_on_close_without_inheritance(self) -> None:
        kernel = mock.Mock()
        kernel.CreateJobObjectW.return_value = 123
        kernel.SetInformationJobObject.return_value = True
        flags: list[int] = []

        def configure(job: int, kind: int, info: Any, size: int) -> bool:
            value = ctypes.cast(
                info, ctypes.POINTER(windows.JobLimits)
            ).contents
            flags.append(value.basic.flags)
            self.assertEqual(
                (job, kind, size), (123, 9, ctypes.sizeof(windows.JobLimits))
            )
            return True

        kernel.SetInformationJobObject.side_effect = configure
        self.assertEqual(windows.create_job(kernel), 123)
        self.assertEqual(flags, [0x2000])
        kernel.CreateJobObjectW.assert_called_once_with(None, None)

    def test_create_process_receives_atomic_job_and_exact_handle_list(
        self,
    ) -> None:
        kernel = mock.Mock()
        kernel.CreateJobObjectW.return_value = 123
        kernel.SetInformationJobObject.return_value = True
        kernel.InitializeProcThreadAttributeList.return_value = True
        order: list[int] = []

        def update(*args: Any) -> bool:
            order.append(args[2])
            if args[2] == 0x20002:
                handles = ctypes.cast(
                    args[3], ctypes.POINTER(ctypes.c_void_p * 3)
                ).contents
                self.assertEqual(len(set(handles)), 3)
                self.assertNotIn(123, handles)
            return True

        def create(*args: Any) -> bool:
            self.assertEqual(order, [0x2000D, 0x20002])
            self.assertEqual(args[5], 0x80000 | 0x400)
            self.assertTrue(args[4])
            startup = ctypes.cast(
                args[8], ctypes.POINTER(windows._StartupEx)
            ).contents
            self.assertEqual(startup.startup.flags, 0x100)
            self.assertTrue(startup.attributes)
            information = ctypes.cast(
                args[9], ctypes.POINTER(windows._ProcessInfo)
            ).contents
            information.process, information.thread, information.pid = (
                456,
                789,
                2,
            )
            return True

        kernel.UpdateProcThreadAttribute.side_effect = update
        kernel.CreateProcessW.side_effect = create
        with (
            mock.patch.object(windows, "_kernel", return_value=kernel),
            mock.patch.object(windows, "_handle", side_effect=lambda fd: fd),
        ):
            child = windows.launch(("fixture.exe",), Path("/fixture"), {})
        self.assertEqual(child.pid, 2)
        child.close()
        self.assertEqual(
            [call.args[0] for call in kernel.CloseHandle.call_args_list],
            [789, 123, 456],
        )
        kernel.AssignProcessToJobObject.assert_not_called()
        kernel.ResumeThread.assert_not_called()
