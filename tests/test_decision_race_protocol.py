"""Require native contention and release before a race worker can mutate."""

from __future__ import annotations

import multiprocessing
import sqlite3
from multiprocessing.process import BaseProcess
from pathlib import Path
from unittest import mock

from muninn import store
from muninn.decision_write import submit_intent
from muninn.review_json import canonical_json
from tests import test_decision_races as races
from tests.test_decision_races import Connection, race_writer
from tests.test_decision_resolution import ResolutionCase


class DecisionRaceProtocolTests(ResolutionCase):
    """Exercise worker barriers against real disposable stores and locks."""

    def setUp(self) -> None:
        super().setUp()
        ident, context = self.application()
        intent = self.intent(ident, action="reject")
        self.transaction(lambda: submit_intent(self.conn, intent, 23.5))
        self.arguments = (
            str(self.home),
            "negative",
            canonical_json(context),
            canonical_json(intent),
        )

    def channels(self) -> tuple[Connection, Connection]:
        """Own both pipe endpoints even when a protocol assertion fails."""
        parent, child = multiprocessing.get_context("spawn").Pipe()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        return parent, child

    def receive(self, channel: Connection) -> object:
        """Fail within the existing parent deadline if a worker exits."""
        try:
            self.assertTrue(channel.poll(5), "writer barrier timed out")
            return channel.recv()
        except (EOFError, OSError) as exc:
            self.fail(f"writer closed before acknowledgment: {exc}")

    def stop_process(self, process: BaseProcess) -> None:
        """Contain a worker that is still parked after a failed assertion."""
        if process.is_alive():
            process.terminate()
        process.join(5)

    def start_writer(self) -> tuple[BaseProcess, Connection]:
        """Start the actual race worker before taking any parent lock."""
        parent, child = self.channels()
        process = multiprocessing.get_context("spawn").Process(
            target=race_writer, args=(*self.arguments, child)
        )
        process.start()
        child.close()
        self.addCleanup(self.stop_process, process)
        self.assertEqual(self.receive(parent), "ready")
        return process, parent

    def test_contended_writer_parks_until_release_and_retry(self) -> None:
        process, parent = self.start_writer()
        before = self.projection()
        with store.writer_lock(self.home, wait_s=0):
            parent.send("contend")
            self.assertEqual(self.receive(parent), "attempting")
            self.assertEqual(self.receive(parent), "contended")
            self.assertEqual(self.projection(), before)
        self.assertEqual(self.projection(), before)
        parent.send("retry")
        self.assertEqual(self.receive(parent), "locked")
        self.assertEqual(self.projection(), before)
        parent.send("finish")
        self.assertEqual(
            self.receive(parent), ("released", ("rejected", None, None))
        )
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        with store.writer_lock(self.home, wait_s=0):
            pass

    def test_contender_refuses_to_claim_contention_without_a_holder(
        self,
    ) -> None:
        parent, child = self.channels()
        parent.send("contend")
        parent.send("finish")
        before = self.projection()
        with self.assertRaisesRegex(AssertionError, "did not contend"):
            race_writer(*self.arguments, child)
        self.assertEqual(self.projection(), before)

    def test_early_retry_still_fails_the_native_guard(self) -> None:
        parent, child = self.channels()
        parent.send("contend")
        parent.send("retry")
        parent.send("finish")
        before = self.projection()
        with (
            store.writer_lock(self.home, wait_s=0),
            self.assertRaises(store.BusyError),
        ):
            race_writer(*self.arguments, child)
        self.assertEqual(self.receive(parent), "ready")
        self.assertEqual(self.receive(parent), "attempting")
        self.assertEqual(self.receive(parent), "contended")
        self.assertFalse(parent.poll())
        self.assertEqual(self.projection(), before)

    def test_invalid_initial_commands_cannot_mutate(self) -> None:
        before = self.projection()
        for command in (None, 1, ("go",), "retry", "finish"):
            with self.subTest(command=command):
                parent, child = self.channels()
                parent.send(command)
                parent.send("finish")
                with self.assertRaisesRegex(ValueError, "writer command"):
                    race_writer(*self.arguments, child)
                self.assertEqual(self.projection(), before)

    def test_invalid_retry_cannot_mutate(self) -> None:
        before = self.projection()
        for command in (None, 1, ("retry",), "go", "finish"):
            with self.subTest(command=command):
                parent, child = self.channels()
                parent.send("contend")
                parent.send(command)
                parent.send("finish")
                with (
                    store.writer_lock(self.home, wait_s=0),
                    self.assertRaisesRegex(ValueError, "writer command"),
                ):
                    race_writer(*self.arguments, child)
                self.assertEqual(self.projection(), before)

    def test_invalid_finish_releases_lock_without_mutation(self) -> None:
        before = self.projection()
        for command in (None, 1, ("finish",), "go", "retry"):
            with self.subTest(command=command):
                parent, child = self.channels()
                parent.send("go")
                parent.send(command)
                with self.assertRaisesRegex(ValueError, "writer command"):
                    race_writer(*self.arguments, child)
                with store.writer_lock(self.home, wait_s=0):
                    self.assertEqual(self.projection(), before)

    def test_closed_channel_releases_lock_without_mutation(self) -> None:
        parent, child = self.channels()
        parent.send("go")
        before = self.projection()
        send = child.send

        def close_parent(message: object) -> None:
            send(message)
            if message == "locked":
                parent.close()

        with (
            mock.patch.object(child, "send", side_effect=close_parent),
            self.assertRaises((EOFError, OSError)),
        ):
            race_writer(*self.arguments, child)
        with store.writer_lock(self.home, wait_s=0):
            self.assertEqual(self.projection(), before)

    def test_released_acknowledgment_follows_both_resource_closes(
        self,
    ) -> None:
        parent, child = self.channels()
        parent.send("go")
        parent.send("finish")
        opened: list[sqlite3.Connection] = []
        connect = store.connect_rw
        send = child.send
        released = []

        def observe_connection(path: Path) -> sqlite3.Connection:
            conn = connect(path)
            opened.append(conn)
            return conn

        def observe_release(message: object) -> None:
            if isinstance(message, tuple) and message[0] == "released":
                self.assertEqual(len(opened), 1)
                with self.assertRaises(sqlite3.ProgrammingError):
                    opened[0].execute("SELECT 1")
                with store.writer_lock(self.home, wait_s=0):
                    released.append(message)
            send(message)

        with (
            mock.patch.object(
                store, "connect_rw", side_effect=observe_connection
            ),
            mock.patch.object(child, "send", side_effect=observe_release),
        ):
            race_writer(*self.arguments, child)
        self.assertEqual(released, [("released", ("rejected", None, None))])

    def test_cleanup_contains_a_worker_waiting_for_parent_command(
        self,
    ) -> None:
        process, parent = self.start_writer()
        self.stop_process(process)
        self.assertFalse(process.is_alive())
        self.assertIsNotNone(process.exitcode)
        with self.assertRaises(AssertionError):
            self.receive(parent)

    def test_parent_rejects_invalid_release_acknowledgments(self) -> None:
        case = races.DecisionRaceTests(
            "test_both_decision_application_race_orders_apply_before_reject"
        )
        for message in (
            None,
            1,
            "released",
            ("released",),
            ("locked", None),
            ["released", None],
            ("released", None, None),
        ):
            with self.subTest(message=message):
                parent, child = self.channels()
                child.send(message)
                with self.assertRaises(AssertionError):
                    case.receive_released(parent)
