# Windows maintenance recovery qualification

Windows maintenance recovery remains unqualified. The runtime continues to
refuse `muninn recover` when Windows repair is required. A passing readiness
probe does not change that behavior.

## Native CI refusal and preservation checks

`tests.test_windows_recovery_refusal` runs actual CLI subprocesses on Windows
x64 and ARM. Both proof lanes run the module explicitly before lifecycle
checks. The fixture uses a private temporary home, a populated current
database and retained database archive, and an existing tombstone key.
Windows user, application-data, temporary and provider paths are isolated.
The tests do not mock boot observations, platform selection, ACLs or locks.

Six cases check distinct outcomes:

| Fixture state | Required result |
|---|---|
| Healthy complete witness matching the lock | No recovery needed |
| Unapplied thread and keyed erasure intent | Windows repair unqualified |
| Exited fixture owner and synthetic Linux baseline | Windows repair unqualified |
| Live fixture owner retaining its lifetime lock | Pause unverified; owner remains alive and locked |
| Malformed baseline hard-linked to the test key | Pause unverified; key preserved |
| Public read access on the witness | Pause unverified; unsafe ACL preserved |

The live-owner case checks the observed state-verification refusal. It does
not identify which native inspection refused or establish repair readiness.

Before and after the command, the tests compare file identities and content
hashes, populated database rows, pending intent and the exact material file
set. Only the named, content-free `calls.jsonl` append is permitted.
The exact lifetime-lock file instead requires zero length and unchanged
identity while its byte range is locked; all other material is read and
hashed. A nonempty lifetime lock fails the check. Child
cleanup uses the retained fixture process handle and bounded waits. Token
elevation codes are observations about the CI account, not desktop proof.

These checks measure refusal and preservation on hosted runners. They do
not implement repair or prove a successful recovery, boot transition,
crash-durable retirement or ordinary non-admin desktop behavior. The Linux
baseline is synthetic input; it is not a Windows boot observation. The
hard-link case checks malformed-state refusal, not a retirement algorithm.
Qualification flags remain false. A local non-Windows skip supplies no
native evidence; acceptance requires actual x64 and ARM execution with raw
logs and tested-source bindings. No personal Windows machine is assumed.

## Native boot-counter readiness probe

`tests.windows_recovery_probe` reads `BootId` from the 64-bit registry view at
`HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters`.
It requests query access only. It does not write registry values, stop a
service, reboot, read provider transcripts, or open the real Muninn store.

Microsoft describes this counter as incrementing on each successful boot:
[Memory integrity and VBS enablement](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/oem-hvci-enablement).
That description identifies a candidate observation. It does not establish
all of the recovery safety conditions below.

The Windows x64 and ARM proof lanes run three observations in one process
and two observations in fresh processes. All must be exact DWORD integers
and equal. Missing keys, denied access, malformed values, disagreement,
and failed or timed-out child processes fail the probe. There is no time,
PID, or randomly generated identifier fallback.

The native report is `muninn-native-diagnostics/muninn-recovery-probe.json`
under the isolated CI scratch directory. It records the commit, architecture,
counter, sample counts, and finite failure code. `qualified`, `reboot_proof`,
`desktop_proof`, and `retirement_proof` remain false.

The counter has no cross-host binding in this report and must not be used as repair authority.
New native proof uploads request 30-day retention. Check the actual API `expires_at`;
repository policies can limit the request, and older artifacts keep their original
expiry. For evidence needed longer, copy the original ZIP, SHA256/API digest, source
commit and run/job provenance into the campaign evidence directory before expiry.
These archives do not include raw job logs, and retention does not qualify recovery.

## Evidence required before enabling recovery

- A native controlled-reboot observation bound to the same host and repair
  files, with same-boot refusal before the reboot.
- Separate sleep, hibernation, Fast Startup, restore, counter reset and
  wraparound controls. A changed observation must never admit surviving
  predecessor processes.
- Verify when the registry counter is published, including late boot and
  service restarts. An update while predecessor processes survive must not
  become permission to repair.
- An available and well-formed observation on supported x64 and ARM hosts.
  Unavailable or ambiguous observations must refuse without changing evidence.
- Native file-identity, ACL, reparse and replacement controls for the lock,
  witness and baseline, with owner ownership acquired before writer ownership.
- Durable retirement of old repair authority, including interrupted replay,
  witness publication and retirement. A deletion-on-close result alone does
  not prove durable directory removal.
- Pending erasure replay and preservation of the tombstone key, current
  database and retained archives.
- An ordinary non-admin desktop account qualification. Hosted CI and a
  process with a filtered admin token do not establish this condition.

The readiness probe performs none of the boot transitions above. The agent
must not reboot a machine to remove a release blocker. Host access and
person-operated transitions require separate coordination with the owner.
