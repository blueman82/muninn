# 0012: Content tombstones are keyed HMAC tags, and `tombstone.key` is a durable secret

Status: Accepted, 2026-10-05. Decided by: implementer, inside the hardening work the owner approved (PR #10 and its review
fixes). The owner did not approve this design itself; the key's lifecycle (never regenerated, refusal when it is missing) is
the point to revisit.

**Context.** `erase` writes line tombstones (ids and a hash of the raw line) so a rescan cannot restore content. A fork copies
its parent's history under a new thread id and perhaps new line numbers, so a line tombstone cannot find the copy. A tombstone
of the event's role and text can, but a plain hash of a short erased text, such as a password, could be confirmed by guessing
against `tombstones.jsonl`.

**Decision.** Each erased event also gets a content tombstone: `ev2:` plus HMAC-SHA256 of role and text, keyed with
`tombstone.key` in the data directory.
- The key is 32 random bytes, mode 0600, created atomically the first time an erase applies (never in a dry run) and never
  logged. It is never made while keyed tombstones exist, since a new key would match none of them.
- Ingest applies a tag only to a fork's replayed prefix, so a later identical user turn of the fork's own survives.
  `erase_family.py` extends an erase across the fork family by the same rule.
- If the key is missing or not 32 bytes while keyed tombstones exist, `ingest`, `rebuild` and `erase` refuse (exit 4, error
  `tombstone_key`) and `doctor` reports an error. `rebuild` leaves the key in place, and `bin/muninn-uninstall` keeps it with
  the data directory (ADR 0009).

**Consequences.**
- Losing the key does not delete anything, but erased content can return through forks until it is erased again. A backup of the
  data directory must include `tombstone.key`.
- The key sits beside the log it protects, so it defends against reading `tombstones.jsonl` alone, not against reading the whole
  directory.
- No plain-hash content tombstone ever shipped, so there is no legacy format to read. Line tombstones keep their plain line hash.
- Changing the tag scheme touches `tombstones.py`, `ingest_parse.py`, `erase_collect.py`, `erase_family.py`, the docs and the
  tests together.
