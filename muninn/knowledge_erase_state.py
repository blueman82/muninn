"""Hold mutable identities for the governed erasure fixed point."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SupportClosure:
    """Hold structural identities for the complete governed erasure plan."""

    evidence: set[str]
    versions: set[tuple[str, int]]
    requests: set[str]
    unresolved: set[str]
    candidates: set[str] = field(default_factory=set[str])
    decisions: set[str] = field(default_factory=set[str])

    def sizes(self) -> tuple[int, ...]:
        """Compare all graph frontiers without copying their identities."""
        return tuple(
            len(value)
            for value in (
                self.evidence,
                self.versions,
                self.requests,
                self.unresolved,
                self.candidates,
                self.decisions,
            )
        )
