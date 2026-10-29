# Learning failure inspection and evidence checks

Muninn checks the original source and the owner's authority before it accepts a
batch of proposed knowledge changes. If Muninn rejects a batch, it saves no
knowledge changes from that batch. This document describes the checks and
inspection commands added for issue #75.

## Owner authority

Owner authority means that the evidence can support a decision by the owner.
The source must be an active primary session. The selected field must be a
permitted user-message field. A user role alone does not prove owner authority.

The selected text must state the owner's direct intent or instruction.
Direct speech elsewhere in the message cannot give authority to an ambiguous
selection. The selection must keep the complete instruction or sentence that
contains it. It must keep negation, context, scope, and restrictions at both ends.
It can omit outer whitespace and terminal punctuation.

For example, the selection `use Python.` from `Never use Python.` omits the
negation. Muninn rejects that selection. A newline can continue the object of an
instruction. The continuation does not become a separate owner statement.

Muninn accepts a limited set of context phrases before an instruction.
For example, `For this project, I prefer Python.` can state direct intent.
The opening phrase must have these parts, in this order:

1. `for`, `in`, `on`, `within`, or `regarding`.
2. `this` or `the current`.
3. `project`, `repository`, `repo`, or `task`.
4. A comma.

Only spaces and tabs can separate these words. Other opening contexts remain
untrusted. These include named or possessive attributions, narrative framing,
and examples. `In Alice's words, use Rust.` reports another speaker's words.
`For example, I prefer Rust.` introduces an example. A selection after either
prefix cannot become a direct owner instruction.

Muninn treats one identifier or path in backticks as a code value. It does not
interpret that value as prose or use it to identify a speaker. Quoted prose,
fenced content, pasted or reported instructions, and provider injection markers
cannot gain authority from nearby direct speech. Neither can assistant content,
tool content, or metadata from the host that runs the agent. Muninn treats
questions, conditions, uncertain statements, and ambiguous wording as untrusted.

Muninn checks wording that reports another speaker. It does not assume that
speaker's name. `I prefer Rust, Alice said.` and `Use Rust, the README says.`
remain reported material. In `Use reported errors.`, the word `reported`
describes errors and does not identify a speaker.

After the optional opening context phrase, another comma makes the sentence
untrusted. Muninn treats it as an unsupported extra clause. This check also
rejects unfamiliar attribution verbs without a guessed speaker name.

These checks recognize limited sentence patterns. They cannot identify every
language form or prove where all text originated. The transcript cannot
distinguish an unmarked copy of direct prose from the same prose typed by the
owner. Unsupported wording remains untrusted.

Preferences, decisions, global changes, and replacements keep their existing
requirements for `direct_owner` and `owner_statement` evidence. This change
adds no owner approval workflow. Pinned policy decisions stay unchanged.

## Source-read allowance

An allowance limits the work or content that one verification operation can use.
During one `validate_evidence` operation, Muninn reuses original bytes that it
has just read. It identifies each original by its complete physical locator:

- JSONL locators include the path, byte offset, and byte length.
- Cursor locators include the database, conversation, and record key.

Each distinct physical read uses the read allowance. Each selection still gets
separate checks of its source, state, scope, projection, selector, hash, excerpt,
tombstone, and provenance. Projection identifies how Muninn extracted the field.
The selector identifies the chosen part of that field. A tombstone records
content that erasure excludes. Provenance records where the evidence originated.

Saved evidence text has a separate limit, measured in UTF-8 bytes. Excerpts,
selectors, locators, scope, identity, hash, and version strings all use this
allowance. Repeated or overlapping selections use it again for each selection.
This reuse therefore cannot allow unlimited selected content or
locator metadata.

Muninn discards the reused bytes when the operation returns or fails.
Preparation, boundary checks, and final acceptance remain separate verification
operations. Each later operation rereads originals to detect changes between
operations. This change increases no configured allowance.

Final failures retain the specific code: `evidence_byte_limit`,
`evidence_record_limit`, `evidence_work_limit`, or `evidence_selection_limit`.
Older `evidence_limit` records keep their generic meaning. An old `config`
record cannot recover a cause that Muninn did not retain. Muninn does not invent
historical limits or measured counters. Muninn does not automatically retry an
unchanged source-limit failure: the same input would reach the same limit.

## Human inspection and privacy

`muninn learn explain R_ID` returns JSON with `request_id` and `explanation`.
The `explanation` field contains a complete explanation in ordinary language.
With `--human`, Muninn also prints the final redacted explanation to stderr.
To read only that explanation, run:

```sh
muninn learn explain R_ID --human >/dev/null
```

The command still writes JSON to stdout. `--pretty` changes only JSON
indentation. Hooks still return compact JSON and exit 0.

The explanation uses only retained metadata: records that describe the request.
It displays no source text. It reports:

- The request state and retained evidence count.
- The recorded failure phase.
- Saved changes recorded in the commit receipt.
- Recorded model launches.
- Current admission, which controls whether learning can accept work.
- The responsible person and the action they can take.
- How Muninn saves and clears failure notices.

Missing launch records do not prove that no model call occurred.
A saved setup notice does not prove that the setup still fails.

Muninn does not retain failed proposals. Their proposed sentence and exact
cited passage are unavailable. The explanation displays no transcript or
proposal text. Use `muninn learn show R_ID` to inspect evidence under the
existing access checks. Changed or erased evidence can have no usable excerpt.

The explanation adds no source reads and retains no new private content.
Automatic startup notices, logs, stats, and doctor receive no new transcript or
proposal text.

You can inspect and dismiss safety rejections. A rejection code alone does not
prove that the wording check was correct. If Muninn incorrectly rejects direct
owner wording, the maintainer must investigate. Local operators can restore
approved setup. Maintainers must investigate unclear defects and exhausted
source allowances. The explanation does not recommend an unchanged retry or a
policy change to suppress a refusal.

Upgrades preserve saved notices. At startup, setup notices take priority.
Otherwise, Muninn selects the newest saved request notice. After you dismiss one
request notice, an older notice can appear.

Dismissal preserves requests, attempts, calls, evidence, receipts, and attempt
error codes. It removes the saved notice's stage and action. Successful
completion clears that request's notice. A later failure can create another
notice. Dismissal does not repair setup or enable learning.

## Verification boundary

Regression tests use synthetic originals and disposable homes. They cover:

- Shared JSONL and Cursor originals, and distinct physical locators.
- Limits on selected content, and later changes to originals.
- Owner wording and attempts to disguise reported material as owner speech.
- Rejection of a complete batch without partial knowledge changes.
- Generic historical causes, human output streams, and JSON compatibility.
- Exclusion of erased content and content from another scope.
- Notice priority, dismissal, later failures, and successful completion.
- Copies of saved state during upgrade.

These tests do not inspect private owner requests or qualify a production
runner. They do not establish policy approval, deploy a release, or retry real requests.
Issue #70's historical preview refusal remains a separate unresolved question.
This change does not establish its cause.
