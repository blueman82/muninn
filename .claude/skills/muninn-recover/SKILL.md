---
name: muninn-recover
description: Resume a verified interrupted Muninn maintenance pause with `muninn recover`. Use when doctor reports recovery guidance or a maintenance command reports `reboot_required`.
---

# muninn recover

    muninn recover

Use only when the owner asks to recover Muninn or `doctor` returns
`recovery_guidance`. The command starts no new erase request. It may apply
erasure intent already saved in `tombstones.jsonl`. It waits for Muninn's
managed writer and uses the existing checks before it repairs pause records.

## Steps

1. Run `muninn doctor --pretty` and read `recovery_guidance`.
2. On macOS or Linux, run `muninn recover`.
3. If the result says `reboot_required`, restart the computer and run
   `muninn recover` again. Muninn cannot reboot a computer for its owner.
4. If the result says `recovered`, run `muninn doctor --pretty` to check the
   service and store. The command may have stopped a managed writer while it
   checked recovery. If doctor says the service is stopped, start it through
   the system service manager; `recover` does not restart it.
5. If it refuses, read its `why` and `next_action`. Keep the data folder and
   every recovery file unchanged when it asks you to contact the maintainer.
6. On Windows, recovery is not qualified. The command refuses before changing
   recovery files because Windows boot checks and repair-file retirement have
   not passed native qualification. Keep them unchanged, save the report, and
   contact the maintainer.

Do not remove or rename witness, baseline, tombstone, key, or archive files to
clear a refusal. Muninn does not restart the operating system. The system
service manager controls service startup after recovery.
