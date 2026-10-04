"""An erased line cannot come back through a fork's copy of the history.

Synthetic provider trees and a temp MUNINN_HOME only (EraseCase).
"""

from __future__ import annotations

from muninn import erase
from tests.erase_support import EraseCase
from tests.test_classify import PARENT, codex_meta, reply, user_msg
from tests.test_ingest import rollout

FORK = "thr-erase-fork"


def parent_records() -> list[dict[str, object]]:
    """Records of a parent whose second prompt will be erased."""
    return [
        codex_meta("user", PARENT),
        user_msg(1, "parent q"),
        reply(2, "parent a"),
        user_msg(3, "secret q"),
    ]


def fork_records() -> list[dict[str, object]]:
    """Records of a fork that copied the parent's history, then went on."""
    return [
        codex_meta("user", FORK, forked_from_id=PARENT),
        user_msg(1, "parent q"),
        reply(2, "parent a"),
        user_msg(3, "secret q"),
        user_msg(4, "fork own"),
    ]


class ForkCopyTests(EraseCase):
    """Content tombstones cover forks on ingest, erase and rebuild."""

    def texts(self, thread: str) -> list[str]:
        """Return the stored event texts of a thread in order."""
        return [e[3] for e in self.events(thread)]

    def test_erased_parent_line_stays_out_of_a_later_fork(self) -> None:
        self.write(rollout(PARENT), parent_records())
        self.run_ingest()
        self.erase(event_ref=f"codex:{PARENT}:4.1")
        self.write(rollout(FORK), fork_records())
        self.run_ingest()
        self.assertEqual(self.texts(FORK), ["fork own"])
        self.run_ingest(full=True)  # a rebuild-style full read agrees
        self.assertEqual(self.texts(FORK), ["fork own"])

    def test_erase_removes_a_copy_already_stored_in_a_fork(self) -> None:
        self.write(rollout(PARENT), parent_records()[:3])
        self.run_ingest()
        # The fork was read while its parent was still short, so the third
        # prompt is stored as the fork's own event.
        self.write(rollout(FORK), fork_records())
        self.run_ingest()
        path = self.roots["codex-sessions"] / rollout(PARENT)
        self.append(path, [user_msg(3, "secret q")])
        self.run_ingest()
        self.assertIn("secret q", self.texts(FORK))
        out = self.erase(event_ref=f"codex:{PARENT}:4.1")
        self.assertEqual(out["lines"], 2)  # the parent's line and the copy
        self.assertEqual(self.texts(FORK), ["fork own"])
        self.assertEqual(out["residue"], 0)

    def test_tombstones_replayed_after_a_rebuild_still_block(self) -> None:
        self.write(rollout(PARENT), parent_records())
        self.run_ingest()
        self.erase(event_ref=f"codex:{PARENT}:4.1")
        self.write(rollout(FORK), fork_records())
        self.conn.execute("DELETE FROM tombstone")
        self.conn.execute("DELETE FROM event")
        self.conn.execute("DELETE FROM source")
        self.assertGreater(erase.reapply_tombstones(self.conn, self.home), 0)
        self.write(rollout(PARENT), parent_records()[:3])
        self.run_ingest()
        self.assertEqual(self.texts(FORK), ["fork own"])

    def test_same_text_in_an_unrelated_session_is_kept(self) -> None:
        self.write(rollout(PARENT), parent_records())
        self.write(
            rollout("thr-other"),
            [codex_meta("user", "thr-other"), user_msg(1, "secret q")],
        )
        self.run_ingest()
        self.erase(event_ref=f"codex:{PARENT}:4.1")
        self.run_ingest(full=True)
        self.assertEqual(self.texts("thr-other"), ["secret q"])
