"""The schtasks query parser accepts every encoding the tool emits."""

from __future__ import annotations

import unittest

from muninn.obs_service import parse_task_query

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
