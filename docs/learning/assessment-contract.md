# Offline knowledge-learning assessment

The offline CLI defines strict C6 records and scores synthetic, independently
attributed labels. A separate callable Python harness exercises isolated candidate
launches with disposable test children. Neither path grants paid-work authority or
produces native proof. Passing synthetic tests establishes no measured learning
performance. The frozen comparator revisions
for later work are current Muninn `5c1a69b0edff81df80836d72a80971a91a8f9520`
and Hermes `f925b01791fe052e7d55dc975750c0bd985aff91`; neither runs here.

`python3.13 -m tools.learning_eval.run --manifest FILE --phase pilot|heldout
--output DIR` accepts only `OfflineManifest` JSON with explicit `synthetic: true`,
`assessment`, `cases_path`, `trials` and `host_results`. Paths are resolved from the
working directory. Use `.artifacts/knowledge-learning/` for private inputs/output;
that directory is ignored. Missing budget/approval fields, heldout freeze metadata,
duplicate JSON keys, unknown fields, nonfinite values and digest mismatches refuse
before output. The offline CLI always reports `runner_calls: 0`. Approval strings in a
synthetic fixture are examples, never actual owner authorization.

The existing Task 2 `Scope`, `Digest`, `UtcSeconds`, `Provider`, `EvidenceRecord`,
`Usage`, `ObservedConfig`, `ReviewPolicy` and `PilotBudget` types remain authoritative.
`Trial.usage` is a required `Usage` object whose unreported measurements are null.
No missing value becomes zero. C6 model fields retain their declared names.

`PilotBudget.conservative_call_cost_decimal` is required but nullable: explicit
null preserves an unknown per-call bound; omission refuses validation. A non-null
bound is a strict finite nonnegative decimal string in the budget's `currency`,
which is required when either this bound or `max_cost_decimal` is non-null,
including zero. The total monetary cap and remaining allowance are separate from
an explicitly approved conservative per-call bound and cannot supply its estimate.
Approval metadata must cover the exact bound through the canonical whole-policy
hash and immutable request snapshot. Changing the bound changes that identity.
Metadata presence alone proves neither actual owner approval nor trustworthy
pricing; structural validation cannot establish either. Synthetic bounds are test
values, never operational defaults or spending authorization. Actual prices and
owner budget approval remain prerequisites for later pilot work. An inaccurate
bound cannot guarantee a monetary cap; an overly conservative bound can exhaust
allowance early.

Every supplied trial must name the SHA-256 of one canonical full `ReviewPolicy`
in `Assessment.policies`. Each `comparator_config_digests` entry is the SHA-256 of
the canonical requested `ObservedConfig` JSON, including explicit null fields.
Both CLI validation and direct scoring reject an unknown/changed policy or requested
configuration before evaluating outcomes. Hashing uses the canonical JSON described
below. Observed settings remain separate: a mismatch is retained/disclosed, with
inconclusive attribution to the frozen treatment; it is never a parity claim.

The offline scoring interfaces are `load_cases(Path) -> tuple[Case, ...]`,
`validate_partition(tuple[Case, ...]) -> None`,
`score_trials(cases, trials, assessment, *, host_results=()) -> EvaluationReport`
and `write_report(report, Path) -> None`. The approved keyword-only host argument
adds supplied proof/failure rows to the three specified positional arguments.
Rows must match their frozen requirement exactly. Missing required rows are emitted
as unverified; failed rows are never dropped or remapped. Recovered indexed evidence
never substitutes for required native events or versions.

`required_host_seed()` enumerates exactly macOS ARM64, Linux x64, Windows x64 and
Windows ARM64, each with local Claude, Codex and Cursor. Event names in this
synthetic seed describe required categories, not proof that a native hook exists.
All actual versions and trust/evidence fields start null. Service labels describe
candidate assessment routes, not installed service/version claims. Actual versions,
owner-approved required resources and native evidence remain later prerequisites.
Wider architectures and Cursor resume/compaction delivery remain unverified.

