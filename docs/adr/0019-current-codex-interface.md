# 0019: Follow the current trusted Codex interface

## Status

Accepted for source implementation, 2026-11-04. Initial owner activation remains
a separate affirmative installer decision. Source review, final gate, delivery,
installation and live acceptance are separate milestones.

## Context

An exact saved executable path can disappear when the Codex manager updates.
Keeping obsolete packages or rewriting policy for each version makes learning
depend on a changing program version. Ignoring an old grant's hash would silently
change what the owner approved.

## Decision

Add an explicit managed interface selection for the owner's fixed local Codex
manager on Darwin/arm64. Resolve `current` only within its canonical `releases`
subtree. Check the complete bounded package and main Apple signature. Negotiate
required advertised arguments with three fixed, isolated model-free probes.
Missing isolation refuses. Package metadata is not whole-package publisher proof.

Keep old exact policy shapes, canonical bytes and fingerprints. Enforce their
actual executable hashes. One initial checked UPDATE RULE can replace only the
runner selection. It requires a concrete nonwriting preview and fresh yes/y
consent. Keep every nonruntime policy term, account, scope and limit exact.

Store stable selection in the policy and attempt. Store actual checked identity
and append-only activation history per call. Compatible updates leave the grant
unchanged. Permit one positively proved pre-exec refresh under the same reserved
call. Never automatically relaunch started or uncertain execution.

Keep the complete eligible old queue through one finite initial capsule. Check
its immutable snapshots and original inputs at every authority/read gate. Keep
normal generation, source, erasure, attempt and budget fences. Unsupported pending
decision work refuses. Do not cancel or recreate work to migrate it.

Treat capability cache as disposable evidence. Safe invalid content requires all
isolated checks again under the original deadline. Unsafe storage refuses. Write
complete private metadata atomically. Authority loss cannot reconstruct consent.

Use typed versioned JSON in existing storage. There is no DDL or store marker
change. Current copy and row fingerprints include the exact JSON. Old code still
cannot parse the new authority. A committed grant stays paused after failure;
checked recovery must use compatible code and must not restore an old archive.

Cleanup records a proved SQL commit before it pauses admission. If that record
write also fails, the service stays stopped and the exact SQL state stays
unchanged. Admission can remain enabled while the service is stopped. Repair
the record storage fault, then repeat the same command. Do not start the
service by hand or accept a changed generation as proof of the same operation.

## Consequences

Codex updates no longer require a version-specific grant. The current route is
limited to Apple silicon macOS. Generic runner behavior and executable limits
remain unchanged. Codex package bounds are 384 MiB per file, 512 MiB per package,
256 inventory paths and depth eight.

Same-user compromise, auxiliary origin and actual billing remain unverified.
Help output proves advertised arguments, not future model behavior or full
isolation. Normal result validation, containment and all prior limits remain.
Private retained backups keep their existing erasure and restore limits.
