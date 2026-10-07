# Cross-platform support plan: Windows and Linux

Date: 2026-10-07. Status: implementation authorised; hosted CI bootstrap
underway; platform acceptance pending.

## Scope and evidence

(verified) The owner requested Windows access, then clarified macOS and
Windows runners and added native Linux support. The owner subsequently
authorised the full implementation and GitHub Actions configuration through
`gh` authenticated as `blueman82`.
(inferred) Make native Windows 11 and native Linux first-class targets alongside
existing macOS, covering the CLI, background indexing, installer lifecycle
and supported provider hooks. WSL 2 is a separate Linux installation
environment with its own acceptance requirements. Each environment gets
separate installations, provider configurations, data directories and keys.

(verified) This planning worktree is `/Users/garyharr/Github/muninn-windows`,
branch `codex/windows-support-plan`, based on freshly fetched `origin/main`
at `34b86b21d8520651c4671d12e4269a2db07e2683`. Source references below describe
that committed base, not the uncommitted Cursor work in the original feature
worktree. Reconcile intervening main changes before implementation.

(verified) The user has no Windows or WSL environments to provide. The owner
authorised implementation, branch publication and standard hosted CI runs.
No merge, billing changes or installation into the owner's real home are
authorised. Hosted bootstrap jobs are distinct from platform acceptance.

(inferred) Initial platform scope: Windows 11 x64 with CPython 3.13, Git,
local NTFS state and a normal user account; native Ubuntu 24.04 LTS x64 with
Python 3.13, SQLite FTS5, Git and local ext4 state; WSL 2 Ubuntu with the same
Linux runtime requirements and state on its Linux filesystem. Ubuntu's default
Python is not assumed to satisfy the 3.13 floor: provision and verify it.
The launcher accepts 3.13+; first acceptance targets 3.13 specifically.
Existing macOS remains supported and regression-tested. Other Linux distros,
ARM64, network shares, cloud-synced state, shared Windows/WSL databases and
cross-environment hook execution
remain outside the first support claim. Reject unsupported state locations
when safe locking or access control cannot be established. Reassess ARM64
once x64 acceptance is complete.

Unknown: actual native provider versions, hook execution shells, configured
homes, supported platform hook events, and Cursor Windows/Linux database layout on
real installations. Verify them before promising each integration.

## Current barriers

All rows are (verified) source findings; their platform implications are
(inferred) unless an external platform source is cited.

