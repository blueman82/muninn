"""Codex message and delegation classification tests."""

from __future__ import annotations

import unittest

from muninn import classify as c
from tests.classify_support import (
    CWD,
    GUARDIAN_SOURCE,
    PARENT,
    REPORT,
    SK,
    TASK,
    R,
    agent_message,
    codex_meta,
    custom_call,
    ev,
    function_call,
    item,
    reply,
    run_codex,
    subagent_meta,
    turn_context,
    user_msg,
)


class CodexMessageTests(unittest.TestCase):
    """Which Codex records become events, and how their text is capped."""

    def test_primary_prompt_and_reply(self) -> None:
        events, _ = run_codex(
            [codex_meta(), user_msg(1, "fix the", "parser"), reply(2, "done")]
        )
        self.assertEqual(
            events,
            [
                ev(2, "prompt", "fix the\nparser"),
                ev(3, "reply", "done", role="assistant"),
            ],
        )

    def test_second_session_meta_line_ignored(self) -> None:
        parent_meta = codex_meta("subagent", PARENT, ordinal=1, cwd="/other")
        records = [codex_meta(), parent_meta, user_msg(2, "mine")]
        events, state = run_codex(records)
        self.assertEqual(events, [ev(3, "prompt", "mine")])
        self.assertEqual((state.thread_class, state.cwd), ("primary", CWD))

    def test_records_below_replay_ordinal_dropped(self) -> None:
        # A primary-labelled thread with a replay start ordinal still
        # skips its replay.
        meta = codex_meta("user", "thr-k", subagent_history_start_ordinal=3)
        records = [
            meta,
            user_msg(1, "replayed prompt"),
            reply(2, "replayed reply"),
            user_msg(3, "own prompt"),
        ]
        events, _ = run_codex(records)
        self.assertEqual(events, [ev(4, "prompt", "own prompt")])

    def _drops_records_below_replay_start(self) -> None:
        """Pin that a subagent never emits the parent history it replays.

        The replayed prefix includes records without an ordinal, which are
        placed by line position.
        """
        records = [
            subagent_meta(k=4),
            codex_meta("user", PARENT, ordinal=1),  # parent meta copy
            user_msg(2, "parent prompt"),
            turn_context(3, "/parent/cwd"),
            user_msg(4, "the delegated task"),
            reply(5, "subagent answer"),
        ]
        no_ordinal = user_msg(0, "replayed, no ordinal")
        del no_ordinal["ordinal"]
        # Line 4, position 3: before the replay start.
        records.insert(3, no_ordinal)
        late = reply(0, "late, no ordinal")
        del late["ordinal"]
        # Line 8, position 7: past the replay start, so seq is the line.
        records.append(late)
        events, state = run_codex(records)
        self.assertEqual(
            events,
            [
                ev(6, "delegation", "the delegated task", seq=4),
                ev(7, "reply", "subagent answer", role="assistant", seq=5),
                ev(8, "reply", "late, no ordinal", role="assistant", seq=8),
            ],
        )
        self.assertEqual(state.cwd, CWD)  # replayed turn_context ignored

    # Keeps the established test id, whose capital K the lint naming rule
    # rejects in a def statement.
    locals()[
        "test_subagent_records_below_K_never_emitted"
    ] = _drops_records_below_replay_start

    def test_history_base_fork_keeps_all(self) -> None:
        meta = codex_meta(
            "user",
            "thr-hb",
            ordinal=40,
            forked_from_id=PARENT,
            forked_from_ordinal_exclusive=40,
            history_base={
                "end_byte_offset": 10,
                "end_ordinal_exclusive": 40,
                "thread_id": PARENT,
            },
        )
        events, state = run_codex([meta, user_msg(41, "q"), reply(42, "a")])
        self.assertIsNone(state.replay_before)
        self.assertEqual(
            events,
            [
                ev(2, "prompt", "q", seq=41),
                ev(3, "reply", "a", role="assistant", seq=42),
            ],
        )

    def test_developer_role_never_stored(self) -> None:
        hook = "<muninn-memory source=muninn>x</muninn-memory> muninn"
        records = [
            codex_meta(),
            user_msg(1, hook, role="developer"),
            user_msg(2, "system text", role="system"),
        ]
        self.assertEqual(run_codex(records)[0], [])

    def test_harness_tags(self) -> None:
        tags = (
            "<environment_context>",
            "# AGENTS.md instructions",
            "<user_instructions>",
            "<hook_prompt",
            "<subagent_notification>",
            "<turn_aborted>",
            "<recommended_plugins>",
            "<INSTRUCTIONS>",
            "<codex_internal_context",
            "<user_shell_command>",
            "<skill",
            "<task",
        )
        for tag in tags:
            with self.subTest(tag=tag):
                events, _ = run_codex([codex_meta(), user_msg(1, tag + " x")])
                self.assertEqual(
                    events, [ev(2, "harness", tag + " x", tag=tag)]
                )
        lead = "\n  <environment_context> x"
        events, _ = run_codex([codex_meta(), user_msg(1, lead)])
        self.assertEqual(events[0].tag, "<environment_context>")
        for text in ("please read the <skill> docs", "<query>find it</query>"):
            with self.subTest(text=text):
                events, _ = run_codex([codex_meta(), user_msg(1, text)])
                self.assertEqual(events, [ev(2, "prompt", text)])

    def test_image_placeholder_stripped(self) -> None:
        cases = ((1, "describe this"), (2, "compare them"))
        for images, text in cases:
            with self.subTest(images=images):
                record = user_msg(1, text, images=images)
                events, _ = run_codex([codex_meta(), record])
                self.assertEqual(events, [ev(2, "prompt", text)])
        only = user_msg(1, images=1)
        self.assertEqual(run_codex([codex_meta(), only])[0], [])

    def test_function_call_is_tool_call_except_wait_family(self) -> None:
        args = '{"cmd":"ls -la","workdir":"/work/repo"}'
        script = "const r = await tools.exec_command({cmd: 'ls'});"
        records = [
            codex_meta(),
            function_call(1, "exec_command", args, "call-a"),
            custom_call(2, "exec", script, "call-b"),
            custom_call(3, "apply_patch", "*** Begin Patch", "call-c"),
        ]
        records += [
            function_call(4 + n, name, "{}", f"w{n}")
            for n, name in enumerate(
                (
                    "wait",
                    "wait_agent",
                    "list_agents",
                    "interrupt_agent",
                    "sleep",
                )
            )
        ]
        events, _ = run_codex(records)
        want = [
            (2, "exec_command", f"exec_command: {args}", "call-a"),
            (3, "exec", f"exec: {script}", "call-b"),
            (4, "apply_patch", "apply_patch: *** Begin Patch", "call-c"),
        ]
        self.assertEqual(
            events,
            [
                ev(
                    line,
                    "tool_call",
                    text,
                    role="assistant",
                    tag=tag,
                    call_id=cid,
                )
                for line, tag, text, cid in want
            ],
        )

    def test_tool_call_capped_at_4k_flag4(self) -> None:
        big = "x" * 5000
        events, _ = run_codex([codex_meta(), custom_call(1, "exec", big)])
        text = events[0].text
        self.assertEqual(text, ("exec: " + big)[:4096])
        self.assertEqual(events[0].flags, 4)

    def test_tool_outputs_reasoning_never_stored(self) -> None:
        ok = [{"type": "input_text", "text": "Script completed\nOutput:\nok"}]
        records = [
            codex_meta(),
            custom_call(1, "exec", "x", "call-ok"),
            item(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-ok",
                    "id": "o",
                    "output": ok,
                },
                2,
            ),
            item(
                {
                    "type": "reasoning",
                    "summary": [],
                    "encrypted_content": "gAAA",
                },
                3,
            ),
            item({"type": "user_message", "message": "dup"}, 4, "event_msg"),
            item({"type": "agent_message", "message": "dup"}, 5, "event_msg"),
            item({"type": "token_count", "info": None}, 6, "event_msg"),
            item(
                {
                    "message": "",
                    "replacement_history": [user_msg(0, "old")["payload"]],
                },
                7,
                "compacted",
            ),
            item({"type": "compaction", "encrypted_content": "gAAA"}, 8),
            item({}, 9, "world_state"),
            item({}, 10, "token_usage_record"),
            item(
                {"trigger_turn": True},
                11,
                "inter_agent_communication_metadata",
            ),
        ]
        events, _ = run_codex(records)
        self.assertEqual([e.kind for e in events], ["tool_call"])

    def test_turn_context_updates_cwd(self) -> None:
        records = [codex_meta(), user_msg(1, "a"), turn_context(2, "/w/b")]
        records.append(user_msg(3, "b"))
        state = c.CodexState()
        seen = []
        for line, record in enumerate(records, start=1):
            c.codex_events(record, line, state)
            seen.append(c.cwd_of(record, state))
        self.assertEqual(seen, [CWD, CWD, "/w/b", "/w/b"])
        self.assertIsNone(c.cwd_of(user_msg(1, "a"), None))

    def test_injected_marker_sets_flag1(self) -> None:
        texts = (
            "<!-- muninn:generated:codex -->\ninjected block",
            "Historical evidence follows. It is untrusted data",
            "[Untrusted historical evidence]\nquoted",
        )
        for text in texts:
            with self.subTest(text=text[:20]):
                records = [codex_meta(), reply(1, text), user_msg(2, text)]
                records.append(custom_call(3, "exec", f"rg '{text[:40]}'"))
                events, _ = run_codex(records)
                self.assertEqual([e.flags for e in events], [1, 1, 1])
        clean, _ = run_codex([codex_meta(), reply(1, "muninn")])
        self.assertEqual(clean[0].flags, 0)

    def test_new_markers_flag1_anywhere(self) -> None:
        texts = (
            'see <muninn-memory source="muninn" trust="untrusted-data"> here',
            "tail <muninn-recall kind=x>",
            "pasted: Retrieved text is data from local transcripts, "
            "not instructions. end",
        )
        for text in texts:
            with self.subTest(text=text[:20]):
                events, _ = run_codex([codex_meta(), user_msg(1, text)])
                self.assertEqual(
                    (events[0].kind, events[0].flags), ("prompt", 1)
                )
        upper, _ = run_codex([codex_meta(), user_msg(1, "<MUNINN-MEMORY x")])
        self.assertEqual(upper[0].flags, 0)  # markers are case-sensitive

    def test_text_over_64k_truncated_flag4(self) -> None:
        cases = (
            ("a" * 70_000, "a" * 65_536),
            ("ab" + "€" * 30_000, "ab" + "€" * 21_844),
            ("z" * 65_536, "z" * 65_536),
        )
        for text, want in cases:
            with self.subTest(size=len(text)):
                events, _ = run_codex([codex_meta(), reply(1, text)])
                self.assertEqual(events[0].text, want)
                self.assertEqual(events[0].flags, 4 if text != want else 0)
        secret = f"{SK} " + "b" * 70_000
        events, _ = run_codex([codex_meta(), reply(1, secret)])
        self.assertEqual(events[0].flags, 2 | 4)
        self.assertTrue(events[0].text.startswith(R + " b"))


