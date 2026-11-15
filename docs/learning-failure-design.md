# Learning failure inspection and evidence checks

Muninn checks the `original` source and the owner's authority before it accepts a batch of proposed knowledge changes.
If Muninn rejects a batch, it records no knowledge changes from that batch.
This document describes the checks and inspection commands added for issue #75.

## Owner authority

Owner authority means that the `evidence` can supply `support` for a decision by the owner.
ADR 0022 sets these rules.
Muninn decides owner authority from provenance, not from the wording.

A user-message field gets `direct_owner` only when all of these are true:

- The source is an active primary session.

- The indexer reads the message as a `prompt` (`classify.user_text_kind`), not as harness or delegation text.

- The provider record has no injection flag (`isMeta`, `isSidechain`, `isCompactSummary`, the channel flag, or `toolUseResult`).

- The message has no tool result.

- The message has no harness tag, Claude harness prefix, ChatGPT reference tag, or Muninn marker (`FLAG_MARKERS`) anywhere.

- The selection covers whole sentences. Outer whitespace and a final `.`, `!`, or `;` can stay outside it.

Questions, conditions, plain agreement such as `i agree`, and quoted or pasted text in an owner message get `direct_owner`.
The transcript cannot show the difference between pasted text and typed text.
The agent decides what the words mean.
In Cursor, every user bubble is a `prompt`, so a `stop` hook `followup_message` can look like owner text.
The owner accepted these risks.

## Verbatim proof

A preference, a decision, a global change, and a replacement of a preference or a decision need owner support.
Each line of such a claim must equal whole sentences of a bound excerpt.
A line is the note content, one note condition, or one guide claim.
The comparison is case-sensitive.
It collapses whitespace and ignores one final `.`, `!`, or `;`.
A line can equal a run of consecutive whole sentences of the excerpt.
A sentence ends at `.`, `!`, or `;` before whitespace. A `?` does not end a sentence.

For example, the claim `use Python.` from `Never use Python.` omits the negation.
Muninn rejects that claim.
The claim `Use Postgres.` with the cited owner line `Use pytest.` is also rejected.

A bound excerpt comes from one of these:

- A cited `direct_owner` owner line.

- A cited assistant reply that is the final permitted field directly before a cited `direct_owner` owner line in the same thread.

For the second case, Muninn reads the gap again in the commit transaction.
A tool call, a tool result, another message, or an erased line in the gap refuses the pair.
The agent decides that the owner reply agrees.
A question and a `yes` reply can be the proof.

The model can also write `corrected`, a copy with spelling and grammar fixed.
Muninn does not check it.
`know list`, `know show`, and `know decision show` show it next to the proof.
Recall, `know context`, `know search`, and review model input show only the proof.

A verified ADR 0018 decision binds the owner reply to the complete displayed candidate.
When Muninn applies that decision, the display is the verbatim proof.

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
A refusal code alone does not prove that the provenance or verbatim check was correct.
If Muninn incorrectly rejects owner support, the maintainer must examine the cause.
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

- Owner provenance per provider, Muninn markers, and harness tags inside a message.

- Verbatim whole-sentence proof, agreement pairs, and gaps that refuse a pair.

- Refusal of a complete batch without recording part of its knowledge changes.

- Generic historical causes, human output streams, and JSON compatibility.

- Exclusion of erased content and content from a different scope.

- Notice selection, dismissal, subsequent errors, and `request` success.

- Copies of recorded `state` during upgrade.

These tests do not examine private owner `requests` or qualify a production runner.
They do not prove policy approval, deploy a release, or retry actual `requests`.
Issue #70's historical preview refusal stays a different unresolved question.
This change does not prove its cause.
