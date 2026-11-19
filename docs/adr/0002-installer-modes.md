# 0002: The installer has two modes, `--fresh` and `--upgrade`

## Status

Accepted

Date: 2026-10-01

## Context

A new machine has no data directory, launchd job or release; an installed machine only
needs to select a release and restart. Additional modes would increase maintenance and
test coverage without an identified use case.

## Decision

`install/installer.py` runs `--fresh` (new machine) or `--upgrade` (select a clean
commit); exactly one is required. `install/rollback.py` only undoes an interrupted run
from its `rollback-record.json`.

## Consequences

Installer operations are exercised with a temporary HOME in `tests/test_installer.py`. A
fresh install on a second physical machine remains unverified.

## Record operation to leave an unfinished start (2026-11-19, proposed)

The installer also has the record operation `--leave-unfinished`
(ADR 0024). It is not a third mode. It writes one mark in one rollback
record and installs nothing. It starts and stops no service, and it changes
no link, provider file or store. `--fresh` and `--upgrade` stay the only
install modes.