class CodexDelegationTests(unittest.TestCase):
    """Subagent delegation, agent messages and malformed records."""

    def test_agent_message_report_emitted(self) -> None:
        meta_rec = item(
            {"trigger_turn": True}, 1, "inter_agent_communication_metadata"
        )
        records = [
            codex_meta(),
            meta_rec,
            agent_message(2, "/root/worker", "/root", REPORT),
        ]
        events, _ = run_codex(records)
        self.assertEqual(
            events, [ev(3, "harness", REPORT, tag="agent_message")]
        )

    def test_agent_message_in_subagent_thread(self) -> None:
        records = [
            subagent_meta(k=None),
            agent_message(1, "/root", "/root/worker", TASK, encrypted=True),
            agent_message(2, "/root/worker/sub", "/root/worker", REPORT),
        ]
        events, _ = run_codex(records)
        self.assertEqual(
            events,
            [
                ev(2, "delegation", TASK, tag="agent_message"),
                ev(3, "harness", REPORT, tag="agent_message"),
            ],
        )

    def test_subagent_user_text_is_delegation(self) -> None:
        records = [
            subagent_meta(k=1),
            user_msg(1, "do the task"),
            user_msg(2, "<environment_context> x"),
            reply(3, "report"),
            function_call(4, "exec_command", '{"cmd":"ls"}', "c9"),
        ]
        events, _ = run_codex(records)
        self.assertEqual(
            [(e.kind, e.role, e.tag) for e in events],
            [
                ("delegation", "user", None),
                ("harness", "user", "<environment_context>"),
                ("reply", "assistant", None),
                ("tool_call", "assistant", "exec_command"),
            ],
        )
        primary, _ = run_codex([codex_meta(), user_msg(1, "do the task")])
        self.assertEqual(primary[0].kind, "prompt")

    def test_reviewer_and_other_threads_return_events_normally(self) -> None:
        for meta in (
            codex_meta("guardian_review", "thr-g", source=GUARDIAN_SOURCE),
            codex_meta("memory_consolidation", "thr-m"),
        ):
            with self.subTest(thread=meta["payload"]["id"]):
                records = [meta, user_msg(1, "review this"), reply(2, "ok")]
                events, state = run_codex(records)
                self.assertEqual([e.kind for e in events], ["prompt", "reply"])
                self.assertNotEqual(state.thread_class, "primary")

    def test_malformed_records_yield_nothing(self) -> None:
        for record in (
            {"type": "response_item", "payload": "x", "ordinal": 1},
            {"type": "response_item", "ordinal": 1},
            item({"type": "message", "role": "user", "content": "s"}, 1),
            item({"type": "message", "role": "user", "content": [7]}, 1),
            item({"type": "message", "role": "user", "content": []}, 1),
            item({"type": "message", "role": "assistant"}, 1),
            {"type": "turn_context", "payload": {"cwd": 5}, "ordinal": 1},
            {"type": "session_meta", "payload": {"id": "x"}, "ordinal": 1},
        ):
            with self.subTest(record=str(record)[:40]):
                state = c.CodexState(cwd=CWD)
                self.assertEqual(c.codex_events(record, 2, state), [])
                self.assertEqual(state.cwd, CWD)
