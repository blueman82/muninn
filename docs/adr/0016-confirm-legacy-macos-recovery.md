# ADR 0016: Confirm recovery for older macOS records

## Status

Accepted.

## Context

New macOS recovery records save the data volume UUID.
Older records do not save this value.
After a restart, macOS can change a file's device number.
The current recovery check refuses an older record when those numbers change.

The saved witness can still match by file number and exact content.
This evidence does not identify the old data volume.
Muninn must not assume that the current volume is the old volume.

## Decision

The installer checks an older record before it asks for approval.
The check requires a later macOS boot, the same lock and witness file numbers,
the same witness content, and a readable current volume UUID.
The check does not change recovery files or the store.

If these checks pass, the installer asks the owner to continue.
After approval, Muninn repeats the checks while it holds the maintenance lock.
It writes a new record with current file identities and the current volume UUID.
It keeps the saved boot ID, so recovery can verify that a restart already occurred.
It then runs the existing recovery procedure.
The installer continues only after recovery succeeds.

If a check fails or the owner declines, Muninn keeps the recovery files and store.
It leaves the service stopped and reports the reason.
New recovery records use the existing volume UUID check without a prompt.
This approval accepts the remaining disk-identity uncertainty.
It does not prove that the current volume is the old volume.
This decision does not qualify Windows repair.

## Consequences

An owner must approve recovery once for a matching older macOS record.
This approval accepts a limit in the old record.
It does not replace proof of the old volume identity.
Changed lock or witness inodes, or changed witness content, still block recovery.
All successful recovery continues to apply saved deletion requests.
