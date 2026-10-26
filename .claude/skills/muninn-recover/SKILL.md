---
name: muninn-recover
description: Resume a verified interrupted Muninn maintenance pause with `muninn recover`. Use when doctor reports recovery guidance or a maintenance command reports `reboot_required`.
---

# muninn recover

    muninn recover

Use only when the owner asks for recovery or `doctor` returns
`recovery_guidance`. The command starts no new erase request. It may apply
erasure intent saved in `tombstones.jsonl`. It waits for the managed writer.
It repairs pause records only after the existing checks pass.
Doctor's `baseline_present_recovery_unverified` and `recovery_check_required`
report presence and a next step, not validated records or successful repair.
Malformed or unsafe records can cause recovery to refuse.
During an upgrade, the installer asks once about a matching older macOS record without a disk ID.
The question explains that Muninn cannot verify the disk identity.
Continue only if this is the Muninn data you intend to recover.
The installer repeats its checks before repair.
For direct recovery, use `--confirm-legacy-volume` only after the command reports
`legacy_volume_confirmation_required` and you confirm the data.

## Steps

1. Run `muninn doctor --pretty`.
2. Read `recovery_guidance` in the result.
3. On macOS or Linux, run `muninn recover`.
4. If recovery reports `legacy_volume_confirmation_required`, check the Muninn data path.
5. Run `muninn recover --confirm-legacy-volume` only for the correct data path.
6. If recovery returns `reboot_required`, restart the computer.
7. Run `muninn recover` again.
8. If recovery returns `recovered`, run `muninn doctor --pretty`.
9. If doctor reports a stopped service, start it through the system service manager.
10. If recovery refuses, read `why` and `next_action`.
11. If it asks for help, keep the data folder and recovery files unchanged.
12. On Windows, run `muninn recover` to read its refusal result.
13. On Windows, preserve all repair files and save the doctor report.
14. Contact the maintainer because Windows repair is not qualified.

Do not remove or rename witness, baseline, tombstone, key or archive files.
`recover` does not restart the computer or service. The system service manager
controls service startup after recovery.
