# Windows maintenance recovery qualification

Windows maintenance recovery remains unqualified. The runtime continues to
refuse `muninn recover` when Windows repair is required. A passing readiness
probe does not change that behavior.

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
The existing CI artifact expires after one day. Copy its original bytes
and run/job provenance into the campaign evidence directory before expiry.

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
