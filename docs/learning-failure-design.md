# Learning failure inspection and evidence checks

Muninn checks the `original` source and the owner's authority before it accepts a batch of proposed knowledge changes.
If Muninn rejects a batch, it records no knowledge changes from that batch.
This document describes the checks and inspection commands added for issue #75.

## Owner authority

Owner authority means that the `evidence` can supply `support` for a decision by the owner.
The source must be an active primary session.
The selected field must be a permitted user-message field.
A user role alone does not prove owner authority.

The selected text must express the owner's direct intent or instruction.
Direct-owner wording elsewhere in the message cannot give authority to an ambiguous selection.
The selection must keep the complete instruction or sentence that contains it.
It must keep negation, context, scope, and restrictions at the two ends.

Outer whitespace and terminal punctuation can be left out of the selection.

For example, the selection `use Python.` from `Never use Python.` omits the negation.
Muninn rejects that selection.
A newline can continue the object of an instruction.
The continuation does not become a different owner statement.

Muninn accepts a restricted set of context phrases before an instruction.
For example, `For this project, I prefer Python.` can express direct intent.
The opening phrase must have these parts, in this sequence:

1. `for`, `in`, `on`, `within`, or `regarding`.

2. `this` or `the current`.

3. `project`, `repository`, `repo`, or `task`.

4. A comma.

Only spaces and tabs are permitted between these words.
Other opening contexts stay untrusted.
These include named or possessive attributions, narrative framing, and examples.
`In Alice's words, use Rust.` reports a different speaker's words.

`For example, I prefer Rust.` introduces an example.
A selection after one of these prefixes cannot become a direct owner instruction.

Muninn treats one identifier or path in backticks as a code value.
It does not interpret that value as prose or use it to identify a speaker.
Prose in quotes, fenced content, copied or reported instructions, and provider-injected markers cannot get authority from nearby direct-owner wording.
Assistant content, tool content, and metadata from the host that runs the agent also cannot get that authority.

Muninn treats questions, conditions, uncertain statements, and ambiguous wording as untrusted.

Muninn checks wording that reports a different speaker.
It does not guess that speaker's name.
`I prefer Rust, Alice said.` and `Use Rust, the README says.` stay reported material.
In `Use reported errors.`, the word `reported` describes errors and does not identify a speaker.

After the optional opening context phrase, one more comma makes the sentence untrusted.
Muninn treats the clause after that comma as unsupported.
This check also rejects unfamiliar attribution verbs without a guessed speaker name.

These checks recognize restricted sentence patterns.
They cannot identify all language patterns or prove where all text originated.
The transcript cannot distinguish an unmarked copy of direct prose from the same prose typed by the owner.
Unsupported wording stays untrusted.

Preferences, decisions, global changes, and replacements keep their current requirements for `direct_owner` and `owner_statement` `evidence`.
This change adds no owner approval workflow.
Pinned policy decisions stay unchanged.

## Source-read allowance

An allowance limits the work or content that one verification operation can use.
During one `validate_evidence` operation, Muninn uses again `original` bytes that it read during that operation.
It identifies each `original` by its complete physical locator:

- JSONL locators include the path, byte offset, and byte length.

- Cursor locators include the database, conversation, and record key.

Each different physical read uses the read allowance.
Each selection gets its own checks of its source, `state`, scope, projection, selector, hash, excerpt, tombstone, and provenance.
Projection identifies how Muninn extracted the field.
The selector identifies the chosen part of that field.

A tombstone records content that erasure omits.
Provenance records where the `evidence` originated.

Recorded `evidence` text has a different limit, measured in UTF-8 bytes.
Excerpts, selectors, locators, scope, identity, hash, and version strings all use this allowance.
Duplicate or overlapping selections use it again for each selection.
Using the same bytes again cannot allow unlimited selected content or locator metadata.

Muninn discards those bytes when the operation ends, with or without an error.
Setup, boundary checks, and `final` `acceptance` stay different verification operations.
Each subsequent operation rereads `originals` to find changes between operations.
This change increases no configured allowance.

Last errors keep the specified code: `evidence_byte_limit`, `evidence_record_limit`, `evidence_work_limit`, or `evidence_selection_limit`.
Older `evidence_limit` records keep their generic meaning.
A previous `config` record cannot recover a cause that Muninn did not keep.
Muninn does not invent historical limits or measured counters.

Muninn does not automatically retry an unchanged source-limit error: the same input would reach the same limit.

## Human inspection and privacy

`muninn learn explain R_ID` reports JSON with `request_id` and `explanation`.
The `explanation` field contains a complete explanation in ordinary language.
With `--human`, Muninn also prints the last redacted explanation to stderr.
To read only that explanation, run:

```sh
muninn learn explain R_ID --human >/dev/null
```

The command writes JSON to stdout.
`--pretty` changes only JSON indentation.
Hooks report compact JSON and exit 0.

The explanation uses only kept metadata: records that report the `request`.
It displays no source text.
It reports:

- The `request` `state` and kept `evidence` count.

- The recorded error phase.

- Recorded changes recorded in the commit receipt.

- Recorded model launches.

- Current admission, which controls if learning can accept work.

- The responsible person and the step they can do.

- How Muninn records and clears error notices.

Missing launch records do not prove that no model `call` occurred.
A recorded setup notice does not prove that the setup currently has an error.

Muninn does not keep unsuccessful proposals.
Their proposed sentence and unchanged cited excerpt are unavailable.
The explanation displays no transcript or proposal text.
Use `muninn learn show R_ID` to examine `evidence` with the current access checks.

Changed or erased `evidence` can have no usable excerpt.

The explanation adds no source reads and keeps no new private content.
Automatic startup notices, logs, stats, and doctor receive no new transcript or proposal text.

You can examine and dismiss safety refusals.
A refusal code alone does not prove that the wording check was correct.
If Muninn incorrectly rejects direct owner wording, the maintainer must examine the cause.
Local operators can put back approved setup.

Maintainers must examine unclear defects and exhausted source allowances.
The explanation does not recommend an unchanged retry or a policy change to suppress a refusal.

Upgrades keep recorded notices.
At startup, Muninn shows setup notices first.
If not, Muninn selects the newest recorded `request` notice.
After you dismiss one `request` notice, an older notice can show.

Dismissal keeps `requests`, `attempts`, `calls`, `evidence`, receipts, and `attempt` error codes.
It removes the recorded notice's `stage` and `action`.
Muninn clears that request's notice when the `request` succeeds.
A subsequent error can make a new notice.

Dismissal does not repair setup or enable learning.

## Verification boundary

Regression tests use synthetic `originals` and disposable homes.
They include:

- Shared JSONL and Cursor `originals`, and different physical locators.

- Limits on selected content, and subsequent changes to `originals`.

- Owner wording and `attempts` to disguise reported material as owner wording.

- Refusal of a complete batch without recording part of its knowledge changes.

- Generic historical causes, human output streams, and JSON compatibility.

- Exclusion of erased content and content from a different scope.

- Notice selection, dismissal, subsequent errors, and `request` success.

- Copies of recorded `state` during upgrade.

These tests do not examine private owner `requests` or qualify a production runner.
They do not prove policy approval, deploy a release, or retry actual `requests`.
Issue #70's historical preview refusal stays a different unresolved question.
This change does not prove its cause.
