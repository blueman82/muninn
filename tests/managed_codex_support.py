"""Build checked-shape runtime facts without executing a native client."""

from __future__ import annotations

import hashlib
from pathlib import Path

from muninn import review_codex_launch, review_managed_launch
from muninn.review_codex_output import output_contract_digest
from muninn.review_json import canonical_json, canonical_json_value
from muninn.review_models import ReviewPolicy
from muninn.review_progress import digest
from muninn.review_rounds import round_schema
from muninn.review_runtime_models import (
    EnrollmentApproval,
    FileStamp,
    InitialBridge,
    ManagedCapability,
    ManagedCodexSelection,
    ManagedInterfaceBinding,
    RunnerConfig,
    RuntimeIdentity,
)


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
        host_identity="1" * 64,
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
        host_identity="1" * 64,
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
