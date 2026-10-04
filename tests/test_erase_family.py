"""Erase reaches a fork family's copies, never a fork's own later turn.

Synthetic provider trees and a temp MUNINN_HOME only.  A copy stored in a
fork exists only when the fork was read before its parent grew, so each
case reads the forks first and lets the parent catch up afterwards.
"""

from __future__ import annotations

from unittest import mock

from muninn import classify
from tests.erase_support import EraseCase
from tests.test_classify import PARENT, codex_meta, reply, user_msg
from tests.test_erase_forks import fork_records as plain_fork_records
from tests.test_erase_forks import parent_records as plain_parent_records
from tests.test_ingest import rollout

A, B = "thr-fam-a", "thr-fam-b"
GP, CHILD, GRAND = "thr-fam-gp", "thr-fam-child", "thr-fam-grand"
YES = "yes"


def head(thread: str, parent: str | None = None) -> dict[str, object]:
    """Return a session meta record, of a fork when ``parent`` is given."""
    if parent is None:
        return codex_meta("user", thread)
    return codex_meta("user", thread, forked_from_id=parent)


class FamilyCopyTests(EraseCase):
    """Which copies of an erased turn are removed with it."""

    def texts(self, thread: str) -> list[str]:
        """Return the stored event texts of a thread in order."""
        return [e[3] for e in self.events(thread)]

    def grow_parent(self) -> None:
        """Let the parent catch up with the turn its forks already hold."""
        path = self.roots["codex-sessions"] / rollout(PARENT)
        self.append(path, [user_msg(3, YES)])
        self.run_ingest()

    def test_a_forks_own_later_identical_turn_stays(self) -> None:
        self.write(
            rollout(PARENT),
            [head(PARENT), user_msg(1, "p1"), reply(2, "p2")],
        )
        self.run_ingest()
        self.write(
            rollout(A),
            [
                head(A, PARENT),
                user_msg(1, "p1"),
                reply(2, "p2"),
                user_msg(3, YES),
                user_msg(4, "own"),
                user_msg(5, YES),
            ],
        )
        self.run_ingest()
        self.assertEqual(self.texts(A), [YES, "own", YES])
        self.grow_parent()
        out = self.erase(event_ref=f"codex:{PARENT}:4.1")
        self.assertEqual(out["lines"], 2)  # the parent's turn and the copy
        self.assertEqual(self.texts(A), ["own", YES])
        self.assertEqual(self.texts(PARENT), ["p1", "p2"])

    def test_a_fork_whose_first_own_event_is_not_history_is_untouched(
        self,
    ) -> None:
        self.write(
            rollout(PARENT),
            [head(PARENT), user_msg(1, "p1"), reply(2, "p2")],
        )
        self.run_ingest()
        self.write(
            rollout(A),
            [
                head(A, PARENT),
                user_msg(1, "p1"),
                reply(2, "p2"),
                user_msg(3, "own"),
                user_msg(4, YES),
            ],
        )
        self.run_ingest()
        self.grow_parent()
        out = self.erase(event_ref=f"codex:{PARENT}:4.1")
        self.assertEqual(out["lines"], 1)
        self.assertEqual(self.texts(A), ["own", YES])

    def test_siblings_lose_the_copy_but_keep_their_own_turn(self) -> None:
        self.write(
            rollout(PARENT),
            [head(PARENT), user_msg(1, "p1"), reply(2, "p2")],
        )
        self.run_ingest()
        for thread in (A, B):
            self.write(
                rollout(thread),
                [
                    head(thread, PARENT),
                    user_msg(1, "p1"),
                    reply(2, "p2"),
                    user_msg(3, YES),
                    user_msg(4, f"{thread}-own"),
                    user_msg(5, YES),
                ],
            )
        self.run_ingest()
        self.grow_parent()
        out = self.erase(event_ref=f"codex:{A}:4.1")
        self.assertEqual(out["residue"], 0)
        self.assertEqual(self.texts(A), [f"{A}-own", YES])
        self.assertEqual(self.texts(B), [f"{B}-own", YES])
        self.assertEqual(self.texts(PARENT), ["p1", "p2"])

    def test_a_grandchild_copy_goes_with_the_grandparents_turn(self) -> None:
        self.write(rollout(GP), [head(GP), user_msg(1, "g1")])
        self.run_ingest()
        self.write(
            rollout(CHILD),
            [head(CHILD, GP), user_msg(1, "g1"), user_msg(2, "c1")],
        )
        self.run_ingest()
        # Read while its parent holds no "g1", so the copy is stored.
        self.write(
            rollout(GRAND),
            [
                head(GRAND, CHILD),
                user_msg(1, "g1"),
                user_msg(2, "c1"),
                user_msg(3, "own"),
                user_msg(4, "g1"),
            ],
        )
        self.run_ingest()
        self.assertEqual(self.texts(GRAND), ["g1", "c1", "own", "g1"])
        self.erase(event_ref=f"codex:{GP}:2.1")
        self.assertEqual(self.texts(GP), [])
        self.assertEqual(self.texts(CHILD), ["c1"])
        self.assertEqual(self.texts(GRAND), ["c1", "own", "g1"])

    def test_a_fork_whose_parent_is_not_stored_is_not_in_the_family(
        self,
    ) -> None:
        self.write(rollout(PARENT), [head(PARENT), user_msg(1, YES)])
        self.write(
            rollout(A),
            [head(A, "thr-fam-absent"), user_msg(1, YES), user_msg(2, "x")],
        )
        self.run_ingest()
        self.erase(event_ref=f"codex:{PARENT}:2.1")
        self.assertEqual(self.texts(A), [YES, "x"])

    def test_a_forked_from_loop_ends(self) -> None:
        self.write(rollout(A), [head(A, B), user_msg(1, YES)])
        self.write(rollout(B), [head(B, A), user_msg(1, "other")])
        self.run_ingest()
        out = self.erase(event_ref=f"codex:{A}:2.1")
        self.assertEqual(out["lines"], 1)  # and the walk came back
        self.assertEqual((self.texts(A), self.texts(B)), ([], ["other"]))


class PreContentTagTests(EraseCase):
    """An erase made before content tags existed still blocks a fork."""

    def erase_without_tags(self) -> None:
        """Erase the parent's third prompt, then forget its content tag."""
        self.write(rollout(PARENT), plain_parent_records())
        self.run_ingest()
        self.erase(event_ref=f"codex:{PARENT}:4.1")
        self.conn.execute("DELETE FROM tombstone WHERE line = 0")

    def fork_texts(self) -> list[str]:
        """Return the fork's stored texts."""
        return [e[3] for e in self.events("thr-erase-fork")]

    def test_a_new_fork_does_not_store_the_erased_copy(self) -> None:
        self.erase_without_tags()
        self.write(rollout("thr-erase-fork"), plain_fork_records())
        self.run_ingest()
        self.assertEqual(self.fork_texts(), ["fork own"])

    def test_a_classifier_bump_re_reads_without_resurrecting_it(self) -> None:
        self.erase_without_tags()
        self.write(rollout("thr-erase-fork"), plain_fork_records())
        self.run_ingest()
        with mock.patch.object(
            classify, "CLASSIFIER_VERSION", classify.CLASSIFIER_VERSION + 1
        ):
            self.run_ingest()
            row = self.source("thr-erase-fork")
            assert row is not None
            self.assertEqual(
                row["classifier_version"], classify.CLASSIFIER_VERSION
            )
        self.assertEqual(self.fork_texts(), ["fork own"])
