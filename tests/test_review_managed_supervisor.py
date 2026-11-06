"""Drive real owned wrappers with synthetic local clients and temp stores."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock, skipIf

from muninn import (
    review_codex_launch,
    review_managed_files,
    review_managed_probe,
    review_runtime_bridge,
    review_supervisor,
    review_supervisor_result,
)
from muninn.review_json import canonical_json
from muninn.review_launch_inputs import PromptArtifact
from muninn.review_models import ReviewContext
from muninn.review_process import WorkerReady
from muninn.review_round_state import Round
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


@skipIf(os.name == "nt", "Darwin managed route uses POSIX test stand-ins")
class ManagedSupervisorTests(RequestCase):
    """Exercise preparation with fake clients and temporary stores."""

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

    def install(self, version: str, *, two_rounds: bool = False) -> None:
        """Create only a tiny Python stand-in in the synthetic manager."""
        release = package(self.root, version)
        source = (
            f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
            f"with Path({str(self.marker)!r}).open('a') as marker: "
            f"marker.write({version!r}+'\\n')\n"
            "data=json.load(sys.stdin)['data']\n"
            "body={'op':'search','query':'missing','scope':data['scope']} "
            f"if {two_rounds!r} and data['round_index']==1 else "
            "{'op':'final','result':{'schema_version'"
            ":1,'changes':[],'unresolved':[],'no_chan"
            "ge_reason':'No supported changes'}}\n"
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

    def test_two_round_update_preserves_policy_attempt_and_each_execution(
        self,
    ) -> None:
        self.install("0.166.0", two_rounds=True)
        original = review_supervisor_result._advance

        def advance(pending: Round, context: ReviewContext) -> None:
            original(pending, context)
            self.install("0.167.0", two_rounds=True)

        with mock.patch(
            "muninn.review_supervisor_result._advance", side_effect=advance
        ):
            self.finish()
        records = self.call_records()
        self.assertEqual(len(records), 2)
        self.assertEqual(
            [record.candidates[-1].requested.version for record in records],
            ["0.166.0", "0.167.0"],
        )
        self.assertEqual(
            [record.launched_identity for record in records],
            [record.candidates[-1].digest for record in records],
        )
        self.assertEqual(
            self.marker.read_text().splitlines(), ["0.166.0", "0.167.0"]
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT policy_digest FROM review_control"
            ).fetchone()[0],
            self.digest(self.policy),
        )
        self.assertTrue(
            all(
                value is None
                for value in json.loads(
                    self.conn.execute(
                        "SELECT observed_json FROM review_call LIMIT 1"
                    ).fetchone()[0]
                ).values()
            )
        )

    def test_update_at_readiness_appends_candidate_without_extra_charge(
        self,
    ) -> None:
        original = self.supervisor._persist
        changed = False

        def ready(event: review_supervisor.ProcessEvent, now: float) -> None:
            nonlocal changed
            original(event, now)
            if isinstance(event, WorkerReady) and not changed:
                changed = True
                self.install("0.167.0")

        with mock.patch.object(self.supervisor, "_persist", side_effect=ready):
            self.finish()
        record = self.call_records()[0]
        self.assertEqual(
            [item.requested.version for item in record.candidates],
            ["0.166.0", "0.167.0"],
        )
        self.assertEqual(record.refresh_count, 1)
        self.assertEqual(self.marker.read_text().splitlines(), ["0.167.0"])
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )

    def test_update_after_offer_gets_one_acknowledged_wrapper_replacement(
        self,
    ) -> None:
        original = self.supervisor.monitor.activate
        changed = False

        def activate(attempt: str, index: int, frame: bytes) -> None:
            nonlocal changed
            if not changed:
                changed = True
                self.install("0.167.0")
            original(attempt, index, frame)

        with mock.patch.object(
            self.supervisor.monitor, "activate", side_effect=activate
        ):
            self.finish()
        record = self.call_records()[0]
        self.assertEqual(
            [event.kind for event in record.events],
            ["offer", "prestart_refusal", "offer"],
        )
        self.assertEqual(record.refresh_count, 1)
        self.assertEqual(self.marker.read_text().splitlines(), ["0.167.0"])
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )

    def test_second_offer_race_stops_before_any_native_client_or_new_attempt(
        self,
    ) -> None:
        original = self.supervisor.monitor.activate
        offers = 0

        def activate(attempt: str, index: int, frame: bytes) -> None:
            nonlocal offers
            offers += 1
            self.install("0.167.0" if offers == 1 else "0.168.0")
            original(attempt, index, frame)

        with mock.patch.object(
            self.supervisor.monitor, "activate", side_effect=activate
        ):
            self.finish()
        self.assertFalse(self.marker.exists())
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM review_call").fetchone()[
                0
            ],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM review_attempt"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT error_code FROM review_attempt"
            ).fetchone()[0],
            "runtime_race_exhausted",
        )
        self.assertIsNone(self.call_records()[0].launched_identity)

    def test_stop_freezes_an_owned_launch_notice_before_containment(
        self,
    ) -> None:
        original = self.supervisor.monitor.activate
        activated = False

        def activate(attempt: str, index: int, frame: bytes) -> None:
            nonlocal activated
            original(attempt, index, frame)
            activated = True

        deadline = time.monotonic() + 5.0
        with mock.patch.object(
            self.supervisor.monitor, "activate", side_effect=activate
        ):
            while not activated and time.monotonic() < deadline:
                self.supervisor.tick(20.0)
                time.sleep(0.01)
        self.assertTrue(activated)
        notice_seen = False
        while time.monotonic() < deadline:
            with self.supervisor.monitor.lock:
                notice_seen = any(
                    slot.launched is not None
                    for slot in self.supervisor.monitor.slots.values()
                )
            if notice_seen:
                break
            time.sleep(0.01)
        self.assertTrue(notice_seen)
        self.supervisor.stop(21.0)
        record = self.call_records()[0]
        self.assertEqual(
            record.launched_identity, record.candidates[-1].digest
        )
