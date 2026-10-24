"""Retain maintenance barriers and verify predecessor containment."""

from __future__ import annotations

import contextlib
import math
import os
import sqlite3
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from muninn import (
    poller_stop,
    review_boot_identity,
    review_volume_identity,
    store,
)
from muninn import review_quiescence_state as state
from muninn.review_quiescence_state import (
    QuiescenceRefusedError as QuiescenceRefusedError,
)
from muninn.review_repair_binding import same_repair_binding
from muninn.review_requests import recover_attempts
from muninn.tombstones import reapply_tombstones

_POLL_S = 0.1


@dataclass
class EraseStatus:
    """Retain independent truthful milestones through original exceptions."""

    stop_may_have_been_published: bool = False
    repair_phase: Literal[
        "none", "baseline_recorded", "reconciling", "complete"
    ] = "none"
    erasure_phase: Literal[
        "not_started",
        "intent_may_be_durable",
        "intent_durable",
        "committed",
        "verified",
    ] = "not_started"


@dataclass
class MaintenanceOwner:
    """Carry transient program-owned scope state, never containment proof."""

    home: Path
    fd: int
    selected: state.Witness | None
    deadline: float
    status: EraseStatus
    home_identity: tuple[int, int]
    active: bool = field(default=False, init=False)
    writer_resources: contextlib.ExitStack | None = field(
        default=None, init=False
    )
    writer_held: bool = field(default=False, init=False)


@contextlib.contextmanager
def _resources() -> Generator[contextlib.ExitStack]:
    """Release every resource without replacing an original exception."""
    resources = contextlib.ExitStack()
    try:
        yield resources
    except BaseException:
        with contextlib.suppress(BaseException):
            resources.close()
        raise
    else:
        resources.close()


def _main_thread() -> None:
    """Require synchronous main-thread maintenance."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("main_thread_required")


def _check_wait(wait_s: float) -> None:
    """Validate the caller's acquisition budget before any coordination."""
    _main_thread()
    if not math.isfinite(wait_s) or wait_s < 0:
        raise ValueError("invalid_wait")


def _require_owner(owner: MaintenanceOwner) -> None:
    """Require a live scope bound to its unchanged home and descriptor."""
    _main_thread()
    if not owner.active:
        raise RuntimeError("maintenance_owner_closed")
    home = owner.home.stat()
    if (home.st_dev, home.st_ino) != owner.home_identity:
        raise QuiescenceRefusedError("quiescence_unverified")
    state.lock_identity(owner.home, owner.fd)


def _close_writer(owner: MaintenanceOwner) -> None:
    """Release a misnested writer before its lifetime owner is released."""
    owner.writer_held = False
    if owner.writer_resources is not None:
        owner.writer_resources.close()


@contextlib.contextmanager
def maintenance_owner(
    home: Path, *, wait_s: float, status: EraseStatus
) -> Generator[MaintenanceOwner]:
    """Retain lifetime exclusion without opening a store or proving freshness.

    Args:
        home: Existing private data directory.
        wait_s: Finite nonnegative budget shared by all acquisition waits.
        status: Caller-owned milestones retained through original exceptions.

    Yields:
        The program-owned scope for sequential, nonnested writer regions.

    Raises:
        RuntimeError: If a writer region outlives this owner or off main.
        ValueError: If the wait is invalid or coordination is refused.
    """
    _check_wait(wait_s)
    deadline = time.monotonic() + wait_s
    info = home.stat()
    with _resources() as resources:
        fd, selected = _owner(home, deadline, status)
        resources.callback(os.close, fd)
        owner = MaintenanceOwner(
            home, fd, selected, deadline, status, (info.st_dev, info.st_ino)
        )
        resources.callback(_close_writer, owner)
        owner.active = True
        try:
            _require_owner(owner)
            if selected is not None:
                _same_owner(selected, _latest(home))
            yield owner
            if owner.writer_resources is not None:
                raise RuntimeError("maintenance_writer_active")
        finally:
            owner.active = False


