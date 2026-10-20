"""Erasure leaves no bytes behind in the store files.

Every test uses temp dirs only; nothing here touches a live data dir.
"""

from __future__ import annotations

import secrets
import unittest

from tests.erase_support import EraseCase
from tests.store_support import (
    StoreCase,
    insert_event,
    insert_scope,
    insert_source,
)
from tests.test_classify import codex_meta, user_msg
from tests.test_ingest import TID, rollout


class ErasureTests(StoreCase):
    """Secure-delete behaviour proven with synthetic canary tokens."""

    def canary(self) -> tuple[str, list[bytes]]:
        """Make a random token and the byte needles that detect it.

        Returns:
            The token and the byte strings to search the store files for.
        """
        token = secrets.token_hex(16)  # 32 ASCII bytes
        # the whole token plus both ends: FTS5 prefix-compresses terms
        return token, [t.encode() for t in (token, token[:12], token[-12:])]

    def test_erased_canary_leaves_zero_bytes(self) -> None:
        token, needles = self.canary()
        conn = self.rw()
        sid, src = insert_scope(conn), insert_source(conn)
        eid = insert_event(conn, src, sid, text=f"before {token} after")
        self.assertGreater(self.residue(needles), 0)  # the search can hit
        conn.execute("DELETE FROM event WHERE id = ?", (eid,))
        conn.close()
        self.assertEqual(self.residue(needles), 0)

    def test_canary_control_leaves_residue_without_secure_settings(
        self,
    ) -> None:
        token, needles = self.canary()
        conn = self.rw()
        conn.execute("PRAGMA secure_delete=OFF")
        conn.execute(
            "INSERT INTO event_fts(event_fts, rank)"
            " VALUES ('secure-delete', 0)"
        )
        sid, src = insert_scope(conn), insert_source(conn)
        eid = insert_event(conn, src, sid, text=f"before {token} after")
        conn.execute("DELETE FROM event WHERE id = ?", (eid,))
        conn.close()
        self.assertGreater(self.residue(needles), 0)  # the test can fail


class KnowledgeErasureTests(EraseCase):
    """Require physical erasure of normalized notes and selected originals."""

    def test_erased_knowledge_and_quote_leave_zero_bytes(self) -> None:
        token = secrets.token_hex(16)
        needles = [
            value.encode() for value in (token, token[:12], token[-12:])
        ]
        original = self.write(
            rollout(), [codex_meta("user", TID), user_msg(1, f"quote {token}")]
        )
        original_bytes = original.read_bytes()
        self.run_ingest()
        kid = self.note(f"note {token}", TID)

        def residue() -> int:
            return sum(
                path.read_bytes().count(needle)
                for path in self.home.rglob("*")
                if path.is_file()
                for needle in needles
            )

        self.assertGreater(residue(), 0)
        self.assertIn(
            token,
            self.conn.execute(
                "SELECT content_json FROM knowledge_version "
                "WHERE knowledge_id=?",
                (kid,),
            ).fetchone()[0],
        )
        self.assertIn(
            token,
            self.conn.execute(
                "SELECT excerpt FROM review_evidence"
            ).fetchone()[0],
        )
        self.assertEqual(self.count("SELECT count(*) FROM knowledge_fts"), 1)
        out = self.erase(event_ref=f"codex:{TID}:2.1")
        self.assertEqual(out["residue"], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM knowledge WHERE id=?", (kid,)
            ).fetchone()[0],
            "erased",
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT content_json,reason,expiry_json "
                    "FROM knowledge_version WHERE knowledge_id=?",
                    (kid,),
                ).fetchone()
            ),
            (None, None, None),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT text,state FROM knowledge_claim "
                    "WHERE knowledge_id=?",
                    (kid,),
                ).fetchone()
            ),
            (None, "erased"),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT excerpt,state FROM review_evidence"
                ).fetchone()
            ),
            (None, "erased"),
        )
        self.assertEqual(self.count("SELECT count(*) FROM knowledge_fts"), 0)
        self.conn.close()
        self.assertEqual(residue(), 0)
        self.assertEqual(original.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
