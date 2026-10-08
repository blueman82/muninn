"""The schtasks query parser accepts every encoding the tool emits."""

from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from muninn.obs_service import parse_task_query, task_matches

_BODY = '<Task><Principals><Principal id="User"/></Principals></Task>'
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
