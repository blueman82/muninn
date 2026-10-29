# Learning failure inspection and evidence checks

The learning review accepts a batch only after independent original-source
verification and owner-authority checks. A rejected batch changes no knowledge.
This document records the implementation boundaries for issue #75.

## Owner authority

The source must be an active primary session and the selected field must be a
permitted user-message field. User role alone does not establish owner authority.
The selected span must express affirmative direct owner intent or instruction.
Direct speech elsewhere cannot give authority to an ambiguous selected span.
The selection must retain the complete enclosing instruction or sentence,
including its negation, contextual scope, and restriction at either end. It may
trim outer whitespace and terminal punctuation. Removing `Never` from
`Never use Python.` cannot authorize `use Python.` Newlines can continue an
instruction's operand; a continuation does not become independent owner speech.

Ordinary contextual wording can qualify without a mandatory opening word.
For example, `For this project, I prefer Python.` can express direct intent.
The optional leading adjunct has a closed grammar: `for`, `in`, `on`, `within`,
or `regarding`, followed by `this` or `the current`, followed by `project`,
`repository`, `repo`, or `task`, and a comma. Only spaces and tabs separate
these words. Other contexts remain untrusted, including named or possessive
attributions, narrative framing, and examples. `In Alice's words, use Rust.`
and `For example, I prefer Rust.` cannot become direct owner instructions by
removing their prefixes.
A single backticked identifier or path is treated as an opaque code operand.
Quoted prose, fenced content, pasted or reported instructions, provider injection
markers, assistant or tool content, and harness metadata cannot acquire authority
from surrounding direct speech. Questions, conditionals, hedges, and ambiguous
wording fail closed.
Reporting predicates are checked without assuming the speaker's name. Thus
`I prefer Rust, Alice said.` and `Use Rust, the README says.` remain reported
material. The adjective in `Use reported errors.` does not identify a speaker.
After an optional leading context phrase, a further comma introduces an
unsupported extra clause and makes the sentence untrusted. This conservative
boundary also covers unfamiliar attribution verbs without guessing names.

This bounded syntax classifier is not a universal language or provenance detector.
An unmarked copy of direct prose cannot be distinguished from typed prose using
the transcript alone. Unsupported wording remains untrusted. Preferences,
decisions, global changes, and replacements retain their existing requirements
for `direct_owner` and `owner_statement` evidence. No owner approval workflow or
pinned policy decision changes.

## Source-reading allowance

One `validate_evidence` operation reuses freshly read original bytes by complete
physical locator. JSONL keys include path, offset, and byte length. Cursor keys
include database, conversation, and record key. Each distinct physical read is
charged. Each selection still receives its own source, state, scope, projection,
selector, hash, excerpt, tombstone, and provenance checks.

Accumulated durable evidence text has a separate UTF-8 bound. Excerpts,
selectors, locators, scope, identity, hash, and version strings consume it.
Repeated and overlapping selections consume the bound repeatedly, so reuse
cannot permit unlimited selected content or locator metadata.
The operation discards reused bytes on return or failure. Preparation, boundary
checking, and final acceptance remain independent verification operations.
A later operation rereads originals and detects intervening changes.
No configured allowance is increased.

Final failures preserve `evidence_byte_limit`, `evidence_record_limit`,
`evidence_work_limit`, or `evidence_selection_limit`. Older `evidence_limit`
records retain their generic meaning. An old `config` code cannot recover its
discarded cause. No historical limit or measured counter is invented.
An unchanged deterministic source-limit failure has no automatic retry.

## Human inspection and privacy

`muninn learn explain R_ID` returns JSON with `request_id` and a self-contained
ordinary-language `explanation`. `--human` also prints the final redacted prose
to stderr. `muninn learn explain R_ID --human >/dev/null` gives plain inspection
without asking a person to interpret JSON. `--pretty` only changes JSON indentation.
Hook output remains compact JSON with exit 0.

The explanation uses retained metadata. It reports the request state, available
evidence count, recorded failure phase, receipt-based saved impact, model-launch
records, current admission, appropriate actor and action, and notice lifecycle.
It distinguishes missing launch records from proof that no call occurred.
It distinguishes saved setup notices from proof of a current live setup failure.

Failed proposals are not retained. Their sentence and exact cited passage are
explicitly unavailable. The explanation displays no transcript or proposal text.
Existing `learn show` remains the explicit governed evidence inspection surface.
Erasure and changed evidence can make excerpts unavailable. No new source read
or privacy-sensitive retention is introduced. Automatic startup notices, logs,
stats, and doctor receive no new transcript or proposal text.

Safety rejections can be inspected and dismissed. A rejection code alone does
not prove the classifier was correct; incorrectly rejected direct owner wording
requires maintainer investigation. Local operators can
restore approved setup. Maintainers must investigate unclear defects and source
allowances. The explanation does not recommend retrying unchanged rejected input
or changing policy to suppress a refusal.

Upgrades preserve notices. Startup selects the newest saved notice, with setup
priority. Dismissal removes one request reminder and can reveal an older notice.
It preserves attempts, requests, calls, evidence, receipts, and attempt error
codes; the saved stage and action are removed. Success clears that request's
notice, and a later failure can create another. Dismissal neither repairs setup
nor enables learning.

## Verification boundary

Regression fixtures use synthetic originals and disposable homes. Coverage
includes shared JSONL and Cursor originals, distinct locators, bounded selected
content, later changed originals, owner wording and hostile attribution controls,
atomic rejection, generic historical causes, explicit human streams, JSON
compatibility, erased or other-scope content withholding, notice priority,
dismissal, new failures, successful completion, and state copying during upgrade.

These tests do not inspect private owner requests, qualify a production runner,
approve policy, deploy a release, or retry real requests. Issue #70's historical
preview refusal remains separately unproven; this change does not claim its cause.
