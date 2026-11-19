# 0024: Leave an unfinished start, and explain each installer failure

## Status

Proposed, 2026-11-19. The owner must accept or change the take-over rule
below.

## Context

An install with a first review policy saves an activation record at phase
`intent`. Each later install tries to finish that record first. The later
install must use the same repository, the same commit and the same
provider files. When one of them changes, the install stops with
`legacy_candidate_changed` or `review_activation_configuration_changed`.
No command let the person leave that start, so no new install or upgrade
was possible.

The installer also printed only a code, for example
`FAILED: legacy_candidate_changed`. The code did not tell the person what
happened or what to do.

## Decision

### Plain failure text

- After each `FAILED: <detail>` line, `install_main` prints a fixed
  explanation: `What happened:`, `Why:`, `What to do:` and numbered steps.
- The first line stays `FAILED: <detail>`, because tests and scripts read
  it.
- The text of the part before the first `:` of the detail selects the
  explanation from a fixed table in `install/failure_text.py` and
  `install/failure_text_codes.py`. The explanation holds no path, no file
  content and no exception text (ADR 0004).
- A code that is not in the table gets one default text. It says that
  Muninn has no explanation for the code, and it tells the person to
  collect `bin/muninn-install --check` and `muninn doctor --pretty`.

### Leave an unfinished start

- `bin/muninn-install --leave-unfinished [--check]` acts only on one
  first-policy activation record of an upgrade start that
  `install.review_policy.pending` finds, on macOS or Linux.
- A fresh start record, a legacy record (ADR 0016), a host renewal record,
  a Codex interface record or Windows gets `leave_record_unsupported`.
  After a fresh start, neither `--fresh` nor `--upgrade` can continue.
- Take-over rule: a link or provider entry that the start wrote, and that a
  later upgrade takes over as its own before-value, is not a change that
  must be undone. This is the same rule as `install.finish.supersede`.
- The proof refuses, and changes nothing, when:
  - the activation phase is not `intent`, `policy_status` is `active`,
    the store has a committed policy or admission, or Muninn cannot read
    the store (`leave_policy_active`);
  - a recorded link has a value that is not its before-value and not the
    value that `pin` of the start writes, or the release folder of the
    start is missing (`leave_links_changed`);
  - a provider file exists and Muninn cannot read or parse it
    (`leave_provider_unreadable`).
- Before the question, Muninn shows the time stamp and the short commit of
  the start and the plan. It shows no path.
- `--check` shows the plan and a `DRY-RUN:` line, asks nothing and writes
  nothing.
- The answer must be `y` or `yes` in any case. Another answer, end of
  input or no terminal gives `leave_confirmation_required`.
- After the yes, under the installer operation lock, Muninn reads the
  record again (`leave_record_changed` when it changed) and does the proof
  again.
- Then Muninn adds the key `superseded_by` with the value
  `left_unfinished` to the record. It keeps all other keys and removes no
  file. `review_policy.pending` then skips the record.

## Consequences

- The next `bin/muninn-install` does a normal upgrade. It records the
  provider files again, writes its own hook entries and takes over the
  links.
- Learning stays as it was before the leave. A new install asks for
  `--enable-learning` again when learning was off
  (ADR 0017 does not change).
- The flag adds and removes no release (ADR 0001 does not change).
- `--fresh` and `--upgrade` stay the only install modes (ADR 0002).

## Learning on before the start (2026-11-19, proposed)

The first proof refused each store with a policy or admission. A person
who had learning on, or whose start committed its policy, could not
install or upgrade. This part changes the `leave_policy_active` rule of
the proof and the consequence "Learning stays off". The owner must accept
or change the case B rule below.

- Method, read-only and with no model call: Muninn compares the
  `review_control` row of the store with the control snapshot of the
  record (`policy_activation.intent.control`).
  - `no_policy`: admission 0, and `policy_json`, `policy_digest` and
    `activated_at` are NULL (the first case of this ADR).
  - `own_policy` (case B): `policy_json`, `policy_digest` and
    `activated_at` are each equal to the values of the snapshot.
  - `earlier_policy` (case A): not `own_policy`; `policy_json` is a
    valid policy, `policy_digest` is its digest, and `activated_at` is a
    finite number less than `activated_at` of the snapshot.
  - Each other row agrees with no case and gives `leave_policy_active`.
- A record at phase `intent` comes only from the first bootstrap, which
  requires a store with no policy. Thus a store with a policy and a record
  at phase `intent` is case B, unless something outside the start changed
  the store (for example a restored or copied store). Case B is the likely
  case.
- Case B: Muninn marks the start as left with the same mark, after the
  same question. It keeps the committed policy, admission, generation and
  every other part of the store. Learning stays on. A refusal keeps the
  person stuck, and a mark `complete` says that steps ran that did not
  run. Other choices: mark the start as complete, or refuse.
- Case A and case B keep admission as it is (on stays on, paused stays
  paused). Admission and `generation` are not part of the comparison.
- The proof under the lock must find the same case as the first proof.
  A different case gives `leave_record_changed`.
- A record at a phase that is not `intent`, or with `policy_status`
  `active`, still refuses with `leave_policy_active`.
- Learning scope in case B: the start stopped after its policy commit and
  before it saved the learning scope, and the record does not hold the
  scope choice. Muninn cannot recover it and does not invent one. The
  next `bin/muninn-install` asks the scope question again, or takes
  `--learning-scope now|all`. With no terminal and no flag, it stops with
  `learning_scope_required` and changes nothing. In case A the saved
  scope stays, and the upgrade keeps it.
- No case compares `intent.store` (the device and inode of the store
  file). A restored or copied store has a new inode with the same row.
  The link, release and provider proofs stay.
