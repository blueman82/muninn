# 0020: Accept one historical unknown call without changing its evidence

Status: Replaced by [ADR 0026](0026-simple-learning.md).

Previous status: accepted for the owner-authorized recovery command.

An interrupted call can have an unknown result and no recorded launch identity.
An ended failed attempt does not prove that the native process stopped or that
provider charges stopped. The ordinary installer and supervisor must continue to
refuse unacknowledged uncertainty.

`muninn learn recover-unknown --attempt A_ID --round N --accept-uncertainty`
records the owner's acceptance of that uncertainty for one exact historical
call. It requires an unknown call without any recorded launch, an ended failed
attempt, a failed request without a retry, and a valid existing supported v3
Codex policy. Existing managed grants and the policy bridge must validate. It
creates no grant, executes no model and retries no failed request.

The command retains the maintenance owner and writer barriers, opens a fresh
autocommit connection, captures the selected records and authority, then calls
`verify_current`. Inside one immediate transaction it compares the snapshots,
checks other unfinished work, advances the generation and opens admission. It
uses the existing effective-policy check before inserting the acknowledgement
and committing. Any failure rolls back admission, the fence and the record.
Readers cannot observe admission open before the acknowledgement commits.

The optional `review_unknown_ack` table contains a stable `UA_` identifier,
target IDs, full-row hashes, the historical request and call policy digests,
the policy digest that authorized recovery, old and resulting generations,
time and explicit uncertainty acceptance. Updates are refused. Foreign-key
cascade removes an acknowledgement if its target is deleted. The original
call, charge, request and attempt rows remain unchanged.

The read-only exemption requires exact historical hashes, terminal states and
a current generation at least as new as the recorded fence. Its authorization
policy digest is historical audit metadata. A later independently authorized
policy change does not revoke acceptance of the old uncertainty. Repeating the
command returns the same record without advancing generation or reopening a
later pause. A changed historical record invalidates the exemption. Erasure
retains noncontent audit metadata, but its changed request state invalidates
the old binding. New uncertain work still blocks restart.

The auxiliary table is added without changing the core format marker. An
upgrade archives a store that lacks the table before adding it. Exact old and
new DDL fingerprints are recognized. Copy and rebuild preserve the complete
record. Missing-table reads create nothing and grant no exemption.

To deploy from a checked clean feature checkout, use the exact checked commit:

```sh
./bin/muninn-install --repo "$PWD" --sha CHECKED_COMMIT --upgrade
```

Omit `--enable-learning` and answer `no` to the separate learning restart
prompt. Confirm that admission remains closed and the original unknown call
remains unchanged. Then run the explicit command above through the installed
launcher and restart the service. Installation does not itself accept the
historical uncertainty. The unknown-call count remains truthful after recovery.
