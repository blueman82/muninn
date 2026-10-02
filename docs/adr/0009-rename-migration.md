# 0009: `--upgrade` migrates a pre-rename (pctx / provenance-context) install

Status: Accepted, 2026-10-02. Decided by: owner (migrate the live machine rather than start over).

**Context.** The project was renamed from pctx / provenance-context to muninn. The live machine runs the old names: launchd
job, release dir, data dir with `pctx.sqlite`, `~/.local/bin/pctx`, Claude hooks and the Codex plugin, marketplace and
hook trust.

**Decision.** No new mode (ADR 0002 stands). `--upgrade` detects the old layout (`Ctx.legacy`) and runs `LEGACY_STEPS`:
stop the old job, take the old `writer.lock` (held: refuse as busy), copy the data dir with `pctx.*` renamed `muninn.*`
and check table counts, then pin, start the new job, rewrite the hooks and Codex entries, verify, and check the new store
holds every old row. Only after that does `legacy_remove` stop the old job, take the old writer lock again, refuse to delete anything if the
old store's row counts differ from the copy-time counts, and delete the old plist, link, release, Codex cache and last
the data. It tolerates already-deleted paths, and a rerun of `--upgrade` that finds a verified new install with old
leftovers (`legacy-counts.json` in the new lib dir) only finishes this removal. The old stored-label markers are not
recognised.
Until then a failure rolls back with the existing `install/rollback.py` and restarts the untouched old job. A second run
finds no old layout and is a plain upgrade. `MARKERS` and `CODEX_KEYS` also recognise the old Codex names.

Old `PCTX_*` variables are ignored; the CLI (never a hook) prints one stderr line naming the `MUNINN_*` replacement. Erase's
residue scan also covers an un-migrated old data dir beside the new one.

**Consequences.** The legacy code can be deleted once no machine runs the old names.