@contextlib.contextmanager
def maintenance_writer(owner: MaintenanceOwner) -> Generator[None]:
    """Hold one writer region without opening a database or spawning work.

    Close caller-owned connections and discard material before exit. Never
    start a process, call a model or nest another writer-owning operation.

    Args:
        owner: Live maintenance owner retained across all writer regions.

    Yields:
        Nothing; the writer barrier is held through the caller's work.

    Raises:
        RuntimeError: If called off main, outside the owner or nested.
        store.BusyError: If the shared acquisition budget expires.
    """
    _require_owner(owner)
    if owner.writer_resources is not None:
        raise RuntimeError("maintenance_writer_active")
    try:
        with _resources() as resources:
            owner.writer_resources = resources
            _writer(owner.home, owner.deadline, resources)
            _require_owner(owner)
            owner.writer_held = True
            try:
                yield
            finally:
                owner.writer_held = False
    finally:
        owner.writer_resources = None


def verify_current(owner: MaintenanceOwner, conn: sqlite3.Connection) -> None:
    """Verify completion and replay intent for the caller's current store.

    Open the connection after entering this writer region; close it before
    exit or pathname replacement. Never reuse it across regions. The path
    check cannot authenticate SQLite's backing file or detect stale handles;
    source identity validation and publication remain the caller's duty.

    Args:
        owner: Live owner whose writer barrier is currently held.
        conn: Caller-owned marked-store autocommit connection, without a TX.

    Raises:
        RuntimeError: If either scope is absent or the caller is off main.
        ValueError: If the connection contract or containment is unverified.
        store.StoreUnavailableError: If the current format marker is absent.
    """
    _require_owner(owner)
    if not owner.writer_held:
        raise RuntimeError("maintenance_writer_required")
    autocommit = conn.autocommit is True or (
        conn.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL
        and conn.isolation_level is None
    )
    if not autocommit or conn.in_transaction:
        raise ValueError("maintenance_autocommit_required")
    paths = [
        row[2]
        for row in conn.execute("PRAGMA database_list")
        if row[1] == "main"
    ]
    if (
        not paths
        or not paths[0]
        or Path(paths[0]).resolve() != store.db_path(owner.home).resolve()
    ):
        raise ValueError("maintenance_store_mismatch")
    store.assert_store_format(conn)
    _verify(
        owner.home,
        owner.fd,
        conn,
        repair=False,
        selected=owner.selected,
        status=owner.status,
    )


def _latest(home: Path) -> state.Witness | None:
    """Read a safe bounded witness without treating a routing hint as proof."""
    return state.parse(state.inspect(home, state.WITNESS)[0], state.Witness)


def _same_owner(
    selected: state.Witness | None, latest: state.Witness | None
) -> None:
    """Refuse any observed replacement of the one selected stop target."""
    if selected is not None and (
        latest is None
        or latest.owner != selected.owner
        or latest.lock_identity != selected.lock_identity
    ):
        raise QuiescenceRefusedError("owner_changed")


def _owner(
    home: Path, deadline: float, status: EraseStatus
) -> tuple[int, state.Witness | None]:
    """Acquire lifetime ownership while targeting at most one active owner."""
    selected = None
    while True:
        try:
            return state.acquire(home), selected
        except BlockingIOError:
            latest = _latest(home)
            _same_owner(selected, latest)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QuiescenceRefusedError("coordination_timeout") from None
            if (
                selected is None
                and latest is not None
                and latest.state == "active"
            ):
                assert latest.owner is not None
                lock = (home / state.LOCK).stat()
                if latest.lock_identity != (lock.st_dev, lock.st_ino):
                    raise QuiescenceRefusedError(
                        "quiescence_unverified"
                    ) from None
                selected = latest
                status.stop_may_have_been_published = True
                poller_stop.request(
                    home,
                    latest.owner.pid,
                    latest.owner.generation,
                    deadline=deadline,
                )
                continue
            time.sleep(min(_POLL_S, remaining))


