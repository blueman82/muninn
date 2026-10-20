"""Build disposable normalized knowledge fixtures for retrieval checks."""

from __future__ import annotations

import json

from muninn import knowledge_push, knowledge_read, knowledge_write
from muninn.knowledge_models import KnowledgeContext, KnowledgeView, Scope
from muninn.review_json import canonical_json
from tests.test_review_commit import CommitCase
from tests.test_review_scope import ScopeCommitCase


class KnowledgeContextCase(CommitCase):
    """Supply real normalized claims, support and explicit context limits."""

    def add(self, *, created_at: float = 13.0, **updates: object) -> str:
        """Store a supported fixture through the checked writer producer."""
        data = self.note(**updates)
        if data["form"] == "guide":
            for key in ("content", "conditions", "evidence_refs"):
                data.pop(key)
        change = self.result([data]).changes[0]
        request = self.row(
            "SELECT * FROM review_request WHERE id=?",
            (self.context.request_id,),
        )
        return knowledge_write.apply_change(
            self.conn, change, request, created_at
        ).knowledge_id

    def guide(self) -> str:
        """Store a two-step guide with independently associated support."""
        claim = {"text": "Use Python.", "evidence_refs": [self.evidence[0].id]}
        return self.add(
            form="guide",
            kind="procedure",
            content=None,
            conditions=None,
            evidence_refs=None,
            title="Python guide",
            summary=claim,
            applicability=[claim],
            decision_points=[],
            pitfalls=[],
            steps=[
                {"key": "first", "claim": claim, "depends_on": []},
                {"key": "next", "claim": claim, "depends_on": ["first"]},
            ],
        )

    def context_view(
        self, limit: int = 10000, scope: Scope | None = None
    ) -> KnowledgeContext:
        """Read context at an explicit test time without changing state."""
        return knowledge_push.knowledge_context(
            self.conn, scope or self.scope, limit, 20.0
        )

    def set_expiry(self, ident: str, ref: int = 1, **updates: object) -> None:
        """Persist an explicit fixture expiry without creating a producer."""
        data = {
            "at": 1000.0,
            "condition": None,
            "evidence_refs": [self.evidence[ref].id],
            **updates,
        }
        self.conn.execute(
            "UPDATE knowledge_version SET expiry_json=? WHERE knowledge_id=?",
            (json.dumps(data), ident),
        )

    def global_scope(self) -> Scope:
        """Map isolated fixture originals globally under an explicit policy."""
        scope = Scope(project_id=None, task=None)
        self.conn.execute(
            "UPDATE scope SET key='global',kind='global' WHERE key=?",
            (self.scope.project_id,),
        )
        self.conn.execute(
            "UPDATE review_evidence SET scope_json=?", (canonical_json(scope),)
        )
        policy = self.policy.model_copy(update={"allow_global": True})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(policy), self.digest(policy)),
        )
        return scope

    def search(
        self, query: str = "Python", scope: Scope | None = None
    ) -> tuple[KnowledgeView, ...]:
        """Search the fixture through the public current retrieval API."""
        return knowledge_read.search_knowledge(
            self.conn, query, scope or self.scope
        )


class KnowledgeScopeCase(ScopeCommitCase):
    """Join real selected originals, requests and public knowledge reads."""

    def setUp(self) -> None:
        super().setUp()
        self.project_scope = self.scope

    def context_for(self, scope: Scope) -> KnowledgeContext:
        """Read explicit fixture scope at a stable expiry evaluation time."""
        return knowledge_push.knowledge_context(self.conn, scope, 10000, 20.0)

    def search_for(self, scope: Scope) -> tuple[KnowledgeView, ...]:
        """Search fixture guidance using its exact requested scope."""
        return knowledge_read.search_knowledge(self.conn, "Python", scope)

    def global_originals(self) -> None:
        """Reselect untouched originals after mapping their scope globally."""
        self.conn.execute("DELETE FROM review_evidence")
        self.conn.execute(
            "UPDATE scope SET key='global',kind='global' WHERE key=?",
            (self.scope.project_id,),
        )
        self.scope = Scope(project_id=None, task=None)
        self.select_originals()

    def global_policy(self, *, enabled: bool) -> None:
        """Set explicit fixture policy and current control without defaults."""
        self.policy = self.policy.model_copy(update={"allow_global": enabled})
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,policy_digest=?",
            (canonical_json(self.policy), self.digest(self.policy)),
        )

    def admit_support(self, ident: str) -> tuple[str, ...]:
        """Persist program-resolved support into this fixture's request."""
        supports = knowledge_read.knowledge_support(
            self.conn, ident, 1, self.scope, 20.0
        )
        self.conn.executemany(
            "INSERT OR IGNORE INTO review_input VALUES(?,?)",
            [(self.context.request_id, evidence) for evidence in supports],
        )
        ids = tuple(
            row[0]
            for row in self.conn.execute(
                "SELECT evidence_id FROM review_input WHERE request_id=? "
                "ORDER BY evidence_id",
                (self.context.request_id,),
            )
        )
        self.context = self.context.model_copy(update={"evidence_ids": ids})
        return supports
