# 0012: Use keyed HMAC content tombstones with a durable secret

## Status

Accepted (implementation decision; separate design approval not recorded)

Date: 2026-10-05

## Context

`erase` writes line tombstones (ids and a hash of the raw line) so a rescan cannot
restore content. A fork copies its parent's history under a new thread id and perhaps
new line numbers, so a line tombstone cannot find the copy. A content tombstone can
match the event's role and text across those changes. A plain hash of short erased text,
such as a password, would allow guesses to be confirmed against `tombstones.jsonl`.

## Decision

Each erased event also gets a content tombstone: `ev2:` plus HMAC-SHA256 of role and
text, keyed with `tombstone.key` in the data directory.

- The key is 32 random bytes, mode 0600, created atomically the first time an erase
  applies (never in a dry run) and never logged. It is never made while keyed tombstones
  exist, since a new key would match none of them.
- Ingest applies a tag only to a fork's replayed prefix, so a later identical user turn
  of the fork's own survives. `erase_family.py` extends an erase across the fork family
  by the same rule.
- If the key is missing or not 32 bytes while keyed tombstones exist, `ingest`,
  `rebuild` and `erase` refuse (exit 4, error `tombstone_key`) and `doctor` reports an
  error. `rebuild` leaves the key in place, and `bin/muninn-uninstall` keeps it with the
  data directory (ADR 0009).

## Consequences

The durable-key lifecycle, including refusal when the key is missing, remains subject to
separate design confirmation.

- Losing the key does not delete stored data, but existing content tombstones cannot be
  matched without it. Refusal prevents reimporting erased content through forks until
  the original key is restored. Data-directory backups must include `tombstone.key`.
- The key is stored beside `tombstones.jsonl`. Protection is limited to disclosure of
  that log alone; access to the entire directory exposes both the key and tags.
- No plain-hash content tombstone ever shipped, so there is no legacy format to read.
  Line tombstones keep their plain line hash.
- Changing the tag scheme requires coordinated updates to `tombstones.py`,
  `ingest_parse.py`, `erase_collect.py`, `erase_family.py`, the documentation and the
  tests.
