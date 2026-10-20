# Ongoing Codex production learning

An explicitly recorded ongoing policy can select installed Codex learning through
`serve → Supervisor → review_call → checked commit`. It has **no aggregate call,
spend or elapsed-time ceiling** and no expiry or renewal cycle. It does not grant
verified C6 status: complete tool-source isolation, internal model request count
and actual billing remain unverified. The owner explicitly accepts these
uncertainties in the recorded policy.

## Selection and per-call controls

`ReviewPolicy.budget` accepts a distinct `OngoingBudget` with
`kind: "ongoing_production"`, account identity and actual approval metadata.
It contains no maximum count, cost or elapsed time and creates no estimated
reservation. `PilotBudget` remains finite; assessment validation rejects ongoing
production authority. The historical twenty-launch/$100-reserved pilot stays in
its separate assessment artifacts and is never imported as production usage or
quota. Unknown production usage/cost stays null in durable per-call records.

The approved selection remains bound to this Darwin arm64 host, canonical data
home, Codex 0.163.0 executable SHA256
`8813367285524ac798dd96d1b071aea30d58708c7226fd236cd275cf1cfdee31`, and requested
`gpt-6.1-sol/low`. Ordinary policies still reject Codex, including forged
`verified` labels. Only the explicit ongoing policy and an `unverified` capability
select this path.

Each attempt retains two rounds, one attempt/no automatic retry, concurrency one,
120 seconds, 32768 output bytes, 12000 context bytes and zero hook wait.
The original policy caps original-record reads at 65536 bytes. A separately
recorded v2 approval raises only that raw-read allowance to 1048576 bytes;
native Codex sessions on this Mac contained more than 160000 raw bytes even
for a short exchange. The v2 transition requires a later approval timestamp,
unchanged model and other call controls, and no learning requests or knowledge
in the installed store. It preserves the original activation time and fences
the supervisor generation. Recall remains off. Existing configured-project scope mapping governs
admission and commits; there is no Muninn-only allowlist. Global saves remain
disabled. Evidence, erasure, receipt and commit checks are unchanged.

The shared fixed recipe uses ephemeral execution, ignored user config/rules,
disabled hooks, a read-only sandbox, explicit empty cwd, explicit model/effort,
and the measured strict object-envelope decoder. A tool/action can happen before
its event is detected. The code contains it when observed and durably disables
further admission on terminal errors, uncertain output, validation failure,
configuration drift or an interrupted attempt. Existing disabled admission is
preserved through reinstall; routine startup does not re-enable it.

## Reproducible artifact preparation

`ongoing-codex-policy.proposed.json` is an inert example: false authorization
intentionally fails validation. After actual authorization and review, the
following helper writes complete prompt/schema/binding/selected-capability and
policy artifacts. Supply the real recorded approval timestamp and native pinned
binary path; a JavaScript wrapper fails the executable hash check.

```python
from datetime import datetime
from pathlib import Path
from tools.prepare_codex_policy import prepare

policy_path = prepare(
    Path("/Users/harrison/.local/share/muninn"),
    Path("/absolute/path/to/pinned/native/codex"),
    Path("/Users/harrison/.codex"),
    approved_by="Harrison: recorded ongoing-production authorization",
    approved_at=datetime.fromisoformat("RECORDED_APPROVAL_UTC").timestamp(),
    revision=2,
)
print(policy_path)
```

The helper verifies current host/executable and private paths, preserves existing
identical artifacts and refuses conflicting bytes. It writes no provider config,
reads no credential contents, invokes no model and does not install or activate.
The installer records raw/canonical policy bytes and digests; this is recorded
approval, not a cryptographic signature inferred from a passing test.

From the exact reviewed clean release:

```sh
bin/muninn-install --repo /absolute/path/to/clean/release \
  --sha FULL_REVIEWED_COMMIT_SHA --upgrade --dry-run \
  --review-policy /Users/harrison/.local/share/muninn/review-policy-codex-ongoing.json
```

Native hooks/service/upgrade acceptance and the final combined gate remain
separate requirements. Preserve pilot and installation evidence. No historical
trial expiry, carry-in or production allowance file participates in this policy.
