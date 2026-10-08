"""Erase follow-ups: scoped content tombstones, keyed tags, aside files.

Synthetic provider trees and a temp MUNINN_HOME only.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from muninn import (
    cli,
    knowledge,
    knowledge_push,
    tombstone_key,
    tombstones,
)
from tests.cli_support import CliCase, fake_run
from tests.erase_support import EraseCase
from tests.knowledge_support import KnowCase
from tests.store_support import assert_private
from tests.test_classify import PARENT, codex_meta, reply, user_msg
from tests.test_ingest import rollout

FORK = "thr-harden-fork"
ASIDE = "muninn.sqlite.unreadable-20260101T000000Z"


def parent_records() -> list[dict[str, object]]:
    """A parent whose first and third turns are the same short text."""
    return [
        codex_meta("user", PARENT),
        user_msg(1, "yes"),
        reply(2, "a"),
        user_msg(3, "yes"),
    ]


def fork_records() -> list[dict[str, object]]:
    """A fork that replays the parent, then says yes again of its own."""
    return [
        codex_meta("user", FORK, forked_from_id=PARENT),
        user_msg(1, "yes"),
        reply(2, "a"),
        user_msg(3, "yes"),
        user_msg(4, "fork own"),
        user_msg(5, "yes"),
    ]


class ScopedContentTests(EraseCase):
    """A content tombstone drops only a fork's replayed copy."""

    def texts(self, thread: str) -> list[str]:
        """Return the stored event texts of a thread in order."""
        return [e[3] for e in self.events(thread)]

    def test_later_identical_turn_in_the_thread_survives(self) -> None:
        self.write(rollout(PARENT), parent_records())
        self.run_ingest()
        self.erase(event_ref=f"codex:{PARENT}:2.1")
        self.run_ingest(full=True)
        self.assertEqual(self.texts(PARENT), ["a", "yes"])

    def test_fork_copy_is_dropped_but_its_own_turn_stays(self) -> None:
        self.write(rollout(PARENT), parent_records())
        self.run_ingest()
        self.erase(event_ref=f"codex:{PARENT}:2.1")
        self.write(rollout(FORK), fork_records())
        self.run_ingest()
        self.assertEqual(self.texts(FORK), ["fork own", "yes"])

    def test_erasing_through_a_fork_reaches_the_ancestor_copy(self) -> None:
        records = parent_records()[:3]
        self.write(rollout(PARENT), [*records, user_msg(3, "secret q")])
        self.write(
            rollout(FORK),
            [
                codex_meta("user", FORK, forked_from_id=PARENT),
                user_msg(1, "fork own"),
                user_msg(2, "secret q"),
            ],
        )
        self.run_ingest()
        self.assertIn("secret q", self.texts(PARENT))
        out = self.erase(event_ref=f"codex:{FORK}:3.1")
        self.assertEqual(out["lines"], 2)
        self.assertNotIn("secret q", self.texts(PARENT))
        self.assertEqual(out["residue"], 0)


class KeyedTagTests(EraseCase):
    """Content tags are keyed; unkeyed ones from older versions still work."""

    def erase_first_yes(self) -> None:
        """Ingest the parent and erase its first prompt."""
        self.write(rollout(PARENT), parent_records())
        self.run_ingest()
        self.erase(event_ref=f"codex:{PARENT}:2.1")

    def test_log_holds_no_plain_hash_and_the_key_is_private(self) -> None:
        self.erase_first_yes()
        log = (self.home / tombstone_key.TOMBSTONE_FILE).read_text()
        self.assertIn(tombstone_key.KEYED_PREFIX, log)
        plain = tombstones.role_digest("user", "yes").hex()
        self.assertNotIn(plain, log)
        key = self.home / tombstone_key.KEY_FILE
        assert_private(self, key)
        self.assertEqual(len(key.read_bytes()), 32)

    def test_the_key_is_reused_not_recreated(self) -> None:
        first = tombstone_key.load_key(self.home)
        self.assertEqual(tombstone_key.load_key(self.home), first)


class AsideFileTests(EraseCase):
    """Erase names set-aside stores it cannot scrub; doctor warns."""

    def test_erase_names_each_aside_file_and_the_removal(self) -> None:
        (self.home / ASIDE).write_bytes(b"old store")
        self.write(rollout(PARENT), parent_records())
        self.run_ingest()
        for dry in (True, False):
            out = self.erase(session=PARENT, dry_run=dry)
            self.assertEqual(out["aside_files"], [str(self.home / ASIDE)])
            self.assertEqual(out["aside_remove"], f"rm -- {self.home / ASIDE}")

    def test_no_aside_file_means_no_command(self) -> None:
        out = self.erase(session=PARENT, dry_run=True)
        self.assertEqual(out["aside_files"], [])
        self.assertIsNone(out["aside_remove"])


class AsideDoctorTests(CliCase):
    """Doctor's aside_files check."""

    def check(self) -> dict[str, Any]:
        """Run doctor with a healthy job; return the aside_files result."""
        with mock.patch.object(cli.obs, "run", fake_run()):
            _, out, _ = self.muninn("doctor")
        return {c["check"]: c for c in out["checks"]}["aside_files"]

    def test_warns_only_when_one_exists(self) -> None:
        self.assertTrue(self.check()["ok"])
        (self.home / ASIDE).write_bytes(b"old store")
        got = self.check()
        self.assertIs(got["ok"], False)
        self.assertEqual(got["level"], "warn")
        self.assertEqual(got["detail"], ASIDE)


class BoundedBlockTests(KnowCase):
    """The SessionStart query reads a bounded number of rows."""

    def test_rows_read_are_capped_whatever_the_ledger_size(self) -> None:
        user = [(self.ref(self.prompt), "use the zebra cache")]
        for i in range(6):
            self.add(text=f"use the zebra cache {i}", cites=user)
        with mock.patch.object(knowledge_push, "_SCAN_CAP", 2):
            got = knowledge.block_entries(self.ro(), [self.repo], limit=8)
        self.assertEqual(len(got), 2)
