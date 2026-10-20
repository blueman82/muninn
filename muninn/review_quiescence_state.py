"""Bind private completion evidence to one unchanged lifetime lock."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import sys
import threading
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import Field, model_validator

from muninn import file_sync, platform_io, platform_lock, store
from muninn.knowledge_models import Digest, SchemaVersion, StrictModel
from muninn.review_json import reject_constant, unique_object
from muninn.tombstones import reapply_tombstones

LOCK = "review.supervisor.lock"
WITNESS = "review.supervisor.json"
BASELINE = "review.supervisor.repair.json"
LIMIT = 1024
type Identity = tuple[Annotated[int, Field(ge=0)], Annotated[int, Field(ge=0)]]
type Token = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
type RefusalCode = Literal[
    "coordination_timeout",
    "owner_changed",
    "reboot_required",
    "boot_identity_unavailable",
    "quiescence_unverified",
]


class QuiescenceRefusedError(ValueError):
    """Report a finite coordination refusal without exposing content."""

    def __init__(self, code: RefusalCode) -> None:
        """Keep the original finite reason available to the caller."""
        self.code = code
        super().__init__(code)


class Owner(StrictModel):
    """Identify the one process and stop generation selected for a run."""

    pid: Annotated[int, Field(gt=0, le=0xFFFFFFFF)]
    generation: Token


class Witness(StrictModel):
    """Record active or successfully completed ownership of one lock."""

    version: SchemaVersion
    lock_identity: Identity
    owner: Owner | None
    state: Literal["active", "complete"]

    @model_validator(mode="after")
    def active_owner(self) -> Self:
        """Require an exact stop target for every active owner."""
        if self.state == "active" and self.owner is None:
            raise ValueError("active_owner_required")
        return self


class Fingerprint(StrictModel):
    """Bind exact predecessor bytes and their safe regular-file identity."""

    identity: Identity
    sha256: Digest


class Baseline(StrictModel):
    """Bind one uncertain predecessor to a boot observed after its loss."""

    version: SchemaVersion
    lock_identity: Identity
    boot: Annotated[
        str,
        Field(
            pattern=(
                r"^(linux|darwin):[0-9a-f]{8}"
                r"(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"
            )
        ),
    ]
    predecessor: Fingerprint | None

    @model_validator(mode="after")
    def observed_boot(self) -> Self:
        """Reject the nil identifier that cannot establish a boot change."""
        if self.boot.split(":", 1)[1].replace("-", "") == "0" * 32:
            raise ValueError("boot_identity_unavailable")
        return self


def _private(path: Path, *, directory: bool = False) -> os.stat_result:
    """Reject links, public modes and foreign ownership before opening."""
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or not platform_io.is_private(
        path, directory=directory
    ):
        raise QuiescenceRefusedError("quiescence_unverified")
    if os.name != "nt" and info.st_uid != os.getuid():
        raise QuiescenceRefusedError("quiescence_unverified")
    return info


def _identity(info: os.stat_result) -> Identity:
    """Project the native descriptor identity used by the lock protocol."""
    return info.st_dev, info.st_ino


def lock_identity(home: Path, fd: int) -> Identity:
    """Require a private unchanged lock path matching the held descriptor."""
    _private(home, directory=True)
    path = _private(home / LOCK)
    held = os.fstat(fd)
    if not stat.S_ISREG(held.st_mode) or _identity(path) != _identity(held):
        raise QuiescenceRefusedError("quiescence_unverified")
    return _identity(held)


def acquire(home: Path) -> int:
    """Acquire a noninheritable owner without changing the lock inode."""
    _private(home, directory=True)
    with contextlib.suppress(FileNotFoundError):
        _private(home / LOCK)
    fd = platform_lock.acquire(home / LOCK)
    try:
        lock_identity(home, fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def inspect(home: Path, name: str) -> tuple[bytes, Fingerprint | None]:
    """Hash safe exact bytes while retaining at most one bounded record."""
    _private(home, directory=True)
    path = home / name
    try:
        before = _private(path)
    except FileNotFoundError:
        return b"", None
    result = bytearray()
    digest = hashlib.sha256()
    with platform_io.open_regular(path, root=home, expected=before) as handle:
        platform_io.assert_private_fd(handle.fileno())
        initial = os.fstat(handle.fileno())
        while chunk := handle.read(65536):
            digest.update(chunk)
            if len(result) <= LIMIT:
                result.extend(chunk[: LIMIT + 1 - len(result)])
        after = os.fstat(handle.fileno())
    latest = _private(path)

    def fields(value: os.stat_result) -> tuple[int, ...]:
        # Windows path ctime is creation time; descriptor ctime is change time.
        timestamp = (
            value.st_birthtime_ns
            if sys.platform == "win32"
            else value.st_ctime_ns
        )
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            timestamp,
            value.st_mode,
            value.st_uid,
        )

    if (
        fields(before) != fields(initial)
        or fields(initial) != fields(after)
        or (fields(after) != fields(latest))
        or before.st_ctime_ns != latest.st_ctime_ns
        or initial.st_ctime_ns != after.st_ctime_ns
    ):
        raise QuiescenceRefusedError("quiescence_unverified")
    return bytes(result), Fingerprint(
        identity=_identity(after), sha256=digest.hexdigest()
    )


def parse[T: StrictModel](raw: bytes, model: type[T]) -> T | None:
    """Refuse oversized, duplicate, trailing or non-strict private records."""
    try:
        if len(raw) > LIMIT:
            return None
        json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        return model.model_validate_json(raw, strict=True)
    except (ValueError, RecursionError):
        return None


def publish(home: Path, fd: int, name: str, value: StrictModel) -> None:
    """Publish private bounded state and sync the parent before proceeding."""
    lock_identity(home, fd)
    inspect(home, name)
    raw = value.model_dump_json().encode()
    if len(raw) > LIMIT:
        raise QuiescenceRefusedError("quiescence_unverified")
    store.write_json_atomic(home / name, value.model_dump(mode="json"))
    if os.name != "nt":
        file_sync.sync_path(home)
    lock_identity(home, fd)


def retire_baseline(home: Path, fd: int) -> None:
    """Durably retire old repair authority before exposing new material."""
    lock_identity(home, fd)
    _, fingerprint = inspect(home, BASELINE)
    if fingerprint is None:
        return
    if os.name == "nt":
        raise QuiescenceRefusedError("quiescence_unverified")
    (home / BASELINE).unlink()
    file_sync.sync_path(home)


def seed_new(home: Path, fd: int) -> None:
    """Seed a caller-proven new home under held owner and writer ownership.

    Only controlled initialization with affirmative never-exposed provenance
    may call this primitive. Missing records do not establish that provenance.
    The caller retains the non-reentrant writer context throughout the call.

    Raises:
        QuiescenceRefusedError: If prior witness or repair authority exists.
    """
    identity = lock_identity(home, fd)
    if (
        inspect(home, WITNESS)[1] is not None
        or inspect(home, BASELINE)[1] is not None
    ):
        raise QuiescenceRefusedError("quiescence_unverified")
    publish(
        home,
        fd,
        WITNESS,
        Witness(
            version=1, lock_identity=identity, owner=None, state="complete"
        ),
    )


def _replay(home: Path) -> None:
    """Finish current intent under caller ownership before material access."""
    if not store.db_path(home).is_file():
        raise store.StoreUnavailableError("store is absent")
    conn = store.connect_rw(store.db_path(home))
    try:
        reapply_tombstones(conn, home)
    except BaseException:
        with contextlib.suppress(BaseException):
            conn.close()
        raise
    conn.close()


def begin_owner(home: Path) -> tuple[int, str]:
    """Validate completion and publish active before loading any material."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("main_thread_required")
    fd = acquire(home)
    resources = contextlib.ExitStack()
    try:
        resources.enter_context(store.writer_lock(home, wait_s=0))
        identity = lock_identity(home, fd)
        previous = parse(inspect(home, WITNESS)[0], Witness)
        if (
            previous is None
            or previous.state != "complete"
            or (previous.lock_identity != identity)
        ):
            raise QuiescenceRefusedError("quiescence_unverified")
        _replay(home)
        retire_baseline(home, fd)
        token = uuid4().hex
        publish(
            home,
            fd,
            WITNESS,
            Witness(
                version=1,
                lock_identity=identity,
                owner=Owner(pid=os.getpid(), generation=token),
                state="active",
            ),
        )
        resources.close()
        return fd, token
    except BaseException:
        with contextlib.suppress(BaseException):
            resources.close()
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


def complete_owner(home: Path, fd: int, token: str) -> None:
    """Certify caller-established cleanup for this still-held exact owner.

    The caller must first join containment, commit durable reconciliation and
    discard every retained main and monitor material reference. This helper
    cannot infer those facts from a released lock or an absent process.

    Raises:
        QuiescenceRefusedError: If the exact active owner cannot be verified.
    """
    identity = lock_identity(home, fd)
    previous = parse(inspect(home, WITNESS)[0], Witness)
    owner = Owner(pid=os.getpid(), generation=token)
    if (
        previous is None
        or previous.lock_identity != identity
        or (previous.state != "active" or previous.owner != owner)
    ):
        raise QuiescenceRefusedError("quiescence_unverified")
    publish(
        home, fd, WITNESS, previous.model_copy(update={"state": "complete"})
    )
