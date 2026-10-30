"""Independent JSON phase fixtures for strict renewal receipt tests."""

from __future__ import annotations

from install.host_renewal_models import HostApproval
from install.host_renewal_preview import capture_bindings, preview
from install.record import Record
from muninn import review_codex_launch
from tests.host_renewal_support import HostFixture


class ReceiptFixture(HostFixture):
    """Build each required phase from explicit predecessor fixture facts."""

    def initial(self) -> Record:
        """Build the actual approved initial envelope in JSON mode."""
        plan = preview(self.ctx, self.repo, self.sha)
        approval = HostApproval(
            plan_digest=plan.digest,
            approved_at=10.0,
            authority="installer_prompt",
            confirmed=True,
        )
        return {
            "version": 1,
            "phase": "approved_intent",
            "operation": self.ctx.ts,
            "home": str(self.home),
            "repo": str(self.repo),
            "sha": self.sha,
            "plan": plan.model_dump(mode="json"),
            "approval": approval.model_dump(mode="json"),
            "bindings": capture_bindings(self.ctx, plan).model_dump(
                mode="json"
            ),
            "setup_notice": None,
            "prior_summary": None,
            "recognized_summary": None,
            "operation_identity": [1, 2],
            "held_operation_lock": {
                "stamp": [1, 2, 33152, 0, 1, 1],
                "sha256": "a" * 64,
            },
            "containment_started_at": 10.0,
        }

    def phase(self, phase: str) -> Record:
        """Construct the exact required predecessor facts independently."""
        value = self.initial()
        value["phase"] = phase
        if phase == "approved_intent":
            return value
        control = value["plan"]["state"]["control"]
        contained_control = control | {"generation": control["generation"] + 1}
        value.update(
            contained=value["plan"]["state"] | {"control": contained_control},
            archive={"stamp": [1] * 6, "sha256": "a" * 64},
            archive_path=str(self.ctx.rdir / "store-archive.sqlite"),
            archive_format=2,
            archive_status="complete",
            owner_control=control,
            containment_finished_at=11.0,
        )
        if phase == "contained_archived":
            return value
        candidate = value["plan"]["candidate"]
        value.update(
            predecessor=contained_control,
            successor=contained_control
            | {
                "generation": contained_control["generation"] + 1,
                "policy_json": candidate["canonical"],
                "policy_digest": candidate["digest"],
            },
            ready={
                "runtime": {"directories": {}, "files": {}},
                "files": {},
                "selection": {"links": {}, "cache": None},
            },
        )
        if phase == "upgrade_verified":
            value["verification"] = [
                {"check": name, "ok": True}
                for name in (
                    "doctor",
                    "heartbeat_fresh",
                    "hook_runs",
                    "new_pid_alive",
                    "runtime_bootstrap",
                )
            ]
        return value


def artifact_inputs(
    fixture: HostFixture,
) -> tuple[review_codex_launch.ExperimentalBinding, bytes]:
    """Read the retained fixture recipe with the candidate's host identity."""
    proof = next((fixture.ctx.data / "review-capabilities").glob("*.json"))
    binding = review_codex_launch.ExperimentalBinding.model_validate_json(
        proof.read_bytes()
    )
    binding = binding.model_copy(
        update={"host_identity": fixture.current_host}
    )
    prompt = (
        fixture.ctx.data
        / "review-prompts"
        / (fixture.old.prompt_digest + ".json")
    )
    return binding, prompt.read_bytes()
