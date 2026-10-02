"""A Codex ChatGPT-handoff session is the owner's, minus what was pasted."""

from __future__ import annotations

import unittest

from muninn import classify as c
from muninn.codex_events import (
    CHATGPT_HANDOFF,
    CHATGPT_REFERENCE_TAG,
    CHATGPT_REFERENCE_WINDOW,
)
from tests.classify_support import (
    GUARDIAN_SOURCE,
    codex_meta,
    run_codex,
    user_msg,
)


class ChatgptHandoffTests(unittest.TestCase):
    """How handoff threads are classed and what their messages become."""

    def test_a_chatgpt_handoff_session_stores_what_the_owner_typed(
        self,
    ) -> None:
        meta = codex_meta(CHATGPT_HANDOFF, "thr-h")
        pasted = f"{CHATGPT_REFERENCE_TAG}:\n" + '{"title":"CV Fit"}'
        records = [meta, user_msg(1, pasted), user_msg(2, "apply to these")]
        events, state = run_codex(records)
        self.assertEqual(state.thread_class, "primary")
        # The pasted conversation is not the owner speaking; their typing is.
        self.assertEqual([e.kind for e in events], ["harness", "prompt"])
        self.assertEqual(events[1].text, "apply to these")

    def test_the_opening_message_is_harness_in_every_shape_it_arrives(
        self,
    ) -> None:
        pasted = f"{CHATGPT_REFERENCE_TAG}:\n" + '{"title":"CV Fit"}'
        request = "## My request:\nContinuing from the ChatGPT conversation"
        shapes = {
            "leading newline": f"\n{pasted}",
            "leading spaces": f"  \n{pasted}",
            "after a wrapper": f"<note>ctx</note>\n{pasted}",
            "with the composed request": f"\n{pasted}\n\n{request}",
        }
        for name, text in shapes.items():
            with self.subTest(shape=name):
                meta = codex_meta(CHATGPT_HANDOFF, "thr-h")
                events, _ = run_codex([meta, user_msg(1, text)])
                self.assertEqual([e.kind for e in events], ["harness"])

    def test_the_heading_deep_in_a_message_does_not_hide_it(self) -> None:
        text = "x" * (CHATGPT_REFERENCE_WINDOW + 1) + CHATGPT_REFERENCE_TAG
        events, _ = run_codex([codex_meta(), user_msg(1, text)])
        self.assertEqual([e.kind for e in events], ["prompt"])

    def test_a_guardian_stays_a_reviewer_even_from_a_handoff(self) -> None:
        meta = codex_meta(CHATGPT_HANDOFF, "thr-g", source=GUARDIAN_SOURCE)
        self.assertEqual(c.codex_thread(meta).thread_class, "reviewer")

    def test_the_names_match_what_codex_writes(self) -> None:
        """The fixtures use the constants, so pin the real strings here."""
        self.assertEqual(CHATGPT_HANDOFF, "chatgpt_handoff")
        self.assertEqual(
            CHATGPT_REFERENCE_TAG, "## Referenced ChatGPT conversation"
        )


if __name__ == "__main__":
    unittest.main()
