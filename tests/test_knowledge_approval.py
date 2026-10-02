"""Knowledge add: a short quote is an approval of a preceding proposal."""

from __future__ import annotations

from typing import Any

from tests.knowledge_support import KnowCase


class ApprovalTests(KnowCase):
    """A quote under 12 characters is an approval of a proposal."""

    def setUp(self) -> None:
        super().setUp()
        src = self.add_source("thr-talk", session="sess-talk")
        self.n = 0

        def say(text: str, **kw: Any) -> int:
            """Add a user event with the next timestamp of the thread."""
            self.n += 1
            kw.setdefault("ts", f"2026-09-02T09:00:{self.n:02d}.000Z")
            return self.add_event(src, self.repo, text, **kw)

        def bot(text: str, **kw: Any) -> int:
            """Add an assistant reply with the next timestamp."""
            return say(text, kind="reply", role="assistant", **kw)

        say("Please design the lookup path.")
        self.r0 = bot("Sure, here is a first outline of the lookup path.")
        self.r1 = bot(
            "I propose to cache lookups in the zebra cache. Shall I?"
        )
        self.c1 = say("Bash: ls src", kind="tool_call", role="assistant")
        self.hold = say("hold on, let me look at it")
        self.yes = say("yes, do it")
        self.r2 = bot("Done, the zebra cache is wired in.")
        self.short_reply = bot("Done.")
        self.r3 = bot("Shall I also cache the yak lookups too?")
        self.yes2 = say("yes, do it")
        solo = self.add_source("thr-solo", session="sess-solo")
        self.first = self.add_event(solo, self.repo, "go ahead")
        other = self.add_source("thr-other", session="sess-other")
        for filler in ("an unrelated question", "more filler", "and more"):
            self.add_event(other, self.repo, filler)
        self.r_other = self.add_event(
            other,
            self.repo,
            "I propose to cache lookups elsewhere",
            kind="reply",
            role="assistant",
        )
        self.quote = {
            self.r0: "first outline of the lookup",
            self.r1: "propose to cache lookups",
            self.r2: "zebra cache is wired in",
            self.r3: "also cache the yak lookups",
            self.r_other: "propose to cache lookups",
        }

    def cite(self, event: int, quote: str | None = None) -> tuple[str, str]:
        """Build a citation pair for an event.

        Args:
            event: Primary key of the cited event.
            quote: Quote to use; defaults to the stored quote of the event.

        Returns:
            A ``(ref, quote)`` pair.
        """
        return (self.ref(event), quote or self.quote[event])

    def test_short_approval_needs_preceding_reply_citation(self) -> None:
        yes = self.cite(self.yes, "yes, do it")
        self.refused("approval_needs_reply", cites=[yes])
        # an earlier reply, a later reply, another thread's reply: not it
        for wrong in (self.r0, self.r2, self.r_other):
            with self.subTest(wrong):
                self.refused(
                    "approval_needs_reply", cites=[yes, self.cite(wrong)]
                )
        got = self.add(cites=[yes, self.cite(self.r1)])
        self.assertEqual(
            [(c["ref"], c["quote"]) for c in got["entry"]["cites"]],
            [
                (self.ref(self.yes), "yes, do it"),
                (self.ref(self.r1), "propose to cache lookups"),
            ],
        )
        self.add(kind="preference", cites=[yes, self.cite(self.r1)])
        spaced = (self.ref(self.yes), "yes,do it")
        self.refused("quote_not_found", cites=[spaced, self.cite(self.r1)])
        spaced = (self.ref(self.yes), "  yes,   do  it ")
        self.add(cites=[spaced, self.cite(self.r1)])
        # the second "yes, do it" answers the later proposal
        yes2 = self.cite(self.yes2, "yes, do it")
        self.refused("approval_needs_reply", cites=[yes2, self.cite(self.r1)])
        self.add(cites=[yes2, self.cite(self.r3)])
        # two approvals in one entry each need their own proposal
        self.refused(
            "approval_needs_reply", cites=[yes, yes2, self.cite(self.r1)]
        )
        self.add(cites=[yes, yes2, self.cite(self.r1), self.cite(self.r3)])

    def test_only_a_whole_user_prompt_can_be_short(self) -> None:
        r1 = self.cite(self.r1)
        self.refused("quote_length", cites=[(self.ref(self.yes), "yes"), r1])
        self.refused("quote_length", cites=[(self.ref(self.yes), "do it"), r1])
        self.refused(
            "quote_length", cites=[(self.ref(self.short_reply), "Done."), r1]
        )
        self.refused(
            "quote_length", cites=[(self.ref(self.c1), "Bash: ls"), r1]
        )
        self.refused(
            "quote_not_found", cites=[(self.ref(self.yes), "yes do it"), r1]
        )
        self.refused(
            "approval_needs_reply", cites=[(self.ref(self.first), "go ahead")]
        )  # nothing came before it
        self.refused(
            "approval_needs_reply",
            cites=[(self.ref(self.first), "go ahead"), r1],
        )

    def test_quote_only_approval_also_needs_the_reply(self) -> None:
        env = {"CODEX_SESSION_ID": "sess-talk"}
        self.refused(
            "approval_needs_reply", quote_only="yes, do it", cites=[], env=env
        )
        # the latest "yes, do it" is the second; it answers r3, not r1
        self.refused(
            "approval_needs_reply",
            quote_only="yes, do it",
            cites=[self.cite(self.r1)],
            env=env,
        )
        got = self.add(
            quote_only="yes, do it", cites=[self.cite(self.r3)], env=env
        )
        refs = [c["ref"] for c in got["entry"]["cites"]]
        self.assertEqual(refs, [self.ref(self.r3), self.ref(self.yes2)])
