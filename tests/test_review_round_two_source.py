"""Drive round 2 after a reduction request with a changed source."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from muninn import review_round_launch, review_supervisor_result
from muninn.review_models import ReviewContext
from muninn.review_round_state import Round
from tests.ingest_support import line
from tests.test_review_managed_supervisor import ManagedSupervisorTests
from tests.test_review_source_cause import SECRET_MARKER, _validation_error

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
_BODIES = (
    "{'op':'final','result':{'schema_version':1,'changes':["
    + _GOOD
    + ","
    + _BAD
    + "],'unresolved':[],'no_change_reason':None}}",
    "{'op':'final','result':{'schema_version':1,'changes':["
    + _GOOD
    + "],'unresolved':[],'no_change_reason':None}}",
)


class RoundTwoSourceTests(ManagedSupervisorTests):
    """Reuse the managed fixture; only the new tests below matter."""

    def _stored(self) -> tuple[str, str]:
        """Return the attempt error code and the setup issue code."""
        attempt = self.conn.execute(
            "SELECT error_code FROM review_attempt"
        ).fetchone()
        issue = self.conn.execute("SELECT code FROM review_issue").fetchone()
        return (
            (attempt[0] or "") if attempt else "",
            (issue[0] or "") if issue else "",
        )

    def _between_rounds(self, change: object) -> None:
        """Run a change once round 1 has been replaced by round 2."""
        original = review_supervisor_result._advance
        self.install("0.166.0", bodies=_BODIES)

        def advance(pending: Round, context: ReviewContext) -> None:
            original(pending, context)
            if callable(change):
                change()

        with mock.patch(
            "muninn.review_supervisor_result._advance", side_effect=advance
        ):
            self.finish()

    def test_live_transcript_growth_does_not_fail_round_two(self) -> None:
        def grow() -> None:
            with self.path.open("ab") as handle:
                handle.write(line(self.records[-1]))
            self.run_ingest()

        self._between_rounds(grow)
        state = self.conn.execute(
            "SELECT state FROM review_request WHERE id=?", (self.request.id,)
        ).fetchone()[0]
        self.assertEqual(state, "completed")
        self.assertEqual(self._stored(), ("", ""))

    def test_source_loss_records_its_own_cause(self) -> None:
        self._between_rounds(
            lambda: self.conn.execute("UPDATE source SET status='missing'")
        )
        self.assertEqual(
            self._stored(), ("source_unavailable", "source_unavailable")
        )

    def _leak_check(self, error: ValidationError | OSError) -> None:
        """Raise the error in the launch path and look for the marker."""
        with mock.patch.object(
            review_round_launch, "initialize", side_effect=error
        ):
            self.finish()
        self.assertEqual(self._stored()[0], "config")
        self.assertNotIn(SECRET_MARKER, "\n".join(self.conn.iterdump()))
        for path in Path(self.home).rglob("*"):
            if path.is_file() and os.access(path, os.R_OK):
                self.assertNotIn(SECRET_MARKER.encode(), path.read_bytes())

    def test_validation_error_in_launch_leaves_no_secret(self) -> None:
        self._leak_check(_validation_error())

    def test_os_error_in_launch_leaves_no_secret(self) -> None:
        self._leak_check(OSError(SECRET_MARKER))