The case file retains its synthetic origin, family descriptions and uncertain case
IDs. Empty eligible and forbidden key sets are the authoritative representation
of unresolved save labels. The wrapper must name exactly those case IDs, without
duplicates; contradictory or missing declarations refuse loading. These encoded
labels participate in the cases/labels digests. Such cases contribute no fabricated
save-quality counts; independent retrieval and later-task judgments remain eligible.
Known no-save cases identify the claims that must not be saved. Material claim keys
occur in the synthetic evidence excerpts; originals and provenance fields are
synthetic fixtures, not references to real provider records. Every section 6.4
family is represented. The 34 fault scenarios contain concrete synthetic
`input_state` objects and ordered `events` in their evidence, plus independent
`later_task_success_rule` JSON equality predicates for a disposable after-state.
They distinguish retained acknowledged requests, duplicate receipts, stale worker
writes, interrupted cutover activation and native delivery versus indexed recovery.
The partial-guide case has separately identified erased support (with no retained
excerpt) and live surviving support, with distinct forbidden/eligible claim keys.
These predicates label expected outcomes; the scorer consumes independently
attributed judgments and does not itself replay a runtime, filesystem or native
hook. All committed cases are pilot
examples; no real reserved heldout data is present.

`validate_partition` rejects duplicate case IDs, copied/resumed source roots assigned
to different families, families crossing partitions, reversed family chronology and
reversed source lines/selection times. It checks declared provenance; falsely
relabelled unrelated originals require independent evidence auditing.
`input_digests` hashes canonical ordered case and label projections and sorted heldout
family IDs. `config_digest` hashes all Assessment settings except itself. JSON uses
sorted keys, compact separators, UTF-8, no ASCII escaping and no nonfinite values.
Independent labels must be frozen before tuning; heldout requires `frozen_at`.
The harness can verify recorded metadata and byte identities, not historical truth
of an approval or whether a human previously viewed reserved material.

Scoring consumes separately attributed `human` or `frozen_rule` judgments with
nonempty version/provenance, frozen grader IDs and metric keys. Saving-model
identity cannot serve as labeler or grader. Model confidence and self-scores are
never read. Counts report observed independent numerators/denominators; unknown
judgments are not invented. `sample_count` is the scheduled case count times
repetitions, including interrupted, unavailable, missing and no-save attempts.
Incomplete populations remain inconclusive, even when the known subset looks good.
Zero denominator yields null value and interval. Outcome codes
`lost_acknowledged_work`, `duplicate_commits`, `unauthorized_stale_saves` and
`erasure_resurrection` override aggregates with a release blocker.

Metric names and units have explicit supported semantics. Unknown names (including
`missed_eligible_lessons`, whose canonical name is `missed_lessons`) and wrong units
raise `unsupported_metric` during criterion validation and again before direct
scoring. Frozen text alone does not grant an unknown metric independent-label
semantics. Adding a new metric requires an explicit semantics change, not an alias
in a manifest. C6 fields and explicit owner settings remain unchanged.

| Supported names | Required unit | Evidence/label dependency |
|---|---|---|
| `unsupported_saves`, `missed_lessons`, `no_save_errors`, `unnecessary_changes`, `wrong_scope_saves`, `scope_save_errors`, `correction_errors` | `proportion` | Resolved save labels and independent judgment counts |
| `retrieval_useful`, `retrieval_missed`, `retrieval_irrelevant`, `wrong_scope_uses`, `scope_use_errors`, `native_capture`, `recovered_capture` | `proportion` | Independent judgment counts; save-label uncertainty does not erase these distinct judgments |
| `learning_delay`, `foreground_elapsed`, `review_elapsed`, `queue_age` | `seconds` | Same-named reported timing fields |
| `input_tokens`, `output_tokens`, `cached_tokens` | `tokens` | Same-named nullable Usage fields |
| `cost_decimal` | `cost` | Reported nullable Usage cost |
| `peak_memory_bytes` | `bytes` | Same-named nullable resource measurement |
| `later_task` | `paired_difference` | Independently judged later-task outcomes with a success rule |

The paired `later_task` metric compares outcomes against `new_withheld`,
requiring identical, fully reported requested/observed runner, version, model and
reasoning settings. The withheld arm cannot be its own treatment. Pair repetitions
in declared trial order by case. Preserve
that order in frozen inputs. Paired-difference metrics are emitted only for
treatment comparators: the withheld arm remains in trial population/provenance and
pair completeness checks, but is not an impossible self-comparison criterion. Exact binomial intervals use equal-tailed
Clopper-Pearson inversion. Family bootstrap resamples complete families with the
explicit seed and repetition count. Threshold decisions use interval bounds; an
interval crossing the threshold is inconclusive. No numerical owner target,
confidence, seed, sample size or production policy is inferred.

