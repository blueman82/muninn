# Bounded Learning Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (selected) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The owner approved implementation and delegated execution. Obtain independent plan review, then execute without another owner approval request.

**Goal:** Correct eligible model results within existing rounds and distinguish contained failures from startup actions.

**Architecture:** Keep correction state in `Round` and durable correction markers in `review_call.outcome`. Defer only typed candidate failures after original verification. Add a startup selector without changing doctor selection.

**Tech Stack:** Python 3.13, SQLite, existing pinned Pydantic closure, stdlib unittest.

**Spec:** [Approved lifecycle design](../../memory-lifecycle-design.md), sections A–C, HyperSkill reference, acceptance 1–9, 11, 20; [ADR 0017 amendment](../../adr/0017-learning-scope-and-recovery.md#proposed-amendment-bounded-correction-and-action-routing).

## Global Constraints

- Owner approval recorded 2026-10-29: “all above is approved what questions would you ask”. This is project authority, never runtime proof.
- Two rounds, three total attempts, four workers. Keep 5-second and 30-second transient backoffs and 60-second setup rechecks.
- Preserve deadlines, permit ownership, charges, original-read budgets, scope, policy binding, source checks, tombstones, child containment, pauses and generation fences.
- No new table, column, provider, daemon, backend, notification ledger, CLI default or extraction-prompt policy.
- No model call while holding a writer lock. `store.writer_lock` is not reentrant; separate opens conflict in one process.
- `commit_review` expects a held writer lock and no active transaction. It owns `BEGIN IMMEDIATE`; `database(home, transaction=False)` is the supervisor root.
- Python files: at most 400 nonblank lines; functions: at most 100. Strict annotations and Google docstrings; no ignored checker errors.
- Runtime third-party imports remain the hashed Pydantic closure. Tests use disposable originals and temporary `MUNINN_HOME`; never real provider files.
- JSON stdout, compact hook JSON, hook exit zero. Diagnostics contain finite codes, counts and IDs, never model/transcript content.
- Commands below run from this worktree using its complete pinned `.venv`; create it from the two hashed requirements files if missing.
- Each task gets a fresh reviewer before the next dependent task. Commit only its reviewed files after required gates pass. Integration requires the final-tree full gate.

## Review Focus

| Risk input | Required behavior | Owning test |
| --- | --- | --- |
| Valid JSON with unknown `op` versus malformed native output | Only the first can use format correction | Task 1 `test_only_schema_failures_are_correctable` |
| A rejected preference returns as a fact | Refuse the second result; no authority escape | Task 2 `test_reduction_rejects_every_semantic_edit` |
| Rejection occurs before a valid second result, but source changes meanwhile | Original verification rejects the second result | Task 3 `test_changed_original_after_deferred_failure_is_terminal` |
| Two calls were read then final, or crash happened after a correction marker | Report only what durable rows establish | Task 4 `test_correction_history_requires_marker_and_launch` |
| New routine failure follows an old active security/setup issue | Keep the action visible; doctor retains routine detail | Task 5 `test_startup_keeps_older_action_required_issue` |

## Source and file map

Inspected on branch `feat/learning-lifecycle`, planning head `63e9a62ab4a546456ac59f6a2d324b842ac1027e`. Base synchronization remains the orchestrator's job. Recheck files before implementation; do not overwrite concurrent edits.

| File | Responsibility/change |
| --- | --- |
| `muninn/review_rounds.py` | Strict JSON/schema boundary and typed format error |
| New `muninn/review_correction.py` | Fixed feedback models and exact ordered-subset validation |
| `muninn/review_round_state.py` | Optional in-memory correction state |
| `muninn/review_commit.py` | Default-off candidate deferral; preserve its transaction boundary |
| New `muninn/review_candidates.py` | Extract batch/target/support validation if needed to keep commit under 400 lines; typed proposal rejection lives here |
| `muninn/review_supervisor_result.py`, `review_supervisor.py` | Phase-specific handling and one remaining-round continuation |
| `muninn/review_prompt.py` | Bounded complete fixed correction feedback |
| `muninn/review_supervisor_store.py` | Fenced completed-call correction marker |
| New `muninn/review_diagnostics.py` | Finite phase vocabulary and durable correction-history projection |
| `muninn/review_recovery.py`, `hook.py` | Startup-only action selection |
| `muninn/cli_learn.py`, `cli_learn_explain.py`, `review_models.py` | Explicit correction status and phase output |
| `tests/test_learning_eval_procedure_conditions.py`, `tests/fixtures/learning/procedure-conditions.json` | Five synthetic cases through existing pilot framework |
| `docs/REFERENCE.md`, both `.claude/skills/muninn-learn/SKILL.md` and `.codex/skills/muninn-learn/SKILL.md` | Changed answer fields and failure guidance |

Keep extraction prompts and native event schemas unchanged. The new feedback is program-owned prompt data; it never contains rejected model text.

### Task 1: Separate schema failure from terminal decoding and safety refusal

**Files:** Modify `muninn/review_rounds.py`; create `muninn/review_correction.py`; test `tests/test_review_correction.py`, `tests/test_review_rounds.py`.

**Interfaces:** Define `RoundFormatError(ValueError)` with fixed code `model_format`. Keep `parse_round(raw: bytes, context: ReviewContext) -> RoundRequest`. Define `CorrectionFeedback(StrictModel)` with `kind: Literal['format','reduction']`, `code: Literal['model_format','candidate_scope','owner_support','duplicate_target','target_overlap']`, `collection: Literal['changes','unresolved'] | None`, and zero-based `index: int | None`. Require collection/index together for reduction and neither for format.

- [ ] Write `test_only_schema_failures_are_correctable`, table-driven over unknown op, missing required field, wrong strict type, extra field, duplicate JSON keys, NaN, malformed UTF-8/JSON, oversize, expired deadline and widened read scope. For the first four assert `RoundFormatError`; for all others assert a terminal `ValueError` that is not `RoundFormatError`. Use `tests.test_review_rounds.context` and its patched monotonic clock.
- [ ] Run `.venv/bin/python3.13 -m unittest tests.test_review_correction tests.test_review_rounds`; confirm the new behavior fails for the intended boundary.
- [ ] Raise the typed error only around `_ADAPTER.validate_json`, after strict decoding succeeds. Keep `_scope`, `check_round`, and native decoding outside that catch. Feedback uses only the finite model fields; validate indices are nonnegative.
- [ ] Rerun the same command; require `OK`. Assert unknown op is never read as trusted dispatch data, and no original evidence read occurs during parsing.
- [ ] Obtain fresh review, run the required commit gate, then commit only this task's files: `feat: distinguish correctable review format failures`.

### Task 2: Defer only the eligible typed candidate failures

**Files:** Modify `muninn/review_commit.py`; create `muninn/review_candidates.py` and extend `review_correction.py`; test `tests/test_review_candidate_reduction.py`, existing `tests/test_review_commit*.py`.

**Interfaces:** `ProposalRejectedError(CommitRejectedError)` exposes `feedback: CorrectionFeedback` and its unchanged terminal failure code. To avoid an import cycle, move `CommitRejectedError` and candidate validators into `review_candidates.py`; re-export `CommitRejectedError` from `review_commit.py` for current consumers. Extend `commit_review(conn: sqlite3.Connection, context: CommitContext, result: ReviewResult, *, defer_candidate_failure: bool = False) -> Receipt`. Add `require_reduction(original: ReviewResult, corrected: ReviewResult) -> None`. Use fixed empty reason `No supported candidates remain after correction.`

- [ ] Add `CommitCase` based tests: `test_default_failure_still_ends_attempt`, `test_deferred_failure_rolls_back_without_ending_attempt`, `test_original_checks_precede_candidate_deferral`, and `test_reduction_rejects_every_semantic_edit`. Assert zero knowledge/receipt/unresolved writes on deferral, `ended_at is None`, and original evidence validation ran once. Existing callers retain terminal failure bookkeeping.
- [ ] Run `.venv/bin/python3.13 -m unittest tests.test_review_candidate_reduction tests.test_review_commit tests.test_review_commit_admission tests.test_review_commit_budget`; record the expected failing assertions.
- [ ] Raise typed errors for only candidate scope, missing owner support, repeated replacement target, and replacement/unresolved target overlap. Target freshness/scope lookup failures, unknown refs, repeated affected targets inside one unresolved item, source changes and fences stay ordinary terminal errors. Traverse all original-evidence checks before `_batch`; defer only the typed exception, only when opted in. Roll back before returning it; skip `_failure` for that one case.
- [ ] Before deferring, complete all terminal candidate checks across the entire batch. Retain the first eligible feedback while continuing reference and target validation; any terminal unknown ref, invalid affected target or stale target wins, regardless of candidate order. Do not repeat original reads. Add mixed eligible/terminal cases in both orders and assert zero correction calls. Then implement reduction as an ordered subsequence of the original typed canonical changes and unresolved items, with at least one item removed across the two lists. Preserve duplicate multiplicity, order and exact fields; introduce nothing. The supervisor retains typed canonical input, not an arbitrary raw response. Reject changed reason/expiry/supports/targets/dependencies/kinds, a reorder, equal unchanged batch, or new unresolved item. Empty result must use the fixed reason; nonempty result retains normal no-change validation. Rerun tests and require `OK`.
- [ ] Fresh reviewer checks the typed allowlist against all four branches and terminal counterexamples; run commit gate; commit `feat: defer bounded candidate reduction failures`.

### Task 3: Continue one correction round under the same permit

**Files:** Modify `review_correction.py`, `review_round_state.py`, `review_supervisor_result.py`, `review_supervisor.py`, `review_prompt.py`, `review_supervisor_store.py`; test `tests/test_review_supervisor_correction.py`.

**Interfaces:** Define `CorrectionState` in `review_correction.py` with `feedback: CorrectionFeedback` and `original: ReviewResult | None`; `Round.correction: CorrectionState | None = None`. Extend `review_prompt.assemble(..., *, correction: CorrectionFeedback | None = None) -> bytes`. Add `mark_correction(conn: sqlite3.Connection, context: ReviewContext, kind: Literal['format','reduction']) -> None` to supervisor store; caller holds one transaction/root. Extend the result failure callback to `Callable[[ReviewContext, str, float, str], None]` so every failure site specifies its observed phase.

- [ ] Build on `SupervisorCase` and its disposable local runner. Add `test_format_then_final_uses_same_attempt`, `test_reduction_then_final_has_no_partial_write`, `test_corrective_read_and_second_correction_are_refused`, `test_changed_original_after_deferred_failure_is_terminal`, and `test_correction_never_exceeds_concurrency_or_budget`.
- [ ] Run `.venv/bin/python3.13 -m unittest tests.test_review_supervisor_correction tests.test_review_supervisor tests.test_review_supervisor_recovery`; verify red before implementation.
- [ ] On eligible first result, require a remaining round and no prior correction. Refresh request/input union, then mark the completed call under current attempt/policy/generation fences with `correction_format_requested` or `correction_reduction_requested`. Keep state `completed`, ended time, usage and reservation unchanged. Only after the marker commits, increment `round_index`, reset launch state, and retain the same attempt, deadline and permit. Before launch refresh again and use existing freshness, read-budget, process and launch checks.
- [ ] Correction prompt adds complete bounded fixed feedback as data; never store the invalid raw result in history. Reduction's temporary original stays local. If the full prompt exceeds its existing cap, fail safely. In correction state require `FinalRequest`, enforce `require_reduction` when applicable, and commit through the existing writer root. Do not call `execute_read` or a lock-owning helper inside that root.
- [ ] Run the same tests and require `OK`. Assert request ID, attempt ID, deadline, scope, two-round allowance, 3-attempt maximum, 4-worker maximum, charge totals and backoffs are unchanged. Test first-round controlled read leaves no third call; native tool/output/runner failures cause no correction; a missing or noncompleted marker target fences continuation. Fresh review and commit gate precede `feat: continue safe review corrections within existing rounds`.

### Task 4: Report observed phases and durable correction history

**Files:** Create `review_diagnostics.py`; modify `review_candidates.py`, `review_supervisor_result.py`, `review_commit.py`, `review_models.py`, `cli_learn.py`, `cli_learn_explain.py`; tests `tests/test_review_correction_diagnostics.py`, `tests/test_cli_learning_explain.py`.

**Interfaces:** `FailurePhase = Literal['native output','model format','candidate validation','controlled read','original evidence','commit']`. Preserve existing setup/launch phase labels outside result handling. Add `correction_status(conn: sqlite3.Connection, request_id: RequestId) -> Literal['requested','attempted','succeeded','exhausted','unknown']`; add the same string field to `ReviewDetail` and explanation output. `CommitRejectedError` carries a finite `phase` populated at the original-evidence and candidate boundaries; do not derive phase from text.

- [ ] Add `test_correction_history_requires_marker_and_launch`: correlate the marker at round N only with round N+1 of the same attempt. Marker alone is requested; marker plus launched successor is attempted; completed successor plus that same attempt's receipt is succeeded; marker plus completed successor plus that attempt's terminal failure is exhausted. Unmarked read/final calls or insufficient history are unknown. Add `test_later_attempt_receipt_does_not_prove_correction` and mismatched-round counterexamples. Request summary selects the latest attempt with a marker by `(started_at,id)` and reports that attempt's status; no marker means unknown. Add `test_diagnostic_text_never_contains_rejected_payload` using a secret-like sentinel in malformed model and source text.
- [ ] Run `.venv/bin/python3.13 -m unittest tests.test_review_correction_diagnostics tests.test_cli_learning_explain tests.test_learning_failure_messages`; confirm new field/phase assertions fail.
- [ ] Track phase at the actual operation boundary. Keep finite cause codes, including specific evidence limits. Historical missing phase remains `not recorded`. Do not infer correction from call count or reconstruct feedback from stored model output. `clear_issue` can remove a success notice; durable call markers still prove correction. Keep field presentation separate from automatic logs.
- [ ] Rerun tests; require `OK`. Inject a fence failure between completed call and marker update and assert no marker. Verify markers never rewrite native tool events, unknown process state, usage, accounting, or terminal call state.
- [ ] Fresh review, commit gate, commit `feat: expose bounded correction evidence and failure phases`.

### Task 5: Select startup owner actions without hiding doctor failures

**Files:** Modify `review_recovery.py`, `hook.py`, `docs/REFERENCE.md`, both `muninn-learn/SKILL.md` mirrors; test `tests/test_learning_startup_actions.py`, `tests/test_learning_notice_dismissal.py`, `tests/test_hook_learning.py`.

**Interfaces:** `startup_action(conn: sqlite3.Connection) -> dict[str, str] | None`. Keep `notice(conn)` and `health_checks(conn)` unchanged. Use a finite contained-code allowlist and fail unknown codes toward visible owner action. Scan eligible saved issues in setup-first/newest order; do not filter only the newest row and miss an older action.

- [ ] Add `test_startup_keeps_older_action_required_issue`, `test_contained_failure_remains_in_doctor_and_history`, and a table for sign-in, binding, native tool event, uncertain process, policy/approval, pause and unknown codes. Assert startup remains quiet for contained proposal/correction/exhausted-retry/read-limit failures only while admission is safe. Doctor still returns `review_attention` for its current issue.
- [ ] Run `.venv/bin/python3.13 -m unittest tests.test_learning_startup_actions tests.test_learning_notice_dismissal tests.test_hook_learning tests.test_cli_learning_explain`; confirm expected red.
- [ ] Switch only SessionStart to `startup_action`; retain explicit dismissal semantics and inspection. Use `review_policy_status.policy_status(conn)` for saved admission and validated policy/digest state, never stale message wording, to preserve a pause notice. Test closed admission with no saved issue and stale saved action text. Explain deterministic source-read limits as maintainer work with no unchanged rerun instruction. Document all new fields and mirror the skill text together.
- [ ] Rerun tests; require `OK`. Assert repeated startup does not mutate/delete issues, questions never enter startup output, and hooks still emit compact JSON and exit zero.
- [ ] Fresh review, commit gate, commit `feat: show only required learning actions at startup`.

### Task 6: Add the five procedural cases and qualify the unit

**Files:** Create `tests/fixtures/learning/procedure-conditions.json`, `tests/test_learning_eval_procedure_conditions.py`; modify `docs/REFERENCE.md` for the test coverage description; reuse `PilotFixture`, `Case`, `run_pilot`, `input_digests`, `config_digest`, `ExposureRecipe` from existing evaluation modules.

**Interfaces:** The new fixture uses the existing `cases`/`sources` material contract. No runtime API, evaluation backend or model policy changes. Synthetic results are contract coverage, not measured model benefit.

- [ ] Add five cases with independent expected labels: `condition_success` (observed success under A), `condition_paraphrase` (different wording, same A), `condition_absent` (nearby task without A), `failure_unknown` (no established cause), `recalled_repeat` (repeated advice without independent support). Use one pilot-only procedural family with chronological evolution; keep the held-out family digest separate and never include held-out cases in pilot material.
- [ ] Run `.venv/bin/python3.13 -m unittest tests.test_learning_eval_procedure_conditions tests.test_learning_eval_paraphrase tests.test_learning_eval_guards`; verify missing-family assertions fail.
- [ ] Populate synthetic physical source records and frozen digests before running. Assertions: applicability retains A; paraphrase can retrieve the applicable guide; absent A is not labeled supported generalization; unknown failure supplies no proved pitfall; repeated recall supplies no new independent support ID. Compare present/withheld recipes with isolated stores and distinguish frozen retrieval from evolution. Labels never enter runner prompts. Reuse the current fake-runner pattern to exercise real capture, evidence, commit and retrieval.
- [ ] Run the focused command, then `.venv/bin/python3.13 -m tools.check --full` after the final edit. Require `OK` from unittest and exit zero from the gate; retain the gate's actual final summary verbatim. Have a fresh whole-unit reviewer examine safety boundaries, tests and docs.
- [ ] Commit reviewed files after the gate. The orchestrator handles publish/merge/delivery under existing owner authority. Keep local tests separate from later live upgrade, doctor, preview and native host checks.

## Self-review and acceptance map

| Spec area | Tasks | Coverage/limit |
| --- | --- | --- |
| Purpose, existing lifecycle, confirmed gaps 1–3; A | 1–3 | Eligible schema/reduction only; original checks and terminal refusals remain |
| B and C | 4–5 | Startup distinct from doctor; finite phase and durable correction proof |
| D, unresolved read and decisions | Other delivery plan | No claim of conversational completion from this unit |
| Hermes patterns and rejected options | 1–5 constraints | No arbitrary retry, capacity model, inbox, provider, new authority or lock nesting |
| HyperSkill reference and limits | 6 | Five cases, independent support and labels; no runtime-quality claim |
| Compatibility/delivery | All | ADR approved, CLI mirrors, no new repair schema, independent unit gate |
| Acceptance 1–6 | 1–3 | Formatting, budgets, strict subset, no-change, terminal evidence/target/security checks |
| Acceptance 7–9 | 4–5 | Doctor/history retained; actionable startup and truthful correction status |
| Acceptance 11, 20 | 6 | Existing evaluation framework and final pinned gate |

Self-review: interfaces name their defining task; no runtime edits are included in this plan-writing change. The five review-focus cases each have an owning test. Independent plan review must confirm the callback/phase extraction stays within file/function limits and does not change native output contracts.
