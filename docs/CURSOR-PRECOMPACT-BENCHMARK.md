# Measuring Cursor preCompact

Run the opt-in benchmark with synthetic Cursor data:

```sh
python3.13 -m tools.benchmark_cursor_precompact \
  --runs 30 \
  --background-conversations 1000 \
  --messages 500 \
  --message-chars 512 \
  --timeout-s 90
```

By default, the process deadline comes from `integrations/cursor/hooks.json`.
Use `--timeout-s` to compare another positive integer deadline; this changes
only the benchmark subprocess deadline and does not edit the production hook
configuration. The JSON summary reports both `configured_timeout_s` and
`tested_timeout_s`.

Choose workload sizes from the largest environment Muninn is expected to
support. The example values are sample inputs, not measured limits or a pass
threshold. Repeat with larger background databases, longer active
conversations, and `--lock-delay-ms` plus `--lock-hold-ms` to place writer
contention during the refresh's write phase.

The benchmark creates a temporary `HOME`, Cursor database, and Muninn store.
It invokes the real `bin/muninn hook pre-compact --provider cursor` command
for every sample. The generated database has a primary-key `cursorDiskKV`
table, background conversations with two bubbles each, and one active
conversation with the requested message count and text size. No Cursor
installation or provider transcript is read or changed.

Each sample uses a fresh Muninn store initialized before timing. The synthetic
Cursor database is reused between samples, so filesystem cache state is
uncontrolled; the result is not a cold-cache measurement. Output includes
every elapsed time, nearest-rank p95, maximum, database size, completed runs,
hook notices (such as a writer-lock timeout), and runs that reached or
exceeded the tested timeout. A timed-out process is stopped at that tested
timeout.

## Current result

On macOS 27 arm64 with Python 3.13.15, a generated 466,698,240-byte database
with 100,000 active messages of 4,096 characters completed five 90-second
runs in 61.606, 61.656, 63.372, 66.908, and 67.978 seconds. The 30-, 45-, and
60-second deadlines each timed out in one run; 75 and 90 seconds each completed
in one run. The 90-second production deadline leaves 22.022 seconds above the
slowest of these five samples.

This is a measured setting for that synthetic workload and host, not a bound
for all Cursor databases or machines. The five-run nearest-rank p95 equals the
maximum and is too small a sample to estimate a reliable tail. Re-measure with
representative real workload sizes and additional hosts before treating the
deadline as a general guarantee.