The CLI writes `report.json` and `trials.json`. The latter retains per-attempt timing,
resource, requested/observed configuration, outcome and nullable usage records so
slow cases and configuration differences remain inspectable. Report metrics are
frozen named summaries, not a substitute for these distributions. Config mismatches
are disclosed separately and cannot establish an effect of memory. Synthetic CLI
reports cannot claim acceptance; hard blockers still produce `revise`.

## Synthetic callable harness

`tools.learning_eval.run.prepare_capability_trial(context, capability, prompt,
authorization) -> LaunchSpec` accepts only the concrete Claude POSIX candidate
recipe. Preparation grants no reservation or activation permit. `proof_path` names
an explicit private `Candidate` artifact (`kind: synthetic_candidate`), whose exact
bytes must match `TrialAuthorization.candidate_digest`. Requested settings,
executable bytes, generated round schema, output decoder, explicit configuration
roots, selected environment hashes and disposable cwd are freshly checked. The
selected `HOME` must equal the private disposable home; cwd and roots stay inside
that home. Only explicitly selected environment values reach the child. Candidate
artifacts contain no native coverage claims and cannot satisfy production
`prepare_launch`, which continues to require verified native proof.

`Account(path, assessment, initialize=True)` explicitly initializes a private
SQLite account in an existing private directory outside disposable homes. Reopening
requires the same explicit path and immutable assessment/account/approval/budget
identity; absent or corrupt stores refuse. New authorized policy/configuration
snapshots append without replacing historical limits. All four comparator labels,
rounds, retries and fault calls share that ledger. First reservation permanently
anchors elapsed wall time. A separate durable clock high-water mark records every
budget observation, including refusals, before the admission decision. Reopening
preserves that mark, and reversed time refuses further admission. Original and
historical account limits remain active; the stronger original/current conservative
call charge is retained. Unknown usage stays null. Refused schedules are retained
without fabricating a model call.

The account context holds the existing non-reentrant OS controller lock through
scheduling, execution and cleanup. Each SQLite transaction closes before wrapper
creation. `claim` atomically records a one-use launch claim; replay returns the
same row without another permit, and altered bindings refuse. An interrupted claim
never becomes reusable. Reopening preserves unresolved calls as unknown and invokes
the existing recovery primitive only with recorded native identities. Missing,
unsupported or conflicting identities block further execution; there is no PID-only
cleanup, stale-age takeover, refund or automatic account reset. A missing-identity
crash can require separate explicit reconciliation. Cross-machine account sharing
is unsupported.

`tools.learning_eval.execution.execute_trial(account, context, capability, prompt,
authorization, schedule)` runs one model call under that lock. It rechecks candidate
inputs after the durable claim and again after persisting positively checked worker
readiness, before spending activation once. The shared fixed wrapper creator and
`ProcessMonitor` own identity, bounded activation framing, deadlines, output limits
and cleanup. The deadline is capped by the policy and remaining original/historical
account wall allowance. Each round has a separate call ID while the supplied review
attempt remains unchanged. Registration or persistence faults retain conservative charges and
owned-child containment. The wire format preserves empty and whitespace arguments,
including the distinct empty argument after `--tools`, while retaining strict
executable/path, NUL, environment and frame validation. Missing or unreadable
preparation inputs retain a content-free `configuration_refused` scheduled row
without consuming a model-call charge; reopening and reporting preserve it.

Only `new_present` and `new_withheld` have this callable candidate execution path.
Hermes and current-Muninn execution refuse and remain scheduled/unmeasured until
separately reviewed adapters mediate every model call. Reserving once around a
multi-call comparator process is not supported. The CLI has no launch switch.

`report.with_account(report, account)` retains scheduled trial IDs and discloses
budget refusal, unknown spending and unsupported comparators. Its synthetic results
remain unverified; reliability hard blockers retain `revise` precedence. Explicit
`Trial.expected_conditions` and `observed_conditions` bind evidence availability
and selection digests, native state, cache state and approval mode. Historical
absence is unknown. Unknown or unequal conditions are visibly unmatched and cannot
support causal attribution to memory, including paired later-task comparisons.

