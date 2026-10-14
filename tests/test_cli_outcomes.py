"""``muninn stats`` retrieval outcome counts from the call logs."""

from __future__ import annotations

import json

from tests.cli_support import CliCase

TORN = '{"cmd": "search", "stag'
ZEROS = {"calls": 0, "zero": 0, "outside": 0}


def line(cmd: str, **extra: object) -> str:
    """Return one call-log line.

    Args:
        cmd: Command name.
        **extra: Further fields of the line.

    Returns:
        The JSON line.
    """
    return json.dumps({"cmd": cmd, **extra})


class OutcomeStatsTests(CliCase):
    """Outcome counts are one pass over both call-log files."""

    def stats(self) -> dict[str, object]:
        """Run ``stats`` with its own call left out of the log.

        Returns:
            The parsed report.
        """
        code, out, _ = self.muninn("stats", env={"MUNINN_NO_CALLLOG": "1"})
        self.assertEqual(code, 0, out)
        return out

    def test_counts_both_files_and_tolerates_torn_lines(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        old = [
            line("search", stages={"miss_zero": 1}),
            line("hook prompt", hash_ok=False, stages={"miss_floor": 1}),
            TORN,
        ]
        new = [
            line("search", stages={"miss_outside": 1, "kept": 3}),
            line("search", stages={"miss_noterms": 1}),
            line("search"),
            line("hook session", stages={"miss_zero": 1}),
            line("hook prompt", stages={"miss_zero": 1}),
            line("hook prompt", stages={"miss_outside": 1}),
            "[1, 2]",
            "",
        ]
        (self.home / "calls.jsonl.1").write_text("\n".join(old) + "\n")
        (self.home / "calls.jsonl").write_text("\n".join(new))
        out = self.stats()
        self.assertEqual(out["hash_mismatches"], 1)
        self.assertEqual(
            out["retrieval_outcomes"],
            {
                "window_lines": 8,
                "search": ZEROS
                | {"calls": 4, "zero": 1, "outside": 1}
                | {"noterms": 1},
                "hook_prompt": ZEROS
                | {"calls": 3, "zero": 1, "outside": 1}
                | {"floor": 1},
            },
        )

    def test_no_logs_gives_zeros(self) -> None:
        self.assertEqual(
            self.stats()["retrieval_outcomes"],
            {
                "window_lines": 0,
                "search": ZEROS | {"noterms": 0},
                "hook_prompt": ZEROS | {"floor": 0},
            },
        )

    def test_non_object_lines_and_hook_without_stages(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        rows = ["5", "[1, 2]", '"x"', "null", line("hook prompt")]
        (self.home / "calls.jsonl").write_text("\n".join(rows) + "\n")
        self.assertEqual(
            self.stats()["retrieval_outcomes"],
            {
                "window_lines": 1,
                "search": ZEROS | {"noterms": 0},
                "hook_prompt": ZEROS | {"calls": 1, "floor": 0},
            },
        )
