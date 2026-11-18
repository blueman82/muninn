"""Check the read-only measuring tool, which only reports the cost."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from tests.classify_support import custom_call
from tests.ingest_support import Record, line
from tests.test_classify import reply, user_msg
from tests.test_review_requests import RequestCase
from tools import measure_history_windows

STATS = ("p50", "p75", "p90", "p95", "p99", "max")


def turns(count: int, lines: int, size: int) -> list[Record]:
    """Build equal owner turns: prompt, tool records, reply."""
    records: list[Record] = []
    for index in range(count):
        base = 1 + index * lines
        records.append(user_msg(base, "q"))
        records.extend(
            custom_call(base + 1 + n, "run", "x" * size)
            for n in range(lines - 2)
        )
        records.append(reply(base + lines - 1, "a"))
    return records


class MeasureTests(RequestCase):
    """Measure disposable sessions; nothing outside them may change."""

    def save(self, records: list[Record]) -> Path:
        """Write the header and records as the session file."""
        self.path.write_bytes(b"".join(line(r) for r in records))
        return self.path

    def windows(
        self, records: list[Record], limit: int = 1_048_576
    ) -> dict[str, int]:
        """Measure records and return the windows block."""
        path = self.save([self.records[0], *records])
        return measure_history_windows.measure([path], limit)["windows"]

    def test_has_no_cap_or_selection_api(self) -> None:
        for name in ("SWEEP_CAPS", "select_cap"):
            self.assertFalse(hasattr(measure_history_windows, name))
        path = self.save([self.records[0], *turns(3, 10, 50)])
        result = measure_history_windows.measure([path])
        self.assertNotIn("sweep", result)
        self.assertEqual(
            set(result["windows"]),
            {
                "parts", "excluded_evidence_limit", "cuts",
                "cuts_with_full_previous_turn", "cuts_shortened",
                "bytes_new", "bytes_sent",
            },
        )

    def test_equal_turns_report_one_value_per_statistic(self) -> None:
        path = self.save([self.records[0], *turns(3, 10, 50)])
        result = measure_history_windows.measure([path])
        self.assertEqual(result["files"], 1)
        shown = result["turns"]
        self.assertEqual(shown["count"], 3)
        for name in STATS:
            self.assertEqual(shown["lines"][name], 10)
        self.assertEqual(len({shown["bytes"][n] for n in STATS}), 1)
        self.assertGreater(shown["bytes"]["max"], 10 * 50)

    def test_small_session_has_no_cut_and_no_overlap_cost(self) -> None:
        shown = self.windows(turns(3, 10, 50))
        self.assertEqual(shown["parts"], 1)
        self.assertEqual(shown["excluded_evidence_limit"], 0)
        self.assertEqual(shown["cuts"], 0)
        self.assertEqual(shown["cuts_with_full_previous_turn"], 0)
        self.assertEqual(shown["cuts_shortened"], 0)
        self.assertEqual(shown["bytes_sent"], shown["bytes_new"])
        self.assertGreater(shown["bytes_sent"], 10 * 50)

    def test_cuts_split_into_full_turn_and_shortened(self) -> None:
        for records in (turns(14, 34, 3000), turns(8, 108, 2600)):
            shown = self.windows(records)
            self.assertGreater(shown["cuts"], 0)
            self.assertEqual(shown["parts"], shown["cuts"] + 1)
            self.assertEqual(
                shown["cuts"],
                shown["cuts_with_full_previous_turn"]
                + shown["cuts_shortened"],
            )
            self.assertEqual(shown["excluded_evidence_limit"], 0)

    def test_whole_turn_cuts_and_shortened_cuts_are_told_apart(self) -> None:
        # About 102 KB, 34 lines: the whole turn and a new turn fit.
        fits = self.windows(turns(14, 34, 3000))
        self.assertEqual(fits["cuts_with_full_previous_turn"], fits["cuts"])
        self.assertEqual(fits["cuts_shortened"], 0)
        # 108 lines, about 281 KB: the turn and a new turn do not fit
        # together, so the overlap is shortened.
        long = self.windows(turns(8, 108, 2600))
        self.assertEqual(long["cuts_with_full_previous_turn"], 0)
        self.assertEqual(long["cuts_shortened"], long["cuts"])

    def test_overlap_cost_is_sent_minus_new(self) -> None:
        shown = self.windows(turns(14, 34, 3000))
        self.assertGreater(shown["bytes_sent"], shown["bytes_new"])
        # Each original byte is a new byte once.
        self.assertLessEqual(shown["bytes_new"], self.path.stat().st_size)

    def test_layout_gives_numbers_for_one_file(self) -> None:
        path = self.save([self.records[0], *turns(14, 34, 3000)])
        view = measure_history_windows.layout(path)
        self.assertEqual(
            set(view),
            {"lines", "line_bytes", "indexed", "turn_starts", "windows"},
        )
        self.assertEqual(len(view["line_bytes"]), view["lines"])
        self.assertEqual(view["indexed"], sorted(set(view["indexed"])))
        self.assertTrue(
            all(1 <= n <= view["lines"] for n in view["indexed"])
        )
        self.assertTrue(
            set(view["turn_starts"]) <= set(view["indexed"])
        )
        for win in view["windows"]:
            self.assertIn(win["from"], view["indexed"])
            self.assertIn(win["through"], view["indexed"])
            self.assertIn(win["new_from"], view["indexed"])
        self.assertEqual(len(view["turn_starts"]), 14)
        first = view["windows"][0]
        self.assertEqual(
            set(first),
            {
                "from", "through", "new_from", "cut", "excluded",
                "limited",
            },
        )
        self.assertFalse(any(w["limited"] for w in view["windows"]))
        self.assertFalse(first["cut"])
        self.assertTrue(any(w["cut"] for w in view["windows"]))

    def test_oversize_line_is_counted_as_excluded(self) -> None:
        records = [
            user_msg(1, "q"),
            reply(2, "y" * 150_000),
            user_msg(3, "q"),
            reply(4, "a"),
        ]
        shown = self.windows(records, 100_000)
        self.assertEqual(shown["excluded_evidence_limit"], 1)

    def test_cli_prints_json_and_refuses_a_cap_option(self) -> None:
        path = self.save([self.records[0], *turns(14, 34, 3000)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = measure_history_windows.main([str(path)])
        self.assertEqual(code, 0)
        shown = json.loads(out.getvalue())
        self.assertGreater(shown["windows"]["cuts"], 0)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            measure_history_windows.main(["--cap", "0", str(path)])

    def test_folder_input_and_no_change_to_inputs_or_store(self) -> None:
        path = self.save([self.records[0], *turns(3, 10, 50)])
        before = path.read_bytes()
        home = sorted(p.name for p in self.home.iterdir())
        result = measure_history_windows.measure([path.parent])
        self.assertGreaterEqual(result["files"], 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), home)