Heldout validation requires a separately supplied immutable `frozen_assessment`
argument to `validate_manifest`, equal to the strict manifest assessment. Approval
shape, phase, criteria, policies and configuration digest are checked before reading
case material; exact cases, labels and family digests are checked afterward. The
CLI cannot supply that external binding, so heldout CLI requests refuse. Tests use
fabricated temporary heldout cases; this does not freeze or access a real heldout set.

Offline case loading/scoring and callable execution remain separate interfaces.
There is no automatic case-to-prompt, schedule or pilot controller. Actual provider
calls, native isolation trials, owner-approved numerical tolerances, reserved
heldout material, measured pilot/freeze artifacts and real comparator adapters
remain separate authorized work. No synthetic test emits a measured pilot summary,
production policy, native capability proof or activation record.

## Conditional runner capability fields

`RunnerCapability` requires `runner`, `executable`, `version`, `status`
(`verified`, `unsupported` or `unverified`), `model`, `reasoning`,
`schema_digest`, `config_digest` and `proof_path`. Requested values never stand
in for observed settings or usage. `reported_metadata` validates separately
supplied observed/usage mappings; omitted measures remain null, omitted usage
remains absent, and invalid or extra fields refuse. The separate `review_output` decoder defines the native envelope below;
`reported_metadata` remains a strict data-only normalizer. Policy `result_schema_digest` identifies the generated
`ReviewResult` schema; capability `schema_digest` identifies the generated
four-operation `RoundRequest` schema, including its final-result branch.

The conditional adapter prepares only Claude CLI data with fixed JSON/schema,
empty tools, strict empty MCP (`{"mcpServers":{}}`), no session persistence,
safe mode, disabled slash commands and explicit model/effort flags. Codex and
Cursor remain unsupported. There is no bare-mode, read-only or weaker-flags
fallback. A capability label or this repository's synthetic fixture cannot
admit a real executable.

The internal `HostProof` record requires all of these fields:

| Field | Meaning |
|---|---|
| `schema_version`, `recipe`, `provenance` | Version 1, `claude-static-posix-v1`, and controlled `native_trial` issuance; `synthetic` always refuses |
| `output_contract_digest` | Required canonical decoder/envelope/metadata/size contract identity from `review_output.output_contract_digest()`; missing, old or mismatched proof refuses |
| `requested`, `schema_digest`, `cli_argv`, `executable_sha256` | Exact native-trial runner/version/model/effort, round schema, fixed command and executable bytes |
| `cli_cwd`, `config_roots`, `environment_sha256`, `config_digest` | Literal working directory, exhaustive attested config roots, selected environment value hashes and canonical configuration identity |
| `coverage`, `disabled`, `complete` | Exact coverage of cwd/ancestors/project/plugins/managed/auth; disabled tools/MCP/hooks/plugins/session persistence; literal true completeness assertion from the trusted producer |

Proofs reside only at configured Muninn home
`review-capabilities/<sha256-of-proof-bytes>.json`. Home, proof directory and
proof have private same-user POSIX ownership. Symlink/aliased paths and unsafe
permissions refuse. The canonical ancestors above configured home are a trusted
platform boundary. Other ownership-verification platforms remain unsupported.
This assumes controlled program-owned local storage; a privileged same-user
writer can forge it. File hashes and JSON assertions cannot establish trial
truth or exhaustive source discovery. Only later authorized native trials may
issue these records; model/provider output is never a proof producer.

Each preparation freshly checks executable bytes, exact settings/flags/schema,
configuration-root inventories (including missing roots and new entries), and
all selected environment values. No unlisted environment is inherited by the
CLI. Credential values are kept only in the in-memory CLI environment, never
proof, logs or error text. Native issuance must establish an exhaustive static
source recipe for the exact host, authentication path and literal
`<home>/review-runner-cwd`. Unknown, dynamic or unobservable managed sources
cannot qualify. Cwd is itself inventoried; the native producer must additionally
cover all applicable ancestor/project/auth/managed/plugin sources.

Structural preparation limits are 1 MiB of proof, 256 MiB of streamed executable,
4,096 configuration inventory entries, 1 MiB per configuration file and 16 MiB
aggregate configuration contents. Roots must be disjoint canonical paths;
symlinks and nonregular entries refuse. These are conservative availability
limits, not measured production settings or universal I/O/time bounds. Reads
are sequential observations, not an atomic host snapshot. A final fresh check
cannot eliminate concurrent mutation; hosts where that residual breaks isolation
remain unadmitted. Native evaluation must measure refusal, latency, shared-cwd
concurrency and all emitted-file containment/cleanup.

