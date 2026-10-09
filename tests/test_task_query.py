"""The schtasks query parser accepts every encoding the tool emits."""

from __future__ import annotations

import subprocess
import unittest
import xml.etree.ElementTree as ET
from collections.abc import Sequence

from muninn.obs_service import (
    canonical_trigger_user,
    command_names_path,
    disable_task,
    parse_task_query,
    task_matches,
)

_BODY = '<Task><Principals><Principal id="User"/></Principals></Task>'
_URI = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_DECLARATION = '<?xml version="1.0" encoding="UTF-16"?>'


class ParseTaskQueryTests(unittest.TestCase):
    """Declared UTF-16 must not matter when the bytes are something else."""

    def test_each_real_encoding_parses(self) -> None:
        text = _DECLARATION + "\r\n" + _BODY
        for raw in (
            text.encode("utf-16"),
            text.encode("utf-8"),
            ("﻿" + text).encode("utf-8"),
            text.encode("cp1252"),
        ):
            with self.subTest(prefix=raw[:4]):
                self.assertEqual(parse_task_query(raw).tag, "Task")

    def test_bare_document_parses(self) -> None:
        self.assertEqual(parse_task_query(_BODY.encode()).tag, "Task")

    def test_omitted_default_elements_match_their_defaults(self) -> None:
        ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
        body = (
            f'<Task xmlns="{ns}"><Principals><Principal><UserId>S-1-5-21-1'
            "</UserId><LogonType>InteractiveToken</LogonType>%s</Principal>"
            "</Principals><Actions><Exec><Command>c</Command><Arguments>a"
            "</Arguments></Exec></Actions><Triggers><LogonTrigger>%s"
            "</LogonTrigger></Triggers><Settings>%s</Settings></Task>"
        )
        full = body % (
            "<RunLevel>LeastPrivilege</RunLevel>",
            "<Enabled>true</Enabled>",
            "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
            "<Enabled>true</Enabled>",
        )
        stored = body % ("", "", "")
        self.assertTrue(
            task_matches(ET.fromstring(full), ET.fromstring(stored))
        )
        other = full.replace("IgnoreNew", "Parallel")
        self.assertFalse(
            task_matches(ET.fromstring(other), ET.fromstring(stored))
        )


class CanonicalTriggerUserTests(unittest.TestCase):
    """A trigger user reported by name is compared by its resolved SID."""

    _XML = (
        f'<Task xmlns="{_URI}"><Triggers><LogonTrigger>'
        "<UserId>HOST\\u</UserId></LogonTrigger></Triggers></Task>"
    )

    def test_name_becomes_sid_and_failures_leave_it(self) -> None:
        """Only a successful SID answer rewrites the stored name."""
        for code, out, wanted in (
            (0, b"S-1-5-21-1-2-3-1001\r\n", "S-1-5-21-1-2-3-1001"),
            (1, b"", "HOST\\u"),
            (0, b"garbage", "HOST\\u"),
        ):

            def run(
                command: Sequence[str],
                code: int = code,
                out: bytes = out,
            ) -> subprocess.CompletedProcess[bytes]:
                """Answer the lookup with a fixed result."""
                return subprocess.CompletedProcess(command, code, out, b"")

            root = ET.fromstring(self._XML)
            canonical_trigger_user(root, run)
            self.assertEqual(
                root.findtext(
                    "t:Triggers/t:LogonTrigger/t:UserId", None, {"t": _URI}
                ),
                wanted,
            )


class DisableTaskTests(unittest.TestCase):
    """Disabling works whether or not the scheduler kept the element."""

    def test_omitted_and_present_enabled_become_false(self) -> None:
        """An omitted default is added; a present one is rewritten."""
        for inner in ("", "<Enabled>true</Enabled>"):
            root = ET.fromstring(
                f'<Task xmlns="{_URI}"><Settings>{inner}</Settings></Task>'
            )
            disable_task(root)
            self.assertEqual(
                root.findtext("t:Settings/t:Enabled", None, {"t": _URI}),
                "false",
            )

    def test_missing_settings_is_refused(self) -> None:
        """A definition without settings is not silently accepted."""
        with self.assertRaises(ValueError):
            disable_task(ET.fromstring(f'<Task xmlns="{_URI}"/>'))


class CommandNamesPathTests(unittest.TestCase):
    """A command line names a release only by an absolute path."""

    def test_literal_and_absolute_tokens_match(self) -> None:
        """The literal text or an absolute token under the root matches."""
        root = "/lib/release"
        self.assertTrue(command_names_path(f"py -I {root}/bin/x", root))
        self.assertTrue(command_names_path('py "/lib/release/bin/x"', root))

    def test_relative_tokens_never_match(self) -> None:
        """Flags and relative names are not resolved against the caller."""
        self.assertFalse(command_names_path("py -I --go release", "/lib/rel"))
        self.assertFalse(command_names_path("py .", "/"))
