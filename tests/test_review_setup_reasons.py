"""Keep native refusal causes finite and saved notice text untrusted."""

from __future__ import annotations

from functools import partial
from unittest import mock

from muninn import review_codex_policy, review_recovery, review_requests
from muninn.hook_status import learning_status
from muninn.review_runner import prepare_launch
from tests import test_review_codex_launch as launch_tests
from tests.test_cli_learning import CliLearningCase


class SetupReasonTests(launch_tests.CodexProductionLaunchTests):
    """Reject every changed native boundary with its specific safe reason."""

    def test_native_host_and_project_refusal_codes_remain_distinct(
        self,
    ) -> None:
        with (
            mock.patch(
                "muninn.review_codex_launch.host_identity",
                return_value="f" * 64,
            ),
            self.assertRaisesRegex(ValueError, "^codex_host_changed$"),
        ):
            prepare_launch(self.context, self.capability, self.prompt)
        missing = self.context.model_copy(
            update={
                "scope": self.context.scope.model_copy(
                    update={"project_id": None}
                )
            }
        )
        with self.assertRaisesRegex(ValueError, "^codex_project_required$"):
            prepare_launch(missing, self.capability, self.prompt)
        terms = self.policy.experimental_codex
        assert terms is not None
        with (
            mock.patch.object(
                review_codex_policy, "host_identity", return_value="f" * 64
            ),
            self.assertRaisesRegex(ValueError, "^codex_host_changed$"),
        ):
            review_codex_policy.check_host(terms, self.home)

    def test_legacy_notice_is_rendered_from_code_without_timer_promise(
        self,
    ) -> None:
        detail = review_recovery.explain(
            "codex_experimental_boundary", "launch"
        )
        self.assertIn("not recorded", detail["cause"])
        self.assertNotIn("in 60 seconds", detail["action"])
        rendered = learning_status(
            {
                "id": "setup",
                "code": "codex_experimental_boundary",
                "stage": "PRIVATE_STAGE",
                "cause": "PRIVATE_CAUSE",
                "action": "PRIVATE_ACTION",
            }
        )
        self.assertNotIn("PRIVATE", rendered)
        self.assertIn("not recorded", rendered)

    def test_all_original_native_setup_predicates_keep_specific_refusals(
        self,
    ) -> None:
        terms = self.policy.experimental_codex
        assert terms is not None
        cases = (
            ("missing", None, self.capability, "codex_approval_required"),
            (
                "home",
                terms.model_copy(update={"data_home": self.tmp / "different"}),
                self.capability,
                "codex_data_home_changed",
            ),
            (
                "status",
                terms,
                self.capability.model_copy(update={"status": "verified"}),
                "codex_capability_status",
            ),
        )
        for label, changed, capability, code in cases:
            context = self.context.model_copy(
                update={
                    "policy": self.policy.model_copy(
                        update={"experimental_codex": changed}
                    )
                }
            )
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(ValueError, "^" + code + "$"),
            ):
                launch_tests.launch.prepare(context, capability, self.prompt)
        for system, machine in (("Linux", "arm64"), ("Darwin", "x86_64")):
            with (
                mock.patch("platform.system", return_value=system),
                mock.patch("platform.machine", return_value=machine),
                self.assertRaisesRegex(ValueError, "^codex_platform_changed$"),
            ):
                launch_tests.launch.prepare(
                    self.context, self.capability, self.prompt
                )

    def test_saved_legacy_text_and_stage_are_not_reused_on_reads(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        review_recovery.record(
            self.conn, "setup", "codex_experimental_boundary", "launch", 1.0
        )
        self.conn.commit()
        self.conn.execute(
            "UPDATE review_issue SET cause='PRIVATE_CAUSE',"
            "action='will check in 60 seconds PRIVATE_ACTION',"
            "stage='PRIVATE_STAGE'"
        )
        before = list(self.conn.iterdump())
        shown = review_recovery.notice(self.conn)
        assert shown is not None
        self.assertEqual(shown["stage"], "not recorded")
        self.assertIn("not recorded", shown["cause"])
        self.assertNotIn("PRIVATE", str(shown))
        self.assertNotIn("in 60 seconds", str(shown))
        self.assertNotIn(
            "PRIVATE", str(review_recovery.details(self.conn, "setup"))
        )
        self.assertEqual(list(self.conn.iterdump()), before)


class PausedLearningRenderingTests(CliLearningCase):
    """Use actual safe selection before rendering paused learning notices."""

    def test_paused_retryable_and_exhausted_work_never_promises_retry(
        self,
    ) -> None:
        for code in ("deadline", "unavailable", "rejected_revision"):
            for exhausted in (False, True):
                with self.subTest(code=code, exhausted=exhausted):
                    self.doCleanups()
                    self.setUp()
                    request = self.transaction(
                        partial(
                            review_requests.fail_attempt,
                            self.conn,
                            self.context.attempt_id,
                            code,
                            20.0,
                        )
                    )
                    if exhausted:
                        attempt = self.transaction(
                            partial(
                                review_requests.claim_attempt,
                                self.conn,
                                request.id,
                                1,
                                22.0,
                            )
                        )
                        request = self.transaction(
                            partial(
                                review_requests.fail_attempt,
                                self.conn,
                                attempt.id,
                                code,
                                23.0,
                            )
                        )
                    self.assertEqual(
                        request.state, "failed" if exhausted else "waiting"
                    )
                    self.conn.execute("UPDATE review_control SET admission=0")
                    self.transaction(
                        partial(
                            review_recovery.record,
                            self.conn,
                            request.id,
                            code,
                            "review",
                            24.0,
                        )
                    )
                    self.conn.execute(
                        "UPDATE review_issue SET cause='PRIVATE_CAUSE',"
                        "action='PRIVATE_ACTION'"
                    )
                    before = list(self.conn.iterdump())
                    issue = review_recovery.startup_action(self.conn)
                    assert issue is not None
                    message = learning_status(issue)
                    self.assertIn("paused", message)
                    self.assertNotIn("will retry", message)
                    self.assertNotIn("will rebuild", message)
                    self.assertNotIn("PRIVATE", message)
                    self.assertIn(f"muninn learn show {request.id}", message)
                    self.assertEqual(list(self.conn.iterdump()), before)

    def test_open_contained_stays_quiet_and_required_codes_remain_visible(
        self,
    ) -> None:
        self.transaction(
            lambda: review_recovery.record(
                self.conn, self.context.request_id, "deadline", "review", 20.0
            )
        )
        self.assertIsNone(review_recovery.startup_action(self.conn))
        for admission in (1, 0):
            for code in ("prompt_limit", "codex_host_changed"):
                with self.subTest(admission=admission, code=code):
                    self.transaction(
                        lambda: review_recovery.clear(
                            self.conn, self.context.request_id
                        )
                    )
                    self.conn.execute(
                        "UPDATE review_control SET admission=?", (admission,)
                    )
                    self.transaction(
                        partial(
                            review_recovery.record,
                            self.conn,
                            self.context.request_id,
                            code,
                            "review",
                            21.0,
                        )
                    )
                    issue = review_recovery.startup_action(self.conn)
                    assert issue is not None
                    self.assertEqual(issue["code"], code)
                    message = learning_status(issue)
                    self.assertIn(
                        (
                            "Do not retry this request unchanged"
                            if code == "prompt_limit"
                            else "saved computer ID"
                        ),
                        message,
                    )
