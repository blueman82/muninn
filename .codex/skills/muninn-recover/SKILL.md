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

## Steps

1. Run `muninn doctor --pretty`.
2. Read `recovery_guidance` in the result.
3. On macOS or Linux, run `muninn recover`.
4. If it returns `reboot_required`, restart the computer.
5. Run `muninn recover` again.
6. If it returns `recovered`, run `muninn doctor --pretty`.
7. If doctor reports a stopped service, start it through the system service manager.
8. If recovery refuses, read `why` and `next_action`.
9. If it asks for maintainer help, keep the data folder and recovery files unchanged.
10. On Windows, run `muninn recover` to read its refusal result.
11. On Windows, preserve all repair files and save the doctor report.
12. Contact the maintainer because Windows repair is not qualified.

Do not remove or rename witness, baseline, tombstone, key or archive files.
`recover` does not restart the computer or service. The system service manager
controls service startup after recovery.
