---
name: muninn-recall-review
description: Review a batch of 50 automatic Muninn recall deliveries, or inspect whether supplied memories were useful or noise. Use when the hook says review is due or the owner asks to review recall results.
---

# muninn recall-review

    muninn recall-review --pretty
    muninn recall-review --batch 1 --pretty

Follow the returned `review_prompt`. The default is the oldest unreviewed batch,
or the current partial batch. Each batch has at most 50 nonempty generated recall
blocks across Claude and Codex. The durable metadata records final source refs,
excerpt caps, request fingerprints and session ids, never conversation text.

Open `request_ref`, assessment refs and delivered source refs with `muninn open`.
Previews are navigation only. A request stays unresolved until uniquely matched
in its indexed session by fingerprint and a timestamp within two minutes of the
hook. Missing ratings count as unassessed. `self_assessments` counts model ratings,
not validated usefulness. Review examples and disagreements, separated by provider.
Historical full block text is not retained; current knowledge may have changed.

After reviewing a complete batch, acknowledge it explicitly:

    muninn recall-review --batch 1 --ack

Only the next unreviewed complete batch can be acknowledged. This records that the
review happened; it does not certify quality or turn recall off. The reminder stays
in subsequent nonempty recall blocks until acknowledgement. Do not acknowledge
before performing the review. Report missing evidence and limits to the owner.
