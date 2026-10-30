"""Populate independent frozen source-one stores for installer acceptance."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from muninn import review_state, store
from muninn.review_json import canonical_json
from muninn.review_progress import digest
from tests.policy_preview_support import PreviewFixture
from tools.prepare_codex_policy import policy


class V1PreviewFixture(PreviewFixture):
    """Replace the disposable current store with independent historical DDL."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.home.resolve()
        self.ctx = dataclasses.replace(self.ctx, home=self.home)
        self.path = store.db_path(self.ctx.data)
        self.lock = self.ctx.data / "writer.lock"

    def old(
        self, shape: str, *, paused: bool = False, explicit: bool = False
    ) -> None:
        """Populate frozen DDL with actual compatible policy state."""
        self.path.unlink()
        sql = (
            Path(__file__).parent
            / "fixtures"
            / "installer"
            / f"marked-v1-{shape}.sql"
        )
        with closing(sqlite3.connect(self.path, isolation_level=None)) as conn:
            conn.executescript(sql.read_text())
            self.policy, _ = policy(
                self.ctx.data,
                Path("/bin/codex"),
                "fixture-owner",
                1.0,
                revision=2,
                repository_default=not explicit,
            )
            raw = canonical_json(self.policy)
            populate(conn, raw, digest(self.policy), self.home)
            conn.execute(
                "UPDATE review_control SET "
                "admission=?,policy_json=?,policy_digest=?",
                (0 if paused else 1, raw, digest(self.policy)),
            )
            if shape == "state":
                conn.execute(
                    "INSERT INTO learning_setup "
                    "VALUES(1,'now','/repo',1.5,NULL)"
                )
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT * FROM store_meta").fetchall(),
                [(1, "muninn-knowledge", 1)],
            )
            self.assertEqual(
                conn.execute("PRAGMA foreign_key_check").fetchall(), []
            )
        self.path.chmod(0o600)
        self.rec = self.record_for(self.policy)

    def rows(self) -> tuple[str, ...]:
        """Read all populated source rows independently of runtime gates."""
        with closing(
            sqlite3.connect(
                self.path.as_uri() + "?mode=ro&immutable=1", uri=True
            )
        ) as conn:
            return tuple(conn.iterdump())

    def selection(self) -> review_state.LearningSelection | None:
        """Read optional state without a current runtime open."""
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            return review_state.selection(conn)


def populate(
    conn: sqlite3.Connection, raw: str, digest: str, home: Path
) -> None:
    """Populate old columns with independently authored valid governed rows."""
    request, attempt, evidence, knowledge, unresolved = (
        prefix + "1" * 32 for prefix in ("R_", "A_", "E_", "K_", "U_")
    )
    scope = json.dumps({"project_id": "/repo", "task": None})
    body = json.dumps(
        {
            "form": "note",
            "kind": "fact",
            "scope": json.loads(scope),
            "content": "Old claim",
            "conditions": [],
            "evidence_refs": [evidence],
            "reason": "Old reason",
        }
    )
    item = json.dumps(
        {
            "claim": "Old claim",
            "question": "What next?",
            "affected": [],
            "evidence_refs": [evidence],
        }
    )
    locator = json.dumps(
        {
            "kind": "jsonl",
            "path": str(home / "synthetic.jsonl"),
            "byte_offset": 0,
            "byte_length": 10,
        }
    )
    selector = json.dumps(
        {"json_pointer": "/payload/content/0/text", "start": None, "end": None}
    )
    conn.execute(
        "INSERT INTO review_request VALUES(?, 'old-key','codex',"
        "'thread','thread',"
        "1,2,?,?,?,?,'explicit','review','completed',?,1.5,NULL,NULL)",
        (request, "a" * 64, scope, raw, digest, attempt),
    )
    conn.execute(
        "INSERT INTO review_attempt VALUES(?,?,1,NULL,'{}',2.0,2.0,3.0,"
        "'completed',NULL,NULL)",
        (attempt, request),
    )
    conn.execute(
        "INSERT INTO review_evidence VALUES(?,'codex','thread','thread',1,1,?,"
        "'source','permitted-fields-v2',?,?,?,'owner_statement','direct_owner',?,"
        "'Old evidence','live',1.0)",
        (evidence, "a" * 64, locator, selector, "b" * 64, scope),
    )
    conn.execute("INSERT INTO review_input VALUES(?,?)", (request, evidence))
    conn.execute(
        "INSERT INTO knowledge VALUES(?,1,'withheld',NULL,NULL)", (knowledge,)
    )
    conn.execute(
        "INSERT INTO knowledge_version VALUES(?,1,?,'note','fact',?,?,"
        "'Old reason','review',NULL,3.0,NULL)",
        (knowledge, request, scope, body),
    )
    conn.execute(
        "INSERT INTO knowledge_claim "
        "VALUES(?,1,'note','Old claim','[]','withheld')",
        (knowledge,),
    )
    conn.execute(
        "INSERT INTO knowledge_support VALUES(?,1,'note',?)",
        (knowledge, evidence),
    )
    conn.execute(
        "INSERT INTO unresolved VALUES(?,?,?,'[]',?,3.0,'open')",
        (unresolved, request, item, json.dumps([evidence])),
    )
    conn.execute(
        "INSERT INTO review_receipt "
        "VALUES(?,?,'[]',?,3.0,'completed_unresolved',NULL)",
        (request, attempt, json.dumps([unresolved])),
    )
