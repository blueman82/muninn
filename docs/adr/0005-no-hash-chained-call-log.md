# 0005: Defer hash-chained call logging

## Status

Accepted (deferred)

Date: 2026-10-01

## Context

`calls.jsonl` is a bounded, content-free call log with no tamper evidence. A hash chain
would make edits or deleted lines detectable, but it would also need rotation turned off
to keep proof for months. Tamper evidence is a defense-in-depth measure rather than a
requirement for the current diagnostic log.

## Decision

Keep the bounded, content-free `calls.jsonl` for debugging and support. Do not treat it
as tamper-evident audit evidence.

## Consequences

Edits cannot be detected, and rotation retains approximately 2 MiB of log data. Revisit
hash chaining if an audit requires evidence of which memory records were shown to an
agent. Entries contain only ids and counts, so individual chained records would remain
small.