| Area | Current source evidence | Required work |
|---|---|---|
| Import and locking | `muninn/store.py:13,240`, `muninn/tombstones.py:10,210`, `muninn/cli_rebuild.py:12,52` import `fcntl`; CLI eagerly imports handlers in `muninn/cli.py:25` | Isolate OS-specific imports; preserve the one-writer contract for every command. |
| Private state | `muninn/store.py:70,191` creates a directory then chmods it and creates SQLite with mode 0600; `muninn/obs_log.py:169` and `muninn/tombstone_key.py:45` rely on POSIX creation modes | Secure Windows directory creation and effective ACL verification before sensitive writes, including existing/custom homes. |
| Durability and replacement | `muninn/cli_rebuild.py:52,196,273`, `muninn/tombstones.py:210`, `install/configedit.py:60,69,97`, `install/snapshot.py:82,190` use file/directory sync, rename and replace | Prove Windows file publication, open-reader sharing and failure recovery; preserve tombstone-before-commit ordering. |
| Safe source reads | `muninn/hook.py:145` and `muninn/query/opening.py:88` use `O_NOFOLLOW`/`O_NONBLOCK`; ingest checks lstat then reopens in `muninn/ingest_plan.py:45,292` and `muninn/ingest_parse.py:208` | Preserve regular-file and root boundaries on Windows, including reparse points and replacement races. |
| Poller control | `muninn/cli_serve.py:101` registers SIGTERM/SIGHUP and tightens POSIX modes | Windows stop/start control and crash recovery without assuming Unix signals. |
| Paths and identity | `muninn/store.py:40`, `muninn/ingest.py:60`, `muninn/scope.py:77`, `muninn/obs_status.py:97` assume local/share paths, host-native path parsing, and a current symlink | Resolve each platform consistently; test drive letters, case, Unicode, worktrees, and release identity. |
| Installer | `install/context.py:111,135,245` evaluates `os.getuid()` at import and uses launchctl; `install/preflight.py:96,128`, `install/steps_release.py:61,102,244,359` require symlinks and launchd | Per-platform user lifecycle and release selection with shared transaction/rollback logic. |
| Rollback and uninstall | `install/rollback.py:88,140,176`, `install/snapshot.py:208,249`, `install/uninstall.py:197,209,254` stop launchd, undo links, and restore/store retained data | Stop the actual writer before restoring or deleting; preserve rollback, prune-last and retained-data rules. |
| Launchers and hooks | `bin/muninn:1`, `bin/muninn-install:1`, `integrations/claude/settings-hooks.json:9`, `integrations/codex/hooks/hooks.json:10`, `install/preflight.py:72`, `install/verify.py:166` use shell launchers, literal home substitution and POSIX shlex | Native launcher, correct JSON/TOML serialization and provider-specific shell quoting; no Git Bash prerequisite. |
| Cursor history | `muninn/cursor_import.py:20`, `install/steps_release.py:47`, ADR 0013 | Windows path discovery, read-only import and missing/unsupported database handling; keep full-ingest-only behavior. |
| Doctor | `muninn/obs.py:154,188,198,417,454` checks POSIX modes and launchd; `muninn/obs_status.py:97` reads release symlink | Report actual Windows ACL/scheduler/process health, distinguishing unknown from healthy. |
| Development gate | `tools/check.py:25,79`, `tests/test_integrations.py:29`, `tests/test_query_open.py:202`, `tests/test_serve_alive.py:71`, `.codex/hooks.json:10`, `.githooks/pre-commit:1` assume bin tools, shell, macOS Python or Unix signals | Keep all gates mandatory; provide native paths/commands and platform-specific assertions with equal coverage. |

(verified) Native Linux still has explicit macOS barriers: installer
`Ctx` derives a LaunchAgents plist and `gui/<uid>` launchctl target
(`install/context.py:135,245`); doctor requires that launchd job
(`muninn/obs.py:417,479`); release/lifecycle checks depend on the plist
(`install/preflight.py:96`, `install/steps_release.py:359`); Cursor discovery
uses only a Library path (`muninn/cursor_import.py:20`). The runtime already
has POSIX flock/private-mode primitives and a non-macOS fsync fallback
(`muninn/store.py:70,240`, `muninn/cli_rebuild.py:52`), but that source alone
does not establish Linux support. Hook fragments point at user-local shell
launchers; prove Linux provider homes and trust/quoting independently.