`prepare_launch` produces fixed `LaunchSpec` data and checks local input/round/
deadline limits. It neither reserves spending nor starts processes. The internal
wrapper command uses the running Python interpreter with `-E -s -m
muninn.review_worker --deadline <monotonic-deadline> --max-input-bytes
<policy.max_input_bytes> --max-output-bytes <policy.max_output_bytes>`,
package-root cwd and an
empty environment. This allows module import without inheriting provider
credentials. The CLI receives its separately proven cwd/environment. The
supervisor must persist shared-account reservation before wrapper creation,
release writer ownership, close inherited descriptors, register and persist
native identity, repeat fresh capability checks, and only then activate the
wrapper. Output streaming limits and uncertain charges remain supervisor
obligations; counters must survive restarts. Program-created attempt material
belongs under `review-work/AttemptId` independently of the fixed CLI cwd.

Round JSON permits exactly one of search, open, evidence or final. Local schema,
bytes, round/deadline and scope/window prechecks do not grant evidence-reading
or commit authority. Controlled knowledge/evidence reads and persistence of
support associations before returning content remain the separate read
integration. No native trial, live proof, runtime activation or production
acceptance follows from these disposable-fixture checks.


## Explicit launch artifacts and native output

`load_capability(home, policy)` reads exactly
`home/review-capabilities/selected/<digest-of-complete-policy>.json` as a
complete strict `RunnerCapability`. Selection does not synthesize verified
status, scan candidates, pick a latest entry, issue proof or authorize spend.
Requested runner fields must match the policy; fresh `prepare_launch` proof
checks remain required before wrapper creation and activation. The selection
has a separate 1 MiB structural availability cap.

`load_prompt(home, policy)` reads exactly
`home/review-prompts/<policy.prompt_digest>.json`, bounded by
`policy.max_input_bytes`. The digest binds the exact artifact bytes, which
are returned unchanged after validation. The only artifact fields are
`schema_version: 1`, `assembly: "muninn-review-prompt-v1"` and nonempty trusted
`instructions`. Unknown fields or assembly, duplicate keys, malformed UTF-8,
nonfinite JSON, absence and digest mismatch refuse. No production prompt is
created by these loaders. Both loaders require the configured `data_home`,
canonical private same-user POSIX directories and regular files. Reads verify
file descriptor and path identity, ownership, permissions and observable
changes. They do not provide an atomic filesystem snapshot; the existing
trusted local storage assumption and native concurrent-mutation limits apply.

`decode_claude_output(raw, max_output_bytes)` is data-only. It requires one
UTF-8 JSON object, rejecting duplicate keys, nonfinite values, trailing
objects and text/Markdown fallbacks. Success requires `type: "result"`,
`subtype: "success"`, literal `is_error: false` and an object-valued
`structured_output`. `permission_denials` may only be absent, null or an
empty list. `deferred_tool_use` may only be absent or null. The decoder caps
the whole native stdout and the canonical structured-output bytes separately
at the same explicit positive output limit. It returns only the canonical
structured output, observed configuration and nullable usage; main still
validates the four-operation protocol with `parse_round`.

Only a well-formed singleton `modelUsage` map reports an observed model.
Missing, null, empty or multiple-model maps leave it unknown; malformed maps
refuse. Runner, version and reasoning remain null. Present usage counters
`input_tokens`, `output_tokens` and `cache_read_input_tokens` must be null or
nonnegative integers; booleans, strings and negative/fractional values refuse.
They map to input, output and cached tokens. Missing/null usage remains absent,
and missing/null counters remain unknown. Cache creation is not cache-read
usage. `total_cost_usd` and `costUSD` are estimates: actual `cost_decimal` and
`currency` remain null, preserving the conservative reservation. Extra bounded
envelope fields grant no authority. Invalid/error output executes no operation.

The proof's required output-contract digest binds these exact rules, not just
an output schema. The synthetic fixture's digest remains synthetic and cannot
qualify an executable. Actual native issuance must qualify the executable,
version, flags, schemas, decoder, configuration and success/failure outputs;
this implementation performs no native trial or publication. Wrapper framing,
aggregate stdout-plus-stderr limits, final prompt assembly, durable launch
identity and activation belong to the consumer implementation. Prompt fragments
fitting individually do not establish that the assembled prompt fits.
