"""Query contract: opening one event, paging and raw lines."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from typing import Any

from tests.hook_nonregular import nonregular
from tests.ingest_support import ROOT
from tests.query_support import NOTICE, OpenCase, open_specs, sha


class OpenTests(OpenCase):
    """Opening an event: provenance, neighbours, refs and hash checks."""

    def test_open_byte_exact_and_neighbours_in_source_order(self) -> None:
        _, ids, lines, offsets = self.write_source(
            "thr-abcdef", open_specs(), session="sess-123456", backwards=True
        )
        got = self.open("codex:thr-abcdef:6.1")
        self.assertEqual(got["notice"], NOTICE)
        self.assertEqual(got["id"], ids[6])
        self.assertEqual(got["ref"], "codex:thr-abcdef:6.1")
        self.assertEqual(got["text"], open_specs()[6][0])  # byte-exact
        self.assertIsNone(got["next_offset"])
        self.assertEqual(
            got["provenance"],
            {
                "provider": "codex",
                "thread": "thr-abcdef",
                "session": "sess-123456",
                "ts": "2026-09-30T10:00:00.000Z",
                "role": "user",
                "kind": "prompt",
                "tag": None,
                "scope": "repo",
                "cwd": "/repo/sub",
                "root": "codex-sessions",
                "path": "thr-abcdef.jsonl",
                "line": 6,
                "part": 1,
                "byte_offset": offsets[6],
                "line_sha256": sha(lines[6]),
                "source_status": "active",
            },
        )
        self.assertTrue(got["hash_ok"])
        near = [(n["rel"], n["id"]) for n in got["neighbours"]]
        # ids run backwards; source order is (line, part): 3.2 comes after 3.1
        want = [(-3, 3), (-2, 4), (-1, 5), (1, 7), (2, 8), (3, 9)]
        self.assertEqual(near, [(rel, ids[i]) for rel, i in want])

    def test_neighbours_labels_and_context_bounds(self) -> None:
        _, ids, _, _ = self.write_source("thr-b", open_specs())
        got = self.open(ids[6], context=3)
        labelled = {n["id"]: (n["kind"], n["tag"]) for n in got["neighbours"]}
        self.assertEqual(labelled[ids[5]], ("tool_error", "Bash"))
        self.assertEqual(labelled[ids[4]], ("harness", "environment_context"))
        first = got["neighbours"][0]
        self.assertEqual(
            set(first),
            {
                "id",
                "rel",
                "ts",
                "role",
                "kind",
                "tag",
                "preview",
                "flagged",
                "answer_citable",
            },
        )
        self.assertEqual(self.open(ids[6], context=0)["neighbours"], [])
        one = self.open(str(ids[6]), context=1)["neighbours"]
        self.assertEqual([n["rel"] for n in one], [-1, 1])
        edge = self.open(ids[0], context=2)["neighbours"]
        self.assertEqual([n["rel"] for n in edge], [1, 2])  # nothing before
        many = [("row", {}) for _ in range(50)]
        _, ids, _, _ = self.write_source("thr-many", many)
        rels = [n["rel"] for n in self.open(ids[25], context=99)["neighbours"]]
        self.assertEqual(rels, [*range(-20, 0), *range(1, 21)])

    def test_neighbours_within_a_line_follow_part_order(self) -> None:
        _, ids, _, _ = self.write_source("thr-w", open_specs(), backwards=True)
        got = self.open(ids[1], context=2)  # line 2; line 3 holds parts 1, 2
        self.assertEqual(
            [(n["rel"], n["id"]) for n in got["neighbours"]],
            [(-1, ids[0]), (1, ids[2]), (2, ids[3])],
        )
        back = self.open(ids[4], context=2)  # line 4: parts 3.2, 3.1 before
        self.assertEqual(
            [(n["rel"], n["id"]) for n in back["neighbours"][:2]],
            [(-2, ids[2]), (-1, ids[3])],
        )

    def test_negative_context_is_zero_and_a_subagent_is_marked(self) -> None:
        sub = self.add_source("thr-sub", cls="subagent")
        eid = self.add_event(sub, self.repo, "delegated", kind="delegation")
        self.add_event(sub, self.repo, "and more", kind="reply")
        got = self.open(eid, context=-5)
        self.assertEqual(got["neighbours"], [])
        self.assertEqual(got["provenance"]["class"], "subagent")
        self.assertEqual(len(self.open(eid, context=1)["neighbours"]), 1)

    def test_exact_thread_beats_a_longer_thread_with_that_prefix(self) -> None:
        _, short, _, _ = self.write_source("thr-x", open_specs()[:1])
        self.write_source("thr-xy", open_specs()[:1])
        self.assertEqual(self.open("codex:thr-x:1.1")["id"], short[0])
        self.assertEqual(
            self.open("codex:thr-x:1"), self.open("codex:thr-x:1.1")
        )

    def test_a_line_over_the_ingest_cap_never_verifies(self) -> None:
        big = "z" * (8 * 1024 * 1024 + 5)
        _, ids, lines, _ = self.write_source("thr-huge", [(big, {})])
        self.assertGreater(len(lines[0]), 8 * 1024 * 1024)
        got = self.open(ids[0], raw=True)  # its stored hash matches the file
        self.assertIs(got["hash_ok"], False)
        self.assertNotIn("raw", got)

    def test_previews_are_one_line_and_at_most_200_chars(self) -> None:
        specs = [("x", {}), ("word  \n\n  " * 100, {}), ("tail", {})]
        _, ids, _, _ = self.write_source("thr-p", specs)
        prev = self.open(ids[0], context=1)["neighbours"][0]["preview"]
        self.assertNotIn("\n", prev)
        self.assertNotIn("  ", prev)
        self.assertEqual(len(prev), 200)

    def test_refs_ids_prefixes_and_errors(self) -> None:
        _, ids, _, _ = self.write_source("thr-abc111", open_specs()[:2])
        self.write_source("thr-abc222", open_specs()[:1])
        want = ids[1]
        for ref in (
            "codex:thr-abc111:2.1",
            "codex:thr-abc111:2",
            "codex:thr-abc1:2.1",  # unambiguous prefix
            str(want),
            want,
        ):
            with self.subTest(ref):
                self.assertEqual(self.open(ref)["id"], want)
        codes = {
            "codex:thr-abc:1.1": "ambiguous_ref",  # matches both threads
            "codex:thr-abc111:99.1": "not_found",
            "codex:nothing:1.1": "not_found",
            "claude:thr-abc111:1.1": "not_found",
            "999999": "not_found",
            "garbage": "bad_ref",
            "": "bad_ref",
            "9" * 30: "bad_ref",  # too big for an id, and not a REF
            "codex:thr-abc111:99999999999999999999.1": "bad_ref",
        }
        for ref, code in codes.items():
            with self.subTest(ref):
                self.assertEqual(
                    self.open(ref), {"error": code, "notice": NOTICE}
                )

    def test_open_hash_ok_false_after_rewrite(self) -> None:
        _, ids, _, offsets = self.write_source("thr-h", open_specs())
        path = self.sessions / "thr-h.jsonl"
        self.assertTrue(self.open(ids[6])["hash_ok"])
        original = path.read_bytes()
        changed = bytearray(original)
        changed[offsets[6] + 12] ^= 0x01  # same size, one bit flipped
        path.write_bytes(bytes(changed))
        self.assertIs(self.open(ids[6])["hash_ok"], False)
        self.assertTrue(self.open(ids[0])["hash_ok"])  # other lines intact
        path.write_bytes(original[: offsets[6] - 5])  # cut below the line
        self.assertIs(self.open(ids[6])["hash_ok"], False)
        path.write_bytes(original)
        self.assertTrue(self.open(ids[6])["hash_ok"])
        # cannot be checked: no root given, file gone, or source missing
        self.assertIsNone(self.open(ids[6], roots={})["hash_ok"])
        path.unlink()
        self.assertIsNone(self.open(ids[6])["hash_ok"])
        path.write_bytes(original)
        self.rw.execute("UPDATE source SET status = 'missing'")
        got = self.open(ids[6])
        self.assertIsNone(got["hash_ok"])
        self.assertEqual(got["provenance"]["source_status"], "missing")
        self.assertEqual(got["text"], open_specs()[6][0])  # text survives

    def test_open_does_not_block_on_a_native_pipe(self) -> None:
        source, ids, _, _ = self.write_source("thr-fifo", open_specs()[:1])
        path = self.sessions / "thr-fifo.jsonl"
        path.unlink()
        with nonregular(path) as pipe:
            if sys.platform == "win32":
                self.rw.execute(
                    "UPDATE source SET path = ? WHERE id = ?", (pipe, source)
                )
            code = (
                "import json,sys;sys.path.insert(0,sys.argv[1]);"
                "from pathlib import Path;from muninn import query,store;"
                "conn=store.connect_ro(Path(sys.argv[2]));"
                "value=query.open_event(conn,sys.argv[3],"
                "roots={'codex-sessions':Path(sys.argv[4])},raw=True);"
                "conn.close();print(json.dumps(value))"
            )
            done = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    code,
                    str(ROOT),
                    str(self.db),
                    str(ids[0]),
                    str(self.sessions),
                ],
                capture_output=True,
                timeout=5,
                check=True,
            )
        self.assertEqual(done.stderr, b"")
        got = json.loads(done.stdout)
        self.assertIsNone(got["hash_ok"])
        self.assertEqual(got["error"], "raw_unavailable")
        self.assertNotIn("raw", got)

    def test_open_never_leaves_the_root(self) -> None:
        _, ids, _, _ = self.write_source("thr-x", open_specs()[:1])
        outside = self.tmp / "outside.jsonl"
        outside.write_bytes(b"secret\n")
        self.rw.execute("UPDATE source SET path = '../outside.jsonl'")
        self.assertIsNone(self.open(ids[0])["hash_ok"])
        self.rw.execute("UPDATE source SET path = 'link.jsonl'")
        (self.sessions / "link.jsonl").symlink_to(outside)
        self.assertIsNone(self.open(ids[0])["hash_ok"])


class OpenPagingRawTests(OpenCase):
    """Chunked text, the verified raw line, and freshness fields."""

    def pages(self, event_id: int, **kw: Any) -> list[str]:
        """Collect every chunk of an event by following ``next_offset``.

        Args:
            event_id: The event to open.
            **kw: Extra ``open_event`` keywords.

        Returns:
            The text chunks in order.
        """
        chunks: list[str] = []
        offset: int | None = 0
        while offset is not None:
            got = self.open(event_id, offset=offset, **kw)
            chunks.append(got["text"])
            offset = got["next_offset"]
        return chunks

    def test_open_paging_offset(self) -> None:
        plain = "".join(chr(97 + i % 26) for i in range(30000))
        wide = "日" * 5000  # 3 bytes each: 4000 chars fill a chunk
        edge = "a" * 11999 + "🙂" * 3  # a 4-byte char never straddles
        specs = [(plain, {}), (wide, {}), (edge, {}), ("short", {})]
        _, ids, _, _ = self.write_source("thr-pg", specs)
        first = self.open(ids[0])
        self.assertEqual(len(first["text"]), 12000)
        self.assertEqual(
            (first["next_offset"], first["chars"]), (12000, 30000)
        )
        for index, (text, _) in enumerate(specs[:3]):
            chunks = self.pages(ids[index])
            self.assertEqual("".join(chunks), text)
            self.assertTrue(all(len(c.encode()) <= 12000 for c in chunks))
        self.assertEqual(
            [len(c) for c in self.pages(ids[0])], [12000] * 2 + [6000]
        )
        self.assertEqual([len(c) for c in self.pages(ids[1])], [4000, 1000])
        self.assertEqual([len(c) for c in self.pages(ids[2])], [11999, 3])
        short = self.open(ids[3], offset=5)  # at the end: empty, last page
        self.assertEqual((short["text"], short["next_offset"]), ("", None))
        for offset, code in ((6, "offset_past_end"), (-1, "bad_offset")):
            with self.subTest(offset):
                got = self.open(ids[3], offset=offset)
                self.assertEqual(got, {"error": code, "notice": NOTICE})

    def test_open_raw_line_sha256_matches_file(self) -> None:
        specs = [("alpha", {}), ("bravo é ünï", {}), ("charlie", {})]
        _, ids, lines, _ = self.write_source("thr-r", specs, eol=b"\r\n")
        self.assertNotIn("raw", self.open(ids[1]))  # only when asked
        got = self.open(ids[1], raw=True)
        self.assertEqual(got["raw"], lines[1].decode())
        digest = hashlib.sha256(got["raw"].encode()).hexdigest()
        self.assertEqual(digest, got["provenance"]["line_sha256"])
        self.assertTrue(got["hash_ok"])
        self.assertNotIn("error", got)
        self.assertNotIn("raw_redacted", got)
        blob = (self.sessions / "thr-r.jsonl").read_bytes()
        self.assertIn(got["raw"].encode() + b"\r\n", blob)  # no CR/LF in raw

    def test_open_raw_redacted_and_too_large(self) -> None:
        fits = "x" * (65536 - 17)  # the json line is exactly 64 KiB
        specs = [
            ("token [redacted:secret]", {"flags": 2}),
            ("y" * 70000, {}),
            (fits, {}),
            (fits + "x", {}),
        ]
        _, ids, lines, _ = self.write_source("thr-big", specs)
        self.assertEqual([len(lines[2]), len(lines[3])], [65536, 65537])
        red = self.open(ids[0], raw=True)
        self.assertIs(red["raw_redacted"], True)
        self.assertNotIn("raw", red)
        self.assertNotIn("error", red)
        self.assertTrue(red["redacted"] and red["hash_ok"])
        for index in (1, 3):
            with self.subTest(index):
                big = self.open(ids[index], raw=True)
                self.assertEqual(big["error"], "line_too_large")
                self.assertNotIn("raw", big)
                self.assertTrue(big["hash_ok"])  # still verified
                self.assertEqual(big["notice"], NOTICE)
                self.assertIn("text", big)  # the rest of the answer stands
        self.assertEqual(self.open(ids[2], raw=True)["raw"], lines[2].decode())

    def test_open_raw_refused_when_it_cannot_be_verified(self) -> None:
        _, ids, _, _ = self.write_source("thr-v", [("alpha", {})])
        path = self.sessions / "thr-v.jsonl"
        original = path.read_bytes()
        path.write_bytes(original.replace(b"alpha", b"omega"))
        got = self.open(ids[0], raw=True)
        self.assertEqual(got["error"], "raw_hash_mismatch")
        self.assertIs(got["hash_ok"], False)
        self.assertNotIn("raw", got)
        path.unlink()
        got = self.open(ids[0], raw=True)
        self.assertEqual(got["error"], "raw_unavailable")
        self.assertIsNone(got["hash_ok"])
        self.assertNotIn("raw", got)

    def test_open_marks_flags_and_parent(self) -> None:
        parent = self.add_event(self.add_source("dummy"), self.repo, "call")
        specs = [
            ("call", {"kind": "tool_call", "role": "assistant"}),
            ("boom", {"kind": "tool_error", "flags": 7, "parent": parent}),
        ]
        _, ids, _, _ = self.write_source("thr-f", specs)
        plain = self.open(ids[0])
        self.assertEqual(
            (plain["flagged"], plain["redacted"], plain["truncated"]),
            (False, False, False),
        )
        self.assertNotIn("parent_event_id", plain["provenance"])
        got = self.open(ids[1])
        self.assertEqual(
            (got["flagged"], got["redacted"], got["truncated"]),
            (True, True, True),
        )
        self.assertEqual(got["provenance"]["parent_event_id"], parent)

    def test_open_freshness_fields_only_with_status(self) -> None:
        _, ids, _, _ = self.write_source("thr-s", [("alpha", {})])
        self.assertNotIn("poller", self.open(ids[0]))
        got = self.open(ids[0], status={"last_pass_at": time.time() - 500})
        self.assertEqual(got["poller"], "stale")
        self.assertGreaterEqual(got["index_age_s"], 499)
