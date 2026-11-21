---
name: muninn-learn
description: Turn Muninn learning on or off and request, list, inspect, explain or retry learning reviews with muninn learn.
---

# muninn learn

```sh
muninn learn on
muninn learn off
muninn learn request --session REF [--task TASK]
muninn learn list [--state STATE]
muninn learn show R_ID
muninn learn explain R_ID [--human]
muninn learn retry R_ID
```

All output is JSON.
`--human` on `explain` also prints the explanation to stderr.
Stdout stays JSON.

## Switch and settings

`on` saves `learning.json` and learns only chat from now on.
It prints `{"enabled":true}`.
`off` stops new reviews.
It prints `{"enabled":false}`.

The poller (`muninn serve`) is the only learner.
`learning.json` is in the data directory with mode 0600.
It has four values: `enabled`, `provider` (`claude` or `codex`), `model` and `effort`.
The defaults are off, `claude`, `sonnet` and `low`.
An unreadable or invalid file counts as off.
One constant review policy applies.
`docs/REFERENCE.md` lists its limits.

## Requests

`request` queues one bounded review for one indexed session.
`REF` is an indexed `provider:thread_id` or an unambiguous session root.
`--task` binds the request to one task in the source project.
`R_ID` is `R_` plus 32 lowercase hexadecimal characters.

`list` reports `requests`.
`--state` is `pending`, `running`, `waiting`, `completed`, `failed` or `cancelled`.
`show` reports the request, attempts, calls, evidence and receipt.
`explain` reports `request_id`, `correction_status` and `explanation`.
They show no transcript or proposal text.
`retry` restarts the same request.
A completed request keeps its receipt.

## Failures

Each failed request keeps one finite `error_code`.
`docs/REFERENCE.md`, section "Learning failure messages", has the code table.
Read the code with `muninn learn explain R_ID`.

Do not retry an `evidence_limit` failure unchanged.
It means that source verification passed a read limit.
Run `muninn doctor --pretty` when the code is `config`.
