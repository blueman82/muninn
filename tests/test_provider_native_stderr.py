"""Captured synthetic stderr is classified without exporting message bodies."""

from __future__ import annotations

import unittest

from tests.provider_native_stderr import classify


def clixml(records: bytes) -> bytes:
    """Build one synthetic PowerShell stream document."""
    return (
        b'#< CLIXML\r\n<Objs xmlns="http://schemas.microsoft.com/powershell/2004/04">'
        + records
        + b"</Objs>"
    )


class StderrCodesTest(unittest.TestCase):
    """Progress can only be proven when every bounded stream is recognized."""

    def test_all_progress_records_emit_only_counts(self) -> None:
        raw = clixml(
            b'<Obj S="progress"><S>private transcript and credential</S></Obj>'
            b'<Obj S="progress"/>'
        )
        result = classify(raw)
        self.assertEqual(
            classify(b"\xef\xbb\xbf" + raw)["stderr_progress_only"], 1
        )
        self.assertEqual(
            result,
            {
                "stderr_bytes": len(raw),
                "stderr_clixml": 1,
                "stderr_progress_only": 1,
                "stderr_error_count": 0,
                "stderr_other_count": 0,
            },
        )

    def test_error_or_unknown_records_are_not_progress_only(self) -> None:
        error = classify(
            clixml(b'<Obj S="progress"/><S S="Error">private</S>')
        )
        self.assertEqual(error["stderr_error_count"], 1)
        self.assertEqual(error["stderr_progress_only"], 0)
        unknown = classify(
            clixml(b'<Obj S="progress"/><Obj S="unrecognized"/>')
        )
        self.assertEqual(unknown["stderr_other_count"], 1)
        self.assertEqual(unknown["stderr_progress_only"], 0)

    def test_malformed_oversized_or_non_xml_are_unknown(self) -> None:
        for raw in (
            b"private stderr",
            b"\xef" + clixml(b'<Obj S="progress"/>'),
            b"#< CLIXML\nnot XML",
            clixml(b""),
            clixml(b"<unknown S='progress'/>"),
            b"#< CLIXML\n" + b"x" * 65536,
        ):
            result = classify(raw)
            self.assertEqual(result["stderr_progress_only"], 0)
            self.assertGreater(result["stderr_other_count"], 0)
        self.assertEqual(classify(b"")["stderr_bytes"], 0)