def _writer(
    home: Path, deadline: float, resources: contextlib.ExitStack
) -> None:
    """Retain one immediate writer context within the shared budget."""
    while True:
        try:
            resources.enter_context(store.writer_lock(home, wait_s=0))
            return
        except store.BusyError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(_POLL_S, remaining))


def _recover(conn: sqlite3.Connection, home: Path) -> None:
    """Commit fencing before caller-owned replay of current intent."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE review_control SET generation=generation+1")
        generation = conn.execute(
            "SELECT generation FROM review_control"
        ).fetchone()[0]
        recover_attempts(conn, generation, time.time())
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    reapply_tombstones(conn, home)


def _verify(
    home: Path,
    fd: int,
    conn: sqlite3.Connection,
    *,
    repair: bool,
    selected: state.Witness | None,
    status: EraseStatus,
) -> None:
    """Require completion or a later boot bound to an unchanged predecessor."""
    identity = state.lock_identity(home, fd)
    raw, predecessor = state.inspect(home, state.WITNESS)
    latest = state.parse(raw, state.Witness)
    _same_owner(selected, latest)
    baseline_raw, baseline_file = state.inspect(home, state.BASELINE)
    if (
        latest is not None
        and latest.lock_identity == identity
        and latest.state == "complete"
    ):
        if baseline_file is not None and not repair:
            raise QuiescenceRefusedError("quiescence_unverified")
        reapply_tombstones(conn, home)
        state.retire_baseline(home, fd)
        return
    if not repair:
        raise QuiescenceRefusedError("quiescence_unverified")
    boot = review_boot_identity.observe()
    baseline = state.parse(baseline_raw, state.Baseline)
    if (
        baseline is None
        or baseline.boot.split(":")[0] != boot.split(":")[0]
        or not same_repair_binding(home, baseline, identity, predecessor, boot)
    ):
        volume_uuid = (
            review_volume_identity.observe(home)
            if boot.startswith("darwin:")
            else None
        )
        state.publish(
            home,
            fd,
            state.BASELINE,
            state.Baseline(
                version=1,
                lock_identity=identity,
                boot=boot,
                predecessor=predecessor,
                volume_uuid=volume_uuid,
            ),
        )
        status.repair_phase = "baseline_recorded"
        raise QuiescenceRefusedError("reboot_required")
    if baseline.boot == boot:
        status.repair_phase = "baseline_recorded"
        raise QuiescenceRefusedError("reboot_required")
    status.repair_phase = "reconciling"
    _recover(conn, home)
    if state.inspect(home, state.WITNESS)[1] != predecessor or (
        state.inspect(home, state.BASELINE)[1] != baseline_file
    ):
        raise QuiescenceRefusedError("quiescence_unverified")
    state.publish(
        home,
        fd,
        state.WITNESS,
        state.Witness(
            version=1, lock_identity=identity, owner=None, state="complete"
        ),
    )
    state.retire_baseline(home, fd)
    status.repair_phase = "complete"


@contextlib.contextmanager
def erase_connection(
    home: Path, *, wait_s: float, status: EraseStatus
) -> Generator[sqlite3.Connection]:
    """Yield an autocommit store while retaining owner then writer barriers.

    The budget covers programmed coordination waits, not repair, requested
    erasure work or the duration of already-entered filesystem operations.
    Caller status remains available even when the original operation raises.

    Raises:
        RuntimeError: If invoked outside the main thread.
        ValueError: If the coordination budget is invalid.
        store.StoreUnavailableError: If there is no current store.
    """
    _check_wait(wait_s)
    if not store.db_path(home).is_file():
        raise store.StoreUnavailableError("store is absent")
    with (
        maintenance_owner(home, wait_s=wait_s, status=status) as owner,
        maintenance_writer(owner),
        _resources() as resources,
    ):
        if not store.db_path(home).is_file():
            raise store.StoreUnavailableError("store is absent")
        conn = store.connect_rw(store.db_path(home))
        resources.callback(conn.close)
        _verify(
            home,
            owner.fd,
            conn,
            repair=wait_s > 0,
            selected=owner.selected,
            status=status,
        )
        yield conn
