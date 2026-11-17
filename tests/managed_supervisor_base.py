"""Share the managed supervisor fixture without defining any tests."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path
from unittest import mock

from muninn import (
    review_codex_launch,
    review_managed_files,
    review_managed_probe,
    review_runtime_bridge,
    review_supervisor,
)
from muninn.review_json import canonical_json
from muninn.review_launch_inputs import PromptArtifact
from muninn.review_runtime_models import ManagedCallRecord, RuntimeIdentity
from tests.managed_codex_support import (
    fixture_stamps,
    host_enrollment,
    managed_policy,
    package,
    physical_host,
    publish_authority,
)
from tests.review_pause_support import initialize
from tests.test_review_codex_production import experimental_policy
from tests.test_review_requests import RequestCase

_EMPTY_FINAL = (
    "{'op':'final','result':{'schema_version':1,'changes':[],"
    "'unresolved':[],'no_change_reason':'No supported changes'}}"
)
_GOOD = (
    "{'action':'add','form':'note','kind':'fact','scope':data['scope'],"
    "'content':'Keep this fact.','conditions':[],"
    "'evidence_refs':[data['evidence_ids'][0]],'reason':'Supported.'}"
)
_BAD = (
    "{'action':'add','form':'note','kind':'fact',"
    "'scope':{'project_id':'wrong','task':None},'content':'Bad.',"
    "'conditions':[],'evidence_refs':[data['evidence_ids'][0]],"
    "'reason':'Supported.'}"
)
REDUCTION_BODIES = (
    "{'op':'final','result':{'schema_version':1,'changes':["
    + _GOOD
    + ","
    + _BAD
    + "],'unresolved':[],'no_change_reason':None}}",
    "{'op':'final','result':{'schema_version':1,'changes':["
    + _GOOD
    + "],'unresolved':[],'no_change_reason':None}}",
)


class ManagedSupervisorBase(RequestCase):
    """Fake managed clients and temporary stores; holds no tests."""

    policy_revision = 1
    policy_concurrency = 1
    carry_legacy = False

    def setUp(self) -> None:
        super().setUp()
        initialize(self.home)
        self.root = self.tmp / "manager"
        self.marker = self.home / "fake-native-starts"
        self.install("0.166.0")
        artifact = PromptArtifact(
            schema_version=1,
            assembly="muninn-review-prompt-v1",
            instructions=review_codex_launch.JSON_ONLY_INSTRUCTION,
        )
        raw = canonical_json(artifact).encode()
        old = experimental_policy(self.home).model_copy(
            update={"prompt_digest": hashlib.sha256(raw).hexdigest()}
        )
        if self.policy_revision == 3:
            old = old.model_copy(
                update={"policy_id": "muninn-codex-ongoing-production-v3"}
            )
        if self.policy_concurrency != 1:
            old = old.model_copy(
                update={"concurrency": self.policy_concurrency}
            )
        selected = managed_policy(old, self.root)
        if self.carry_legacy:
            self.policy = old
            self.conn.execute(
                "UPDATE review_control SET policy_json=?,policy_digest=?",
                (canonical_json(old), self.digest(old)),
            )
            self.request = self.enqueue()
        self.policy = selected
        self.conn.execute(
            "UPDATE review_control SET policy_json=?,"
            "policy_digest=?,admission=1",
            (canonical_json(self.policy), self.digest(self.policy)),
        )
        capsule = review_runtime_bridge.capture(
            self.conn,
            old,
            self.policy,
            host_enrollment=host_enrollment(old, self.policy),
        )
        publish_authority(self.home, old, self.policy, capsule)
        prompts = self.home / "review-prompts"
        prompts.mkdir(mode=0o700)
        path = prompts / (self.policy.prompt_digest + ".json")
        path.write_bytes(raw)
        path.chmod(0o600)
        if not self.carry_legacy:
            self.request = self.enqueue()
        for patch in (
            mock.patch(
                "muninn.review_managed_host.observe",
                return_value=physical_host(),
            ),
            mock.patch.object(
                review_managed_files,
                "stamp",
                side_effect=fixture_stamps(self.tmp),
            ),
            mock.patch.dict(os.environ, {"MUNINN_HOME": str(self.home)}),
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("platform.machine", return_value="arm64"),
            mock.patch(
                "muninn.review_codex_launch.host_identity",
                return_value="1" * 64,
            ),
            mock.patch.object(
                review_managed_files, "manager_root", return_value=self.root
            ),
            mock.patch.object(
                review_codex_launch, "_has_acl", return_value=False
            ),
            mock.patch.object(
                review_codex_launch,
                "verify_codex_executable",
                side_effect=self.signed,
            ),
            mock.patch.object(
                review_managed_probe, "run_probe", side_effect=self.probe
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.supervisor = review_supervisor.Supervisor(self.home, self.policy)
        self.supervisor.start(12.0)
        self.addCleanup(self.supervisor.stop, 30.0)

    def install(
        self,
        version: str,
        *,
        two_rounds: bool = False,
        bodies: tuple[str, str] | None = None,
    ) -> None:
        """Create only a tiny Python stand-in in the synthetic manager."""
        release = package(self.root, version)
        if bodies is None:
            search = "{'op':'search','query':'missing','scope':data['scope']}"
            bodies = (search if two_rounds else _EMPTY_FINAL, _EMPTY_FINAL)
        source = (
            f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
            f"with Path({str(self.marker)!r}).open('a') as marker: "
            f"marker.write({version!r}+'\\n')\n"
            "data=json.load(sys.stdin)['data']\n"
            f"body={bodies[0]} if data['round_index']==1 else {bodies[1]}\n"
            "events=[{'type':'thread.started','thread"
            "_id':'fixture'},{'type':'turn.started'},"
            "{'type':'item.completed','item':{'id':'i"
            "tem_0','type':'agent_message','text':jso"
            "n.dumps({'payload_json':json.dumps(body)"
            "})}},"
            "{'type':'turn.completed','usage':None}]\n"
            "for event in events: print(json.dumps(event))\n"
        )
        executable = release / "bin/codex"
        executable.write_text(source)
        executable.chmod(0o700)

    def signed(
        self, path: Path, *, limit: int, deadline: float
    ) -> tuple[Path, str]:
        """Fake Apple provenance while keeping real bounded file hashes."""
        return path, review_managed_files.hash_file(path, limit, deadline)[0]

    def probe(
        self, observed: RuntimeIdentity, args: tuple[str, ...], deadline: float
    ) -> bytes:
        """Advertise the required interface without executing native help."""
        self.assertGreater(deadline, time.monotonic())
        if args == ("--version",):
            return f"codex-cli {observed.requested.version}\n".encode()
        self.assertIn(args, (("--help",), ("exec", "--help")))
        return (
            b"--ephemeral --ignore-user-config --ignor"
            b"e-rules --disable --sandbox --skip-git-r"
            b"epo-check --output-schema --json --color"
            b" --model --config --cd"
        )

    def finish(self) -> None:
        """Drive the coordinator until this request ends."""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            self.supervisor.tick(20.0)
            state = self.conn.execute(
                "SELECT state FROM review_request WHERE id=?",
                (self.request.id,),
            ).fetchone()[0]
            if state in {"completed", "failed"}:
                return
            time.sleep(0.01)
        self.fail("synthetic managed request did not reach a terminal state")

    def call_records(self) -> list[ManagedCallRecord]:
        """Read only exact persisted managed call histories."""
        return [
            ManagedCallRecord.model_validate_json(row[0])
            for row in self.conn.execute(
                "SELECT requested_json FROM review_call ORDER BY round_index"
            )
        ]
