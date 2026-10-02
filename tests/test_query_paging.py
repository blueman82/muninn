"""Query contract: search output stays inside the byte budget."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any
from unittest.mock import patch

from tests.query_support import QueryCase


class SearchBytePagingTests(QueryCase):
    """Every search answer fits the 6 KiB wire budget on every page."""

    repo: int

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.add_scope("/repo")

    def pages(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch every page of a search for ``zebra`` and check the budget.

        Args:
            **kwargs: Extra ``search`` keywords, such as ``recent``.

        Returns:
            The pages in order, ending with the first page that has no more.
        """
        pages: list[dict[str, Any]] = []
        for number in range(1, 80):
            result = self.search("zebra", page=number, limit=10, **kwargs)
            self.assertNotIn("error", result)
            envelope = {"index_age_s": None, "poller": "stale"}
            wire = json.dumps(envelope | result | {"logged": False}) + "\n"
            self.assertLessEqual(len(wire.encode()), 6144)
            self.assertEqual(result["stages"]["returned"], len(result["hits"]))
            pages.append(result)
            if not result["has_more"]:
                break
        else:
            self.fail("pagination did not terminate")
        beyond = self.search("zebra", page=len(pages) + 1, limit=10, **kwargs)
        self.assertEqual(beyond["hits"], [])
        self.assertFalse(beyond["has_more"])
        return pages

    def test_heartbeat_changes_preserve_page_boundaries(self) -> None:
        fixtures: dict[int, tuple[str, list[int]]] = {}

        def fixture(padding: int) -> tuple[str, list[int]]:
            """Create, once, a session whose hits are ``padding`` bytes wide.

            Args:
                padding: Filler bytes appended to each of four hit texts.

            Returns:
                The session root and the four event ids in rank order.
            """
            if padding not in fixtures:
                session = f"age{padding:04d}"
                source = self.add_source(session)
                ids = [
                    self.add_event(
                        source, self.repo, f"zebra {i} " + "x" * padding
                    )
                    for i in range(4)
                ]
                fixtures[padding] = session, ids
            return fixtures[padding]

        transitions = (
            (400, 0),
            (0, 400),
            (9, 10),
            (10, 9),
            (99, 100),
            (100, 99),
            (999, 1000),
            (1000, 999),
        )
        with patch("time.time", return_value=10000):
            for before, after in transitions:
                statuses = [
                    {"last_pass_at": 10000 - age} for age in (before, after)
                ]
                # Locate the real two-hit byte boundary, independent of
                # notice wording and the size of the reserved envelope.
                low, high = 1800, 2800
                while high - low > 1:
                    middle = (low + high) // 2
                    session, _ = fixture(middle)
                    first = self.search(
                        "zebra", session=session, status=statuses[0]
                    )
                    if len(first["hits"]) >= 2:
                        low = middle
                    else:
                        high = middle
                for padding in (low, high):
                    with self.subTest(
                        before=before, after=after, padding=padding
                    ):
                        session, expected = fixture(padding)
                        seen = []
                        for number in range(1, 6):
                            age = before if number == 1 else after
                            status = statuses[0 if number == 1 else 1]
                            result = self.search(
                                "zebra",
                                session=session,
                                page=number,
                                status=status,
                            )
                            self.assertEqual(result["index_age_s"], age)
                            self.assertEqual(
                                result["poller"],
                                "ok" if age <= 180 else "stale",
                            )
                            wire = json.dumps(result | {"logged": False})
                            self.assertLessEqual(len(wire.encode()) + 1, 6144)
                            seen.extend(hit["id"] for hit in result["hits"])
                            if not result["has_more"]:
                                break
                        self.assertEqual(seen, expected)

    def test_unrepresentable_freshness_is_unknown_and_bounded(self) -> None:
        self.add_event(self.add_source("age"), self.repo, "zebra")
        with patch("time.time", return_value=0):
            for last in (
                -(2**63),
                -(10**400),
                -1e100,
                float("inf"),
                float("-inf"),
                float("nan"),
            ):
                with self.subTest(last=last):
                    result = self.search(
                        "zebra", status={"last_pass_at": last}
                    )
                    self.assertIsNone(result["index_age_s"])
                    self.assertEqual(result["poller"], "stale")
            result = self.search(
                "zebra", status={"last_pass_at": -(2**63 - 1)}
            )
            self.assertEqual(result["index_age_s"], 2**63 - 1)
            self.assertEqual(result["poller"], "stale")

    def test_large_utf8_hits_are_complete_and_ranked_on_every_page(
        self,
    ) -> None:
        expected = []
        for i in range(23):
            source = self.add_source(f"large{i:02d}")
            self.add_event(
                source,
                self.repo,
                "zebra " + "雪" * 130 + f" {i:02d}",
                ts=f"2026-09-01T00:{i:02d}:00Z",
            )
            expected.append(f"codex:large{i:02d}:1.1")
        for recent in (False, True):
            with self.subTest(recent=recent):
                pages = self.pages(recent=recent)
                refs = [hit["ref"] for page in pages for hit in page["hits"]]
                self.assertEqual(refs, expected[::-1] if recent else expected)
                self.assertEqual(len(refs), len(set(refs)))
                self.assertTrue(all(page["hits"] for page in pages))

    def test_session_drilldown_keeps_all_oversized_snippet_refs(self) -> None:
        source = self.add_source("big")
        for i in range(4):
            self.add_event(source, self.repo, "zebra " + "雪" * 5000 + f" {i}")
        pages = self.pages(session="big")
        hits = [hit for page in pages for hit in page["hits"]]
        self.assertEqual(
            [hit["ref"] for hit in hits],
            [f"codex:big:{i}.1" for i in range(1, 5)],
        )
        self.assertTrue(all(hit.get("snippet_truncated") for hit in hits))
        self.assertTrue(all("zebra" in hit["snippet"] for hit in hits))

    def test_knowledge_stays_on_first_page_without_skipping_hits(self) -> None:
        for _ in range(3):
            self.rw.execute(
                "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
                " created_at) VALUES (?, 'decision', ?, 'current', 'user', 0)",
                (self.repo, "zebra " + "雪" * 494),
            )
        for i in range(12):
            self.add_event(
                self.add_source(f"k{i:02d}"),
                self.repo,
                "zebra " + "雪" * 100 + f" {i:02d}",
            )
        pages = self.pages()
        self.assertEqual(len(pages[0]["knowledge"]), 3)
        self.assertTrue(
            any(k.get("text_truncated") for k in pages[0]["knowledge"])
        )
        self.assertEqual(
            [k["id"] for k in pages[0]["knowledge"]], ["K1", "K2", "K3"]
        )
        self.assertEqual(
            [
                r[0]
                for r in self.rw.execute(
                    "SELECT text FROM knowledge ORDER BY id"
                )
            ],
            ["zebra " + "雪" * 494] * 3,
        )
        self.assertTrue(all(not page["knowledge"] for page in pages[1:]))
        self.assertEqual(
            [h["ref"] for p in pages for h in p["hits"]],
            [f"codex:k{i:02d}:1.1" for i in range(12)],
        )

    def test_irreducible_metadata_returns_bounded_actionable_error(
        self,
    ) -> None:
        self.add_event(
            self.add_source("huge"), self.repo, "zebra", cwd="x" * 9000
        )
        result = self.search("zebra")
        self.assertEqual(result.get("error"), "output_too_large")
        self.assertTrue(result.get("note"))
        self.assertLessEqual(len(json.dumps(result).encode()), 6144)

    def test_oversized_knowledge_is_explicit_and_later_hits_reachable(
        self,
    ) -> None:
        self.rw.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at) VALUES (?, 'decision', 'zebra', 'current', ?, 0)",
            (self.repo, "actor" * 2000),
        )
        self.add_event(self.add_source("later"), self.repo, "zebra")
        result = self.search("zebra")
        self.assertEqual(result.get("error"), "output_too_large")
        self.assertTrue(result.get("has_more"))
        self.assertLessEqual(len(json.dumps(result).encode()), 6144)
        later = self.search("zebra", page=2)
        self.assertEqual(
            [h["ref"] for h in later["hits"]], ["codex:later:1.1"]
        )
        self.assertFalse(later["has_more"])

    def test_far_past_end_page_includes_requested_number_in_budget(
        self,
    ) -> None:
        cwd = "/" + "x" * 5200
        self.add_scope(cwd)
        result = self.search("zebra", cwd=cwd, page=10**600)
        self.assertLessEqual(len(json.dumps(result).encode()), 6144)
        self.assertEqual(result.get("error"), "output_too_large")

    def test_actual_cli_exact_boundary_and_oversized_error(self) -> None:
        source = self.add_source("edge")
        self.add_event(source, self.repo, "zebra " + "x" * 9000)
        env = os.environ | {
            "PCTX_HOME": str(self.home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        command = [
            sys.executable,
            "-m",
            "pctx",
            "search",
            "zebra",
            "--all-projects",
        ]
        snippets = []
        # The last status consumes the full reserved 19-digit age envelope.
        for last, poller in (
            (time.time(), "ok"),
            (time.time() - 400, "stale"),
            (-1e100, "stale"),
            (None, "stale"),
            (-1e18, "stale"),
        ):
            (self.home / "status.json").write_text(
                json.dumps({"last_pass_at": last})
            )
            done = subprocess.run(
                command, env=env, capture_output=True, check=True
            )
            result = json.loads(done.stdout)
            self.assertEqual(done.stderr, b"")
            self.assertLessEqual(len(done.stdout), 6144)
            self.assertEqual(result["poller"], poller)
            if last in (None, -1e100):
                self.assertIsNone(result["index_age_s"])
            snippets.append(result["hits"][0]["snippet"])
        self.assertEqual(len(set(snippets)), 1)
        self.assertGreaterEqual(result["index_age_s"], 10**18)
        self.assertGreaterEqual(len(done.stdout), 6142)
        self.assertLessEqual(len(done.stdout), 6144)
        self.assertEqual(result["hits"][0]["ref"], "codex:edge:1.1")
        self.assertTrue(result["hits"][0]["snippet_truncated"])
        self.assertFalse(result["has_more"])
        self.add_event(source, self.repo, "giraffe", cwd="x" * 9000)
        command[4] = "giraffe"
        done = subprocess.run(command, env=env, capture_output=True)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stderr, b"")
        self.assertLessEqual(len(done.stdout), 6144)
        result = json.loads(done.stdout)
        self.assertEqual(result["error"], "output_too_large")
        self.assertIn("pctx open", result["note"])

    def test_actual_cli_output_stays_bounded_after_redaction(self) -> None:
        for i in range(23):
            self.add_event(
                self.add_source(f"cli{i:02d}"),
                self.repo,
                "zebra " + "雪" * 80 + " token=x " * 8 + f" {i:02d}",
            )
        self.rw.commit()
        refs = []
        env = os.environ | {
            "PCTX_HOME": str(self.home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for number in range(1, 30):
            done = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pctx",
                    "search",
                    "zebra",
                    "--all-projects",
                    "--limit",
                    "30",
                    "--page",
                    str(number),
                ],
                env=env,
                capture_output=True,
                check=True,
            )
            self.assertEqual(done.stderr, b"")
            self.assertLessEqual(len(done.stdout), 6144)
            result = json.loads(done.stdout)
            self.assertNotIn("token=x", done.stdout.decode())
            refs.extend(hit["ref"] for hit in result["hits"])
            if not result["has_more"]:
                break
        self.assertEqual(refs, [f"codex:cli{i:02d}:1.1" for i in range(23)])
