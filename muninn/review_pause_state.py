"""Bind private completion evidence to one unchanged lifetime lock."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import Field, model_validator

from muninn import (
    file_sync,
    platform_io,
    platform_lock,
    review_volume_identity,
    store,
)
from muninn.knowledge_models import Digest, SchemaVersion, StrictModel
from muninn.review_json import reject_constant, unique_object
from muninn.review_pause_identity import (
    Identity as Identity,
)
from muninn.review_pause_identity import (
    PauseRefusedError as PauseRefusedError,
)
from muninn.review_pause_identity import (
    SnapshotChangedError as SnapshotChangedError,
)
from muninn.review_pause_identity import (
    directory_identity as directory_identity,
)
from muninn.review_pause_identity import (
    identity as _identity,
)
from muninn.review_pause_identity import (
    lock_identity as lock_identity,
)
from muninn.review_pause_identity import (
    private as _private,
)
from muninn.tombstones import reapply_tombstones

LOCK = "review.supervisor.lock"
WITNESS = "review.supervisor.json"
BASELINE = "review.supervisor.repair.json"
LIMIT = 1024
type Token = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


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
    volume_uuid: (
        Annotated[
            str,
            Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"),
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def active_owner(self) -> Self:
        """Require an exact stop target for every active owner."""
        if self.state == "active" and self.owner is None:
            raise ValueError("active_owner_required")
        if (
            self.volume_uuid is not None
            and self.volume_uuid.replace("-", "") == "0" * 32
        ):
            raise ValueError("pause_unverified")
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
    volume_uuid: (
        Annotated[
            str,
            Field(pattern=(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")),
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def observed_boot(self) -> Self:
        """Reject the nil identifier that cannot establish a boot change."""
        if self.boot.split(":", 1)[1].replace("-", "") == "0" * 32:
            raise ValueError("boot_identity_unavailable")
        if (
            self.volume_uuid is not None
            and self.volume_uuid.replace("-", "") == "0" * 32
        ):
            raise ValueError("pause_unverified")
        return self


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
        platform_io.assert_private_fd(handle.fileno())
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
        if _identity(after) != _identity(latest) and (
            _descriptor_metadata(before)
            == _descriptor_metadata(initial)
            == _descriptor_metadata(after)
        ):
            # Unlink can change ctime; the rejected snapshot grants nothing.
            raise SnapshotChangedError()
        raise PauseRefusedError("pause_unverified")
    return bytes(result), Fingerprint(
        identity=_identity(after), sha256=digest.hexdigest()
    )


def _descriptor_metadata(info: os.stat_result) -> tuple[int, ...]:
    """Compare observed descriptor fields without certifying its bytes."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_mode,
        info.st_uid,
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
    payload = value.model_dump(mode="json")
    if isinstance(value, Witness) and value.volume_uuid is None:
        payload.pop("volume_uuid")
    raw = json.dumps(payload).encode()
    if len(raw) > LIMIT:
        raise PauseRefusedError("pause_unverified")
    store.write_json_atomic(home / name, payload)
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
        raise PauseRefusedError("pause_unverified")
    (home / BASELINE).unlink()
    file_sync.sync_path(home)


def seed_new(home: Path, fd: int, *, bind_volume: bool = True) -> None:
    """Seed a caller-proven new home under held owner and writer ownership.

    Only controlled initialization with affirmative never-exposed provenance
    may call this primitive. Missing records do not establish that provenance.
    The caller retains the non-reentrant writer context throughout the call.

    Raises:
        PauseRefusedError: If prior witness or repair authority exists.
    """
    lock_identity(home, fd)
    if (
        inspect(home, WITNESS)[1] is not None
        or inspect(home, BASELINE)[1] is not None
    ):
        raise PauseRefusedError("pause_unverified")
    publish(
        home,
        fd,
        WITNESS,
        make_witness(home, fd, bind_volume=bind_volume),
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
        if previous is None or not completed_matches(home, previous, identity):
            raise PauseRefusedError("pause_unverified")
        _replay(home)
        retire_baseline(home, fd)
        token = uuid4().hex
        publish(
            home,
            fd,
            WITNESS,
            make_witness(
                home, fd, owner=Owner(pid=os.getpid(), generation=token)
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
        PauseRefusedError: If the exact active owner cannot be verified.
    """
    identity = lock_identity(home, fd)
    previous = parse(inspect(home, WITNESS)[0], Witness)
    owner = Owner(pid=os.getpid(), generation=token)
    if (
        previous is None
        or previous.lock_identity != identity
        or (previous.state != "active" or previous.owner != owner)
        or (
            previous.volume_uuid is not None
            and previous.volume_uuid != review_volume_identity.observe(home)
        )
    ):
        raise PauseRefusedError("pause_unverified")
    publish(
        home, fd, WITNESS, make_witness(home, fd, owner=owner, complete=True)
    )


def make_witness(
    home: Path,
    fd: int,
    *,
    owner: Owner | None = None,
    complete: bool = False,
    bind_volume: bool = True,
) -> Witness:
    """Build current identity evidence while the caller holds its lock.

    Installer preparation keeps legacy output until runtime selection.

    Args:
        home: Private Muninn data directory.
        fd: Held lifetime lock descriptor.
        owner: Active owner, or None for ownerless completion.
        complete: Whether an identified owner has finished containment.
        bind_volume: Whether the selected runtime can read UUID fields.

    Returns:
        A witness bound to the held lock and available native volume.
    """
    identity = lock_identity(home, fd)
    volume = (
        review_volume_identity.observe(home)
        if bind_volume and sys.platform == "darwin"
        else None
    )
    lock_identity(home, fd)
    return Witness(
        version=1,
        lock_identity=identity,
        owner=owner,
        state="complete" if owner is None or complete else "active",
        volume_uuid=volume,
    )


def completed_matches(
    home: Path, witness: Witness, identity: Identity
) -> bool:
    """Match completion by persistent volume and inode on macOS.

    Legacy records retain exact device matching until checked migration.

    Args:
        home: Private Muninn data directory.
        witness: Strictly parsed completion evidence.
        identity: Current checked lock identity.

    Returns:
        Whether completion belongs to the current lock.

    Raises:
        PauseRefusedError: If the recorded volume cannot be verified.
    """
    require_volume(home, witness)
    if witness.state != "complete":
        return False
    if witness.volume_uuid is None:
        return witness.lock_identity == identity
    return witness.lock_identity[1] == identity[1]


def require_volume(home: Path, witness: Witness) -> None:
    """Refuse any recorded UUID that does not match the current volume.

    Raises:
        PauseRefusedError: If the platform or volume differs.
    """
    if witness.volume_uuid is not None and (
        sys.platform != "darwin"
        or review_volume_identity.observe(home) != witness.volume_uuid
    ):
        raise PauseRefusedError("pause_unverified")
