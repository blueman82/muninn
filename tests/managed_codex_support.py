"""Build checked-shape runtime facts without executing a native client."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from muninn import (
    review_codex_launch,
    review_managed_files,
    review_managed_launch,
    review_runtime_authority,
)
from muninn.review_codex_output import output_contract_digest
from muninn.review_codex_policy import CodexExperiment, ManagedCodexExperiment
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from muninn.review_rounds import round_schema
from muninn.review_runtime_models import (
    EnrollmentApproval,
    FileStamp,
    HostEnrollment,
    InitialBridge,
    ManagedCapability,
    ManagedCodexSelection,
    ManagedHostIdentity,
    ManagedInterfaceBinding,
    RunnerConfig,
    RuntimeIdentity,
)


def physical_host(value: str = "6" * 64) -> ManagedHostIdentity:
    """Supply synthetic physical identity without invoking Apple APIs."""
    return ManagedHostIdentity(kind="darwin-ioplatformuuid-v1", digest=value)


def managed_policy(old: ReviewPolicy, root: Path) -> ReviewPolicy:
    """Construct typed fixture enrollment while keeping all other terms."""
    terms = old.experimental_codex
    assert isinstance(terms, (CodexExperiment, ManagedCodexExperiment))
    selected = ManagedCodexExperiment(
        kind="ongoing-mac-codex-managed-v1",
        activation_authorized=terms.activation_authorized,
        host_identity=physical_host(),
        data_home=terms.data_home,
        accepted_uncertainties=terms.accepted_uncertainties,
    )
    return ReviewPolicy.model_validate(
        old.model_dump()
        | {
            "runner": selection(root),
            "experimental_codex": selected,
        }
    )


def host_enrollment(old: ReviewPolicy, new: ReviewPolicy) -> HostEnrollment:
    """Keep legacy observations synthetic and separate from new authority."""
    assert isinstance(old.experimental_codex, CodexExperiment)
    assert isinstance(new.experimental_codex, ManagedCodexExperiment)
    return HostEnrollment(
        kind="codex-managed-host-enrollment-v1",
        saved_legacy_host=old.experimental_codex.host_identity,
        observed_legacy_host=old.experimental_codex.host_identity,
        target=new.experimental_codex.host_identity,
    )


def fixture_stamps(base: Path) -> Callable[[Path], FileStamp]:
    """Model Darwin parent modes without hiding disposable fixture changes."""
    actual = review_managed_files.stamp
    ancestors = set(base.parents)

    def read(path: Path) -> FileStamp:
        info = actual(path)
        if path in ancestors:
            return info.model_copy(update={"mode": info.mode & ~0o022})
        return info

    return read


@contextmanager
def private_authority_fixture(home: Path) -> Generator[None]:
    """Model private Darwin fixture I/O without replacing proof validation."""
    parents = {
        home / "review-capabilities" / name
        for name in ("", "selected", "managed-approvals", "managed-bridges")
    }
    directories = parents | {home, home / "review-runner-cwd"}

    def check(path: Path, *, directory: bool) -> None:
        review_runtime_authority.canonical_path(path)
        if directory:
            valid = path in directories and path.is_dir()
        else:
            valid = (
                path == home / "review-codex-schema.json"
                or (
                    path.parent in parents
                    and re.fullmatch(r"[0-9a-f]{64}\.json", path.name)
                    is not None
                )
            ) and path.is_file()
        if not valid:
            raise ValueError("capability_storage")

    def read(path: Path, limit: int) -> bytes:
        check(path, directory=False)
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("capability_limit")
        return raw

    with (
        mock.patch.object(
            review_runtime_authority, "check_private_path", side_effect=check
        ),
        mock.patch.object(
            review_runtime_authority, "read_private_file", side_effect=read
        ),
    ):
        yield


def selection(root: Path) -> ManagedCodexSelection:
    """Bind the fixture manager without using a process or real HOME."""
    return ManagedCodexSelection(
        selection="codex_managed_interface_v1",
        runner="codex_cli",
        manager_root=root,
        selector="current",
        interface="codex-json-review-v1",
        model="gpt-6.1-sol",
        reasoning="low",
    )


def identity(root: Path, version: str = "0.166.0") -> RuntimeIdentity:
    """Return complete synthetic stamps and exact fixed hashes."""
    executable = root / "releases" / version / "codex"
    stamp = FileStamp(
        path=executable,
        device=1,
        inode=2,
        size=3,
        mtime_ns=4,
        ctime_ns=5,
        mode=0o100700,
        uid=1000,
        gid=1000,
        link_target=None,
    )
    return RuntimeIdentity(
        requested=RunnerConfig(
            runner="codex_cli",
            executable=str(executable),
            version=version,
            model="gpt-6.1-sol",
            reasoning="low",
        ),
        package_root=executable.parent,
        executable_sha256="a" * 64,
        package_sha256="b" * 64,
        platform="Darwin/arm64",
        selector_stamp=stamp.model_copy(update={"path": root / "current"}),
        file_stamps=(stamp,),
        recipe_digest="c" * 64,
        schema_digest="d" * 64,
        validator_digest="e" * 64,
        capability_key="f" * 64,
    )


def package(root: Path, version: str = "0.166.0") -> Path:
    """Create a complete tiny standalone layout in a temporary manager."""
    release = root / "releases" / (version + "-aarch64-apple-darwin")
    for directory in ("bin", "codex-path", "codex-resources"):
        (release / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
    for relative in (
        "bin/codex",
        "bin/codex-exec-server",
        "bin/codex-code-mode-host",
        "codex-path/rg",
    ):
        path = release / relative
        path.write_bytes(b"synthetic executable " + version.encode())
        path.chmod(0o700)
    (release / "codex").unlink(missing_ok=True)
    (release / "codex").symlink_to("bin/codex")
    (release / "codex-package.json").write_text(
        '{"layoutVersion":1,"version":"' + version + '",'
        '"target":"aarch64-apple-darwin","variant":"codex",'
        '"entrypoint":"bin/codex","execServer":"bin/codex-exec-server",'
        '"resourcesDir":"codex-resources","pathDir":"codex-path"}'
    )
    current = root / "current"
    current.unlink(missing_ok=True)
    current.symlink_to(release)
    return release


def publish_authority(
    home: Path, old: ReviewPolicy, new: ReviewPolicy, capsule: InitialBridge
) -> ManagedCapability:
    """Publish synthetic explicit approval metadata only in the test home."""
    directory = home / "review-capabilities"
    directory.mkdir(mode=0o700, exist_ok=True)
    cwd = home / "review-runner-cwd"
    cwd.mkdir(mode=0o700, exist_ok=True)
    auth = home.parent / "fake-managed-auth"
    auth.mkdir(mode=0o700, exist_ok=True)
    schema = home / "review-codex-schema.json"
    schema.write_bytes(review_codex_launch.wire_schema())
    schema.chmod(0o600)
    bridge_digest = hashlib.sha256(
        canonical_json(capsule).encode()
    ).hexdigest()
    approval = EnrollmentApproval(
        kind="codex-managed-enrollment-v1",
        old_policy_digest=digest(old),
        new_policy_digest=digest(new),
        bridge_digest=bridge_digest,
        host_identity=capsule.host_enrollment.target,
        host_enrollment=capsule.host_enrollment,
        legacy_artifacts_digest="a" * 64,
        auth_home=auth,
        operation_id="fixture-operation",
        plan_digest="a" * 64,
        approved_at=10.0,
        approval="owner_yes",
    )
    approved = hashlib.sha256(canonical_json(approval).encode()).hexdigest()
    assert isinstance(new.runner, ManagedCodexSelection)
    binding = ManagedInterfaceBinding(
        kind="codex-managed-binding-v1",
        requested=new.runner,
        host_identity=capsule.host_enrollment.target,
        auth_home=auth,
        cli_cwd=cwd,
        schema_path=schema,
        output_contract_digest=output_contract_digest(),
        enrollment_approval=approved,
        initial_bridge=bridge_digest,
    )
    proof = hashlib.sha256(canonical_json(binding).encode()).hexdigest()
    capability = ManagedCapability(
        kind="codex-managed-capability-v1",
        requested=new.runner,
        proof_path=directory / (proof + ".json"),
        schema_digest=hashlib.sha256(
            canonical_json_value(round_schema()).encode()
        ).hexdigest(),
        config_digest=review_managed_launch.fingerprint(binding),
    )
    for relative, raw in (
        (
            "managed-bridges/" + bridge_digest + ".json",
            canonical_json(capsule),
        ),
        ("managed-approvals/" + approved + ".json", canonical_json(approval)),
        (proof + ".json", canonical_json(binding)),
        ("selected/" + digest(new) + ".json", canonical_json(capability)),
    ):
        path = directory / relative
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_text(raw)
        path.chmod(0o600)
    return capability