(verified) Python documents `fcntl` as Unix-only. Python 3.13 supports
`os.fchmod` on Windows, but Windows chmod/fchmod handles the read-only flag;
it does not establish Unix-style privacy. Python 3.13 `os.mkdir(mode=0o700)`
specifically applies Windows access control for the current user and
administrators. The existing mkdir-default-then-chmod sequence does not use
that creation contract. Sources: [fcntl](https://docs.python.org/3.13/library/fcntl.html),
[mkdir](https://docs.python.org/3.13/library/os.html#os.mkdir),
[chmod](https://docs.python.org/3.13/library/os.html#os.chmod),
[fchmod](https://docs.python.org/3.13/library/os.html#os.fchmod).

## Proposed implementation, in dependency order

### 1. Agree platform contracts and prove the difficult primitives

(inferred) Start with small executable experiments on a hosted Windows x64
runner, using synthetic content and temporary homes. Do not refactor the
whole package before these pass. Proposed choices below remain provisional
until the experiments establish their contracts.

- Lock: keep `store.writer_lock(home, wait_s)` as the single entry point.
  Try stdlib `msvcrt.locking(..., LK_NBLCK, 1)` on byte zero of the persistent
  lock file, seeking to zero before both acquisition and explicit unlock,
  then closing the descriptor. Keep the current monotonic deadline/backoff;
  classify only documented lock contention as BusyError, propagating unrelated
  I/O/ACL errors. Do not use LK_LOCK's built-in ten-second retry. Preserve
  non-reentrancy, zero-wait contention, release
  after process termination and inheritance restrictions. Two independent
  processes, a crash, a timeout, and multiple writers must prove exclusion.
  Do not replace it with an existence/PID lock or rely solely on SQLite:
  tombstone, rebuild and snapshot operations also need serialization. If
  any contract fails on CPython 3.13 plus local NTFS, switch only the primitive
  to stdlib ctypes LockFileEx and rerun the contract tests.
  Sources: [msvcrt.locking](https://docs.python.org/3.13/library/msvcrt.html#msvcrt.locking),
  [Microsoft _locking](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking?view=msvc-170),
  [LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex).
- Privacy: create new state directories privately using Python 3.13's
  Windows mode-0700 creation contract; verify ownership and inheritable DACLs
  with narrow stdlib `ctypes` access to native security APIs if necessary.
  Use native tools only where they can be safely checked, without fragile
  parsing of localized output. Check every sensitive artifact, including
  SQLite journals, temporary files, logs, snapshots and the tombstone key.
  Permit only the current user and the system/admin principals explicitly
  specified in the proposed security ADR. Refuse unsafe existing/custom
  state before writing rather than calling chmod and reporting privacy.
- Safe reads: establish a Windows handle-based regular-file and reparse
  policy, check the opened object's identity/final location against the
  allowed root, and bound reads. Cover intermediate junctions, symlinks,
  device/UNC paths and path swaps. Do not implement `getattr(flag, 0)` as a
  substitute for the current read protections. Apply the same validated read
  path to raw open, hook headers and ingest where required by their boundary.
- Durability: test tombstone flush before database commit, key publication,
  atomic status/config/release selection, rebuild replacement, snapshot
  restore and sharing violations with open readers. Close owned handles
  before replacement; for external readers use a bounded, classified retry
  or a safe refusal that leaves the old state intact. Never suppress sync
  errors or claim directory durability from a successful file fsync.
  Resolve Windows directory publication guarantees explicitly before release.
  Preserve same-directory temp and replacement; never delete-first or copy
  over the live file. Test binary descriptors with LF/CR/control bytes so CRT
  text-mode translation cannot change hashes, offsets or tombstone records.
  Process-crash tests do not prove power-loss durability. ReplaceFileW's
  WRITE_THROUGH flag is documented as unsupported, so it does not settle the
  publication guarantee. Any weaker durability contract needs an explicit
  owner decision before changing the existing guarantee.
  Sources: [CreateFileW sharing/security](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew),
  [ReplaceFileW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew).
- SQLite: test the actual CPython 3.13 SQLite build for FTS5, secure-delete
  settings and hot-journal recovery. A returned pragma value alone is not
  erase verification: scan synthetic secret bytes in database/journal/asides
  and inspect FTS vocab as existing erase tests do.

(inferred) Deliver a platform contract ADR covering these results, plus a
lifecycle ADR. Reference ADRs 0001, 0003, 0006, 0008, 0009, 0011, 0012 and
0013 where their platform-specific wording needs extension. Preserve one
pinned release, interpreter discovery, fresh/upgrade modes, fail-closed
gates, no-write check, retained data, migration snapshot, durable key and
read-only Cursor import. Ask the owner before any actual reversal; this plan
approves none. Privacy or crash guarantees that cannot be preserved block
that platform's release instead of becoming silent exemptions.

**Exit:** lock/security/publication experiments pass on real Windows;
unresolved filesystem guarantees have a documented safe refusal or remain
release blockers. Native Windows 11 acceptance remains a separate gate.

### 2. Make the runtime and CLI portable

(inferred) Change only the OS-dependent boundaries: `muninn/store*`,
`tombstones.py`, `tombstone_key.py`, `cli_rebuild.py`, `cli_serve.py`,
`hook.py`, `query/opening.py`, `ingest*`, `cursor_import.py`, `scope.py`,
`obs*`, and `cli_parser.py`. Add small responsibility-named helpers only
where multiple existing call sites need the proven primitive; no generic
platform class hierarchy or dependency injection framework. Keep all
runtime/installer imports stdlib-only, explicit and at module top. Select
OS imports at module scope; keep the strict annotation and docstring gate.

(inferred) Reuse `MUNINN_HOME`, `MUNINN_ROOTS`, `MUNINN_PYTHON` and existing
entry points. Recommend native state/releases under `%LOCALAPPDATA%\Muninn`
with separate data/lib/bin subdirectories; keep current macOS paths and
Linux user paths. Centralize this small path policy so installer, runtime,
release identity and doctor agree. Validate custom paths against the
security/filesystem contract. Respect provider home overrides only where
provider documentation confirms them; do not assume every provider uses HOME.

(inferred) Keep provider roots platform-local. Treat transcript cwd strings
from another OS conservatively instead of resolving `C:\...` against Linux
or translating `/mnt/c` into a native scope. On Windows test alternate drive
case and separator spellings, drive-root vs drive-relative paths, Git
worktrees, deleted paths and Unicode. Change normalization only where it
preserves existing scope identities; any store re-key/migration needs its
own evidence and migration plan.

(inferred) Add a thin native command entry point for interactive PowerShell
and cmd, delegating to the existing Python CLI with isolated imports.
Resolve `MUNINN_PYTHON`, installer-recorded Python, then a version-checked
Windows Python discovery fallback. Preserve arguments and exit codes and
keep JSON stdout / compact hook JSON / hook exit-0 behavior. No new package
manager, executable bundler, GUI, or mandatory PowerShell policy changes.

**Exit:** native import/CLI and synthetic ingest/search/open/knowledge/erase/
compact/rebuild/doctor pass; macOS remains green. CLI evidence may be
reported as such before claiming complete installer/provider support.

### 3. Implement native installation and background indexing

(inferred) Extend existing `Ctx`/`Runner` and installer transaction steps,
using a small per-platform lifecycle module where needed. Remove import-time
UID evaluation from the shared context. Keep the same clean-SHA archive,
preflight, fresh/upgrade, record, verification, rollback and prune-last flow.
Do not duplicate the installer or add new public installer modes.

(inferred) Recommend a per-user Task Scheduler task, running as the current
user without administrator elevation or stored credentials, starting at
user logon and restart-on-failure. Probe actual registration, process state,
heartbeat, logon/logoff and disabled-task behavior before fixing this choice.
Do not claim pre-logon or unattended machine-wide indexing. If policy blocks
registration, return a clear preflight refusal; foreground CLI use can remain
available independently.

(inferred) Avoid requiring Windows symlink privileges: use a small atomically
published release-selection record containing the pinned SHA and interpreter
path, with stable launchers outside the release directory. Validate SHA/path
and ownership before use; record and restore the prior selection on failure.
Prove Codex marketplace/plugin resolution follows the selected release and
cache refresh after upgrade. Update `install_sha`, status, uninstall ownership
and preflight checks together. Keep one release after successful pruning.

(inferred) Provide a bounded graceful poller stop mechanism for Windows
that is separate from POSIX signal handling, checked between transactions,
with a private request tied to the current process/install generation.
First inspect existing status/heartbeat seams; do not invent a remote service.
Lifecycle stop must wait until the correct process exits before a store swap,
rollback or release prune. Escalate a stuck worker through the native task
control path only after the wait; exercise interrupted writes/hot journals.
Do not kill an unrelated PID after PID reuse. A full graceful-stop proof is
part of the primitive/lifecycle spike, not an assumption about Task Scheduler.

(inferred) File areas: `install/context.py`, `constants.py`, `preflight.py`,
`steps_release.py`, `record.py`, `snapshot.py`, `rollback.py`, `uninstall.py`,
`installer.py`, `verify.py`, `bin/`, platform integration templates and
`muninn/cli_serve.py`, `obs.py`, `obs_status.py`. Native uninstall tests use
temp homes/fakes; agents execute only dry-run against actual installs.

**Exit:** synthetic Windows fresh/check/status/upgrade/schema rollback/
interrupted rollback/prune/uninstall-retain cases pass. `--check` writes
nothing, unsafe preflight changes nothing, failed upgrade preserves the old
usable install/key, and verified scheduler/heartbeat identifies the release.

### 4. Connect each provider and implement native Linux plus WSL installation

(inferred) Render paths using JSON/TOML serialization rather than replacing
`@HOME@` bytes inside pre-serialized JSON. Quote Windows hook commands for the
provider's actual shell; use argv directly for owned subprocesses. Replace
POSIX `shlex.split` verification on Windows with the actual supported
execution contract. Update `integrations/claude/`, `integrations/codex/`,
`install/transforms.py`, `trust.py`, `probe.py`, `steps_config.py`, `verify.py`
and installer fakes/contracts. Exercise paths with spaces, non-ASCII and
shell metacharacters; ensure foreign config entries and ACLs survive edits.

(inferred) Test native Claude Code and Codex separately for transcript path,
hook stdin/stdout, time bounds, disabled/failure behavior, resolved command,
trust hash and cache after upgrade. Synthetic providers prove our adapter;
installed-provider acceptance proves integration. Unknown/untrusted hooks
remain owner steps, never successful acceptance. Do not manufacture trust for
an unverified provider version. Cursor Windows/Linux discovery is provisional
until documented configuration or a real database confirms the path/schema.
Provide a validated explicit database path input when discovery cannot be
established; do not guess a Linux location. Keep absent DB optional, read-only
imports conservative and full-ingest-only behavior (ADR 0013).

(inferred) Native Linux is a full target, not merely preliminary WSL coverage.
Reuse POSIX flock, 0700/0600 private state, safe-open flags, shell launchers,
current/python symlink release selection and existing transaction/rollback
steps. Keep macOS on launchd. Extend the shared installer/doctor boundaries
with a small Linux lifecycle adapter using a systemd user service, reused in
WSL when its user manager is working. This changes `install/context.py`,
`constants.py`, `preflight.py`, `steps_release.py`, `snapshot.py`, `rollback.py`,
`uninstall.py`, `verify.py`, `muninn/obs.py` and Linux integration templates;
keep native Windows release selection separate from the POSIX symlink model.

(inferred) Probe user-manager availability, registration, process identity,
restart-on-failure and heartbeat in native Ubuntu and WSL separately.
Distinguish systemd availability from login/user-service startup and distro
lifetime. Give foreground `serve` / manual `ingest` instructions when the
manager is unavailable; do not mark automatic indexing installed/healthy in
that state. The initial managed mode covers an active user session, not
machine-wide/pre-login service. No automatic systemd, WSL, distro or linger
enablement during installation. Preflight refuses unsupported managed lifecycle
without writing; manual runtime use has its own documented capability status.

(inferred) Keep WSL state/releases inside the Linux filesystem; do not share
an SQLite database, release pointer or tombstone key with Windows. Run WSL
providers and their hooks inside that distribution for the first integration
claim. Native Windows providers plus WSL Muninn require a separately designed
path/shell bridge and are deferred. Windows-mounted source roots may be
opt-in only after read/scope tests; they do not imply shared state support.

**Exit:** each provider/environment has its own acceptance result. Missing
provider is optional; unverified provider is explicitly pending. Native
Linux fresh/check/status/upgrade/schema rollback/prune/uninstall-retain pass
with POSIX permissions and the user-service adapter; manual CLI and managed
polling are documented separately for native Linux and WSL.

### 5. Complete gates, documentation and release evidence

(inferred) Adapt `tools/check.py` tool lookup for `.venv/Scripts`, Windows
executable names and worktrees. Keep stdlib rules, ruff, black, pyright,
shellcheck, hook configuration and tests mandatory. Existing POSIX scripts
still require shellcheck even on Windows; supply a pinned usable checker or
fail the full gate. Validate Git hook execution via Git for Windows and make
Codex/Claude project Stop commands portable without enabling/trusting hooks
on the user's behalf. Any enforcement change needs the ADR 0006 amendment;
no broad platform skips or “missing tool = success” shortcut.

(inferred) Replace shell/macOS-specific fixture commands with Python/Runner
fakes where they test shared behavior; retain true OS-specific suites for
modes/ACLs, signals/native stop, safe-open, durability and scheduler. Both
implementations must have meaningful tests; a mocked OS branch is additional
coverage, never a substitute for running it. Keep tests inside temporary
homes and never use provider transcripts as test fixtures.

(inferred) Publish eventual status by capability/environment in `README.md`,
`docs/QUICKSTART.md`, `docs/REFERENCE.md`, `docs/TROUBLESHOOTING.md`,
`docs/ARCHITECTURE.md`, `docs/STANDARDS.md` and relevant ADR additions.
Synchronize both `.claude/skills/muninn-*` and `.codex/skills/muninn-*` for
changed command flags or answer fields. Include native commands, prerequisites,
private-directory restrictions, WSL service conditions and evidence limits.

**Exit:** the final macOS, native Linux and Windows gates pass on the release SHA, actual
platform/provider acceptance is attached, and documentation states exactly
which capabilities have passed. WSL support requires WSL results, not Ubuntu
CI results alone.

## Hosted validation without user-provided machines

(verified) No `.github/` directory or workflow is tracked in the assessed base.
(inferred) One proposed GitHub Actions workflow uses separate OS jobs in a
standard hosted runner matrix: `macos-15` for the existing full gate and
macOS regressions, `ubuntu-24.04` for native Linux x64 full-gate and service
adapter checks, `windows-2022` for Windows Server x64 native runtime checks,
and `windows-11-arm` for Windows 11 ARM64 native runtime checks. Every target
must pass its complete gate before its support claim; early Windows primitive
jobs are preparatory evidence. Linux CI is required for native Linux support.
GitHub provides these ephemeral machines; no user-provided or self-hosted machines
are needed. Windows 11 x64 desktop/provider acceptance remains separate.
The bootstrap workflow is `.github/workflows/platforms.yml`: macOS and
Linux run the mandatory full gate, while explicitly named Windows
environment-readiness jobs verify Python 3.13 and SQLite FTS5 before the
runtime port is available. Windows readiness does not establish Muninn
support. Repository Actions enablement and branch publication are authorised.

(verified) Standard x64 `windows-2022`/`windows-2025` runners use Windows
Server images. Passing there proves behavior on that image; it does not
prove Windows 11 desktop/provider behavior. GitHub lists `windows-11-arm`
as a standard Windows 11 ARM64 runner for public/private repositories; it can
provide actual Windows 11 runtime evidence on ARM64. It cannot establish
Windows 11 x64 or interactive provider compatibility.
Sources: [runner images](https://github.com/actions/runner-images),
[hosted runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

(inferred) Start with manual dispatch for the proposed macOS/Linux/Windows
matrix. Keep macOS and Linux full gates mandatory during implementation;
run Windows primitives before expanding to its full gate and acceptance checks.
Use explicit labels and one Python 3.13 interpreter, bounded runtime, no service credentials or
real transcripts, no large artifacts/caches, minimal read-only permissions,
and short retention for count/code-only results. Run primitives before the
whole suite; after it is useful, add path-filtered PR execution with
concurrency cancellation. Pin actions to reviewed commit SHAs and record
OS/Python/SQLite versions, release SHA, actual executed checks and any skips.
Do not treat the administrator identity of a hosted runner as proof of the
normal-user/no-symlink-privilege contract. Test that contract explicitly.

(verified) GitHub says standard hosted Actions runners are free for public
repositories. Private repositories consume the account's included minutes
and may incur charges beyond them; larger runners are billed separately.
(verified) A live repository query confirmed [Muninn](https://github.com/blueman82/muninn)
is PUBLIC on 2026-10-07. Standard `macos-15`, `ubuntu-24.04`, `windows-2022`
and `windows-11-arm` runner minutes therefore have zero runner charges under the documented public-repo
rules. Windows environment-readiness workflow activation is now authorised.
Other account usage/payment settings
and budget status were not inspected; do not infer them from visibility.
Sources: [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions),
[runner pricing](https://docs.github.com/en/billing/reference/actions-runner-pricing).

(inferred) Recheck public visibility before enabling CI and use only standard
runners; keep artifacts minimal with short retention. If visibility changes
to private, verify included allowance and cost controls before running.
For private usage, if there is no valid payment method, GitHub says
usage stops when the allowance is exhausted; if payment exists, use a hard
Actions budget with “Stop usage when budget limit is reached” selected before
running, because an alert-only budget does not cap spend. Do not change
billing settings automatically. If zero-spend operation cannot be established,
keep the Windows job inactive and the platform claims pending.
Source: [budgets and alerts](https://docs.github.com/en/billing/concepts/budgets-and-alerts).

Unknown: WSL availability/reboot/virtualization capabilities on a suitable
hosted runner. Ubuntu CI is useful Linux coverage but is not WSL acceptance.
(inferred) Obtain WSL evidence later from a verified CI host or an external
volunteer using the synthetic acceptance script; similarly obtain Windows 11
x64 desktop/provider evidence without requiring the owner to supply a machine.
If neither is available, ship only the proven experimental capabilities and
keep desktop/WSL integration support unclaimed.

### Native Windows validation matrix

All rows are (inferred) required future checks; none has run in this task.

| Check | Hosted Windows synthetic evidence | Windows 11 x64 acceptance |
|---|---|---|
| Runtime/CLI | Native CPython imports on Server x64 and Windows 11 ARM64, JSON/UTF-8, quoting/exit codes; no Unix shell dependency | PowerShell and cmd, user home with spaces/Unicode |
| Lock/store/erase | Competing/crashed writers; FTS, journals, key race/persistence, erase residue | Local NTFS; second unprivileged account denied state access |
| Privacy/read boundary | Existing unsafe/custom DACL refusal; reparse/junction/path-swap/device checks | Normal user without elevation or Developer Mode; user-config ACL retention |
| File publication | Concurrent reader sharing failures, interrupted rebuild/config/pointer/snapshot restore | Real filesystem/security tooling behavior; old state survives refusals |
| Installer/scheduler | Fakes at every failure boundary, plus native scheduler API smoke checks where permitted | Fresh/upgrade, logon restart, graceful stop, disabled task, crash recovery, prune |
| Provider adapters | Synthetic native transcript/DB and hook execution; config/trust/cache tests | Separate installed Claude/Codex hook acceptance; Cursor read-only import |
| Gate | All required tools and shared plus Windows-specific tests | Approved project hooks when supported; manual full gate otherwise |

### Native Linux validation matrix

All rows are (inferred) required future checks; none has run in this task.
Hosted Ubuntu can provide native Linux runtime evidence. A missing hosted
user manager is not a passing service result; obtain an environment with a
working user manager for managed-service acceptance.

| Check | Ubuntu 24.04 x64 synthetic evidence | Native Linux acceptance |
|---|---|---|
| Runtime/CLI | Python 3.13, SQLite FTS5, JSON/UTF-8, full command suite | Version/feature checks, shell invocation, space/Unicode homes |
| Lock/store/erase | flock contention/crash recovery, hot journal, sync, key publication/persistence, FTS/byte residue | Local ext4; privacy and old-state preservation on failure |
| Privacy/read boundary | 0700/0600 under permissive umask; symlink/FIFO/root escape tests | Normal user, existing unsafe-home behavior, second-user denial |
| Installer/service | Existing Runner fakes plus Linux fresh/upgrade/snapshot/rollback/prune cases | Actual systemd user service start/stop/restart/heartbeat; user manager absent/present; no real-HOME mutations in automated tests |
| Provider adapters | Synthetic Linux transcripts/hooks/explicit Cursor DB input | Installed Linux Claude/Codex homes/trust/cache; confirmed read-only Cursor discovery or explicit path |
| Gate | Mandatory full gate with required tools and no broad Linux skips | Approved provider/project hooks where supported; manual full gate otherwise |

### WSL validation matrix

All rows are (inferred) required future checks; none has run in this task.

| Check | Linux CI preliminary coverage | Real WSL 2 acceptance |
|---|---|---|
| Runtime/CLI | Python 3.13, flock, sync, SQLite, search/erase/rebuild | Same checks in selected distribution on Linux filesystem |
| Installer/service | Fake systemd user lifecycle and rollback tests | User manager present/absent, login/distro shutdown/restart, poller heartbeat |
| Privacy/storage | POSIX 0700/0600, journal/key/snapshot checks | Linux state private; unsafe Windows-mounted state refused |
| Provider hooks | Synthetic Unix config/transcripts and command execution | WSL-native providers, paths/trust/encoding, no assumed native-provider bridge |
| Gate | Full Linux gate with required tools | Full gate in WSL; Windows 11 host behavior recorded separately |

## Success and adversarial planning

### Stage 1: Pre-Parade

(inferred) Success scenario: Windows 11 and native Linux colleagues can
install, index, search and erase without losing privacy; a WSL colleague
can identify whether automatic indexing is
available, while macOS remains green.

1. OS primitives preserve writer/privacy/durability guarantees — missing;
   prove first, before building lifecycle around them.
2. Existing macOS behavior and ADR contracts remain intact — current source
   provides the contracts; regression evidence for future changes is missing.
3. Each installer/provider is actually exercised — missing; synthetic hosted
   tests are only the first layer, with desktop/WSL acceptance still needed.
4. Windows CI cost is known before activation — public standard runners are free under
   current rules; recheck visibility and use a hard cap if private billing
   becomes relevant.
5. CLI-only, managed polling and provider support have distinct truthful
   statuses — missing; add the capability/evidence documentation at release.

### Stage 2: Premortem

(inferred) Failure scenario: Windows imports work, but a user's upgrade or
erase fails while documentation has already promised support.

| Failure | Likelihood / impact | Mitigation |
|---|---|---|
| POSIX modes were treated as Windows ACLs | High / transcript or key disclosure | Private-at-creation experiment; DACL verification and second-account denial tests before writes. |
| Open readers or scheduler termination break store replacement/rollback | Medium / lost ledger or unusable store | Actual sharing/crash tests; quiesce owned worker, retain old state and fail boundedly. |
| Windows Server/fake hooks are mistaken for Windows 11/WSL/provider acceptance | High / install works in CI only | Separate matrices; keep unsupported acceptance cells pending. |
| Hosted CI costs exceed the expected free allowance | Low while public; higher if visibility changes / unexpected bill | Standard runners only; verify allowance and hard cap before activation. |

(inferred) Verdict: a staged plan is viable if privacy and publication remain
release blockers; “port imports first and declare support” has a structural gap.

### Stage 3: Red Team

(inferred) Adversary: a skeptical Windows user and a local process attempting
to exploit path/config races.

1. “Your CI account is admin on Windows Server.” Install as a normal Windows
   11 user with symlink privileges absent; prove Task Scheduler and hook
   invocation rather than asking users to enable Developer Mode.
2. Swap a transcript path for a junction/device or broaden an existing data
   DACL. Prove handle/root validation and private-state refusal instead of
   silent flag fallbacks or trusting a pathname check.
3. Hold a database open during upgrade, crash the worker and reject task
   registration. Prove the old store/key/release remain usable, and report
   which checks are unknown when the new install cannot be verified.

(inferred) Challenge: provide native primitive evidence, normal-user desktop
acceptance and explicit WSL service/provider evidence before making a complete
support claim.

(inferred) Synthesis: privacy, publication and honest acceptance boundaries
are the shared risks. The success pass also requires predictable CI cost and
continued macOS behavior. The first priority is the small real-Windows
primitive spike; if it fails, revise the platform contract before implementing
the installer. No evidence from this planning task establishes native
Windows, native Linux or WSL runtime support.

## Did Not Verify

No Windows, native Linux, WSL, Task Scheduler, systemd, native provider, ACL enforcement,
crash durability, real installer, hosted CI run, repository billing settings,
project hook approval or colleague install was exercised. The plan proposes
future validation and does not authorize changes to existing ADR decisions.
