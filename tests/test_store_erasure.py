"""Erasure leaves no bytes behind in the store files.

Every test uses temp dirs only; nothing here touches a live data dir.
"""

from __future__ import annotations

import secrets
import unittest

from tests.store_support import (
    StoreCase,
    insert_event,
    insert_scope,
    insert_source,
)


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

    def test_erased_knowledge_and_quote_leave_zero_bytes(self) -> None:
        token, needles = self.canary()
        conn = self.rw()
        sid = insert_scope(conn)
        kid = conn.execute(
            "INSERT INTO knowledge(scope_id, kind, text, status, actor,"
            " created_at) VALUES (?, 'fact', ?, 'current', 'u', 0)",
            (sid, f"note {token}"),
        ).lastrowid
        conn.execute(
            "INSERT INTO citation(knowledge_id, provider, thread_id, line,"
            " part, line_sha256, role, kind, quote)"
            " VALUES (?, 'codex', 't', 1, 1, 'h', 'user', 'prompt', ?)",
            (kid, f"quote {token}"),
        )
        self.assertGreater(self.residue(needles), 0)
        conn.execute(
            "UPDATE citation SET quote = NULL, span_start = NULL,"
            " span_end = NULL, state = 'erased'"
        )
        conn.execute(
            "UPDATE knowledge SET text = NULL, status = 'erased'"
            " WHERE id = ?",
            (kid,),
        )
        conn.close()
        self.assertEqual(self.residue(needles), 0)


if __name__ == "__main__":
    unittest.main()
