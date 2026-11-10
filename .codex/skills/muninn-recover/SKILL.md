---
name: muninn-recover
description: Resume a verified interrupted Muninn maintenance pause with `muninn recover`. Use when doctor reports recovery guidance or a maintenance command reports `reboot_required`.
---

# muninn recover

    muninn recover

Use this command only with an owner instruction for recovery or when `doctor` reports `recovery_guidance`.
It starts no new erase `request`.
It can apply erasure intent recorded in `tombstones.jsonl`.
It waits for the managed writer.
It repairs pause records only after the current checks pass.

Doctor's `baseline_present_recovery_unverified` and `recovery_check_required` report that a repair path exists and give the next step.
They do not prove correct records or a completed repair.
Malformed records, identity errors, or privacy errors can cause refusal.

During an upgrade, the installer has one confirmation step for a matching legacy macOS record without a disk ID.
The prompt says that Muninn cannot check disk identity.
Continue only for the intended Muninn data.
The installer repeats its checks before repair.

For direct recovery, use `--confirm-legacy-volume` only after the command reports `legacy_volume_confirmation_required` and you give explicit disk confirmation.

Completed macOS records with a matching volume UUID and lock inode need no new boot after a device-number change.
A legacy completed record has no UUID.
Its migration needs explicit disk confirmation through `--confirm-legacy-volume`, after the command reports `legacy_volume_confirmation_required`.
Your confirmation supplies disk identity the record cannot prove.

Recovery checks the record, inode, and repair fingerprint while it holds the owner lock and writer lock.
It applies recorded tombstones before publication.
Active owners and changed records keep their refusal rules.

Before runtime selection, the installer keeps legacy output.
The selected runtime adds the UUID at startup.
Rollback after selection keeps that runtime and keeps the managed writer stopped.

Do not manually select a previous release that cannot read the UUID field.
A SQLite copy does not include the supervisor JSON record.

## Steps

1. Run `muninn doctor --pretty`.
2. Read `recovery_guidance` in the result.
3. On macOS or Linux, run `muninn recover`.
4. If recovery reports `legacy_volume_confirmation_required`, check the Muninn data path.
5. Run `muninn recover --confirm-legacy-volume` only for the correct data path.
6. If recovery reports `reboot_required`, stop and start the computer.
7. Run `muninn recover` again.
8. If recovery reports `recovered`, run `muninn doctor --pretty`.
9. If doctor reports a stopped service, start it through the system service manager.
10. If recovery refuses, read `why` and `next_action`.
11. If it reports a need for help, keep the data folder and recovery files unchanged.
12. On Windows, run `muninn recover` to read its refusal result.
13. On Windows, keep all repair files and the doctor report.
14. Contact the maintainer because Windows repair has no native qualification.

Do not remove or rename witness, baseline, tombstone, key, or archive files.
Muninn does not reboot the computer or start the managed service after recovery.
The system service manager controls service startup after recovery.
