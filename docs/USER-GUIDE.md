# Muninn user guide

This guide is for you if you use Muninn with Claude Code or Codex. It shows you how to use Muninn. It also tells you what to do when Muninn shows a message.

Run each command in a terminal. On Windows, use `bin\muninn-install.cmd` where this guide shows `bin/muninn-install`.

## Check that Muninn works

Muninn has a health check. It looks at the memory database, the background service and the hooks. A hook is a small command that Claude Code or Codex runs when a session starts or ends.

To check Muninn, do these steps:

1. Run `muninn doctor --pretty`.
2. Look for a check that shows `"ok": false` and `"level": "error"`.
3. If you find none, Muninn works.
4. If you find one, send the full output to the person who looks after your Muninn install.

A check at the level `warn` or `info` does not mean that Muninn failed.

## See your memories

Muninn keeps short notes from your chats. These notes are your memories. Muninn shows the memories of the project in your current folder.

To see your memories, do these steps:

1. Open a terminal in the folder of your project.
2. Run `muninn know list`.
3. Find the ID of a memory in the list. It starts with `K_`.
4. Run `muninn know show K_ID`, with your ID, to read the memory.

If the list is empty, Muninn has no memory for this project. Learning can add memories. See [Turn learning on](#turn-learning-on).

To see the memories of all your projects, add `--all-projects` to the command in step 2. To see what Muninn gives Claude Code or Codex at the start of a session, run `muninn know context`. To find an earlier chat, run `muninn search "words from the chat"`.

## Turn learning on

Learning lets Muninn read your finished chats and write memories from them. Muninn sends parts of your chats to Codex to do this.

To use learning, you must have these three items:

- A Mac with Apple silicon.
- Codex, which you can run in a terminal.
- A Codex sign-in.

If you do not have all three, learning stays off.

To turn learning on, do these steps:

1. Open a terminal in the folder that holds your Muninn files.
2. Run `bin/muninn-install --check`, and read the plain sentences that it shows.
3. Run `bin/muninn-install --enable-learning`.
4. Answer the questions on the screen.
5. If the installer prints `OWNER STEP`, start Codex.
6. Run `/hooks` in Codex.
7. Trust the Muninn hooks.
8. Run `muninn learn list` to see your learning tasks.

Run `bin/muninn-install --enable-learning` only after step 2. This command also upgrades Muninn to the version in your folder.

To learn only from new chats, add `--learning-scope now` to the command in step 3.

Muninn starts to learn from your chats. New memories appear in `muninn know list`. If a learning task stops, see [Fix a problem](#fix-a-problem).

## Upgrade Muninn

Upgrade when you want the newest version of Muninn. The upgrade keeps your memories and your settings.

To upgrade, do these steps:

1. Open a terminal in the folder that holds your Muninn files.
2. Run `git status`, and make sure that it shows no changed files.
3. Run `git pull` to get the newest version.
4. Run `bin/muninn-install --status` to see if your installed version is older.
5. Run `bin/muninn-install --check`, and read the plain sentences that it shows.
6. Run `bin/muninn-install`.
7. Run `muninn doctor --pretty`, and make sure that it shows no error.

If the installer tells you to start the computer again, do that. Then run the same command again. If the installer prints `FAILED:` and a code, find the code in [Fix a problem](#fix-a-problem).

## Fix a problem

Muninn shows a code or a message when you must do something. Find the code or the message in this section. Do the steps of that entry.

### Get help when an upgrade cannot finish

`legacy_candidate_changed` and `review_activation_configuration_changed`

- **What you see:** `bin/muninn-install` stops with `FAILED: legacy_candidate_changed` or `FAILED: review_activation_configuration_changed`.
- **What caused it:** an earlier install started to turn on learning, and it did not finish. Each new install tries to finish it first. It must use the same Muninn version and the same settings files as before. Your version or your settings files changed, so the install stops.
- **What happens if you do nothing:** you cannot install a new version of Muninn. Your installed Muninn continues to work, and learning stays as it was.
- **How to repair it:** you can leave the unfinished install when it changed nothing that must be undone. Muninn examines this first. It keeps the install records, and it removes no file. Do not edit or remove the install records.
- **Steps:**
  1. Run `bin/muninn-install --leave-unfinished --check`, and read the plan.
  2. Run `bin/muninn-install --leave-unfinished`, and type `y`.
  3. Run `bin/muninn-install`.
  4. If a command stops with `FAILED:`, run `bin/muninn-install --check` and `muninn doctor --pretty`.
  5. Send the two outputs to the person who looks after your Muninn install.
- **Result:** the new install continues. If Muninn cannot leave the unfinished install, that person can repair the install safely.

### Find out why a learning task stopped

Muninn learns from your chats in small tasks. When a task stops, Muninn shows a short code.

To see the code and what it means, do these steps:

1. Run `muninn learn list --state failed`.
2. Find the task in the list, and write down its ID. The ID starts with `R_`.
3. Run `muninn learn explain R_ID --human`, with your ID.
4. Find the code in this guide, and do the steps for that code.

### Find out what a learning notice means

At the start of a Claude Code or Codex session, Muninn can show a line that starts with `Muninn learning:`. This line is a notice. It tells you that learning has a problem.

To find out what a notice means, do these steps:

1. Read the line that starts with `Muninn learning:`.
2. If the line has an ID that starts with `R_`, run `muninn learn explain R_ID --human`, with that ID.
3. If the line has no ID, run `muninn doctor --pretty`.
4. Find the code or the words of the notice in this guide, and do the steps of that entry.

To hide a notice, run `muninn learn dismiss R_ID`, with the ID of the task. This does not repair the problem. If a notice says that learning stays paused, repair the problem first. Then turn learning on again.

### Fix a clock error

`budget_clock_reversed`

- **What you see:** a learning task stopped with the code `budget_clock_reversed`.
- **What caused it:** the clock of your computer went back to an earlier time. Muninn uses the clock to measure how long each task continues.
- **What happens if you do nothing:** Muninn cannot measure the time correctly. If you start the same task again, it stops again.
- **How to repair it:** set your computer to get the correct time automatically.
- **Steps:**
  1. On macOS, open System Settings, then General, then Date and Time.
  2. Turn on "Set time and date automatically".
  3. On Linux, run `timedatectl set-ntp true`.
  4. On Windows, open Settings, then Time and language, then Date and time, and turn on "Set time automatically".
  5. Run `muninn doctor --pretty`, and make sure that it shows no error.
- **Result:** Muninn measures the time correctly again, and new learning tasks can run.

### Read a changed chat again

`incomplete_window`

- **What you see:** a learning task stopped with the code `incomplete_window`.
- **What caused it:** Muninn started to learn from a part of a chat. Then the chat file changed, and that part is not complete.
- **What happens if you do nothing:** Muninn does not learn from that part of the chat.
- **How to repair it:** tell Muninn to read your chat files again.
- **Steps:**
  1. Run `muninn ingest`.
  2. If the code comes back, run `muninn ingest --full`.
- **Result:** Muninn has the full chat again. It learns from the next part of the chat as usual.

### Start a task again after a rule change

`review_policy_required`

- **What you see:** a learning task stopped with the code `review_policy_required`.
- **What caused it:** the learning rules of Muninn changed while the task waited in the queue.
- **What happens if you do nothing:** Muninn does not learn from that chat.
- **How to repair it:** make a new task. Muninn then uses the new rules.
- **Steps:**
  1. Run `muninn sessions`, and find the chat in the list.
  2. Write down its name. It starts with `claude:` or `codex:`.
  3. Run `muninn learn request --session REF`, with that name.
- **Result:** Muninn learns from the chat with the current rules.

### Do nothing when a different worker took a task

`rejected_fenced`

- **What you see:** a learning task shows the code `rejected_fenced`.
- **What caused it:** two parts of Muninn worked on the same task. The second part took control of the task.
- **What happens if you do nothing:** no problem occurs. Muninn keeps only the result of the second part.
- **How to repair it:** you do not have to do anything.
- **Steps:** none.
- **Result:** Muninn keeps one correct result for the task.

### Do nothing about a read limit

`evidence_limit`

- **What you see:** a learning task shows the code `evidence_limit`. Muninn can also write this code in its log when it skips a part of a chat.
- **What caused it:** a part of your chat was larger than the read limit of Muninn. Now Muninn makes each task small enough to read, or it skips a part that is too large.
- **What happens if you do nothing:** no problem occurs. Muninn learns from the other parts of the chat. A task that stopped with this code before an upgrade stays stopped.
- **How to repair it:** you do not have to do anything.
- **Steps:** none.
- **Result:** Muninn continues to learn from your chats.

### See which read limit skipped a part of a chat

`evidence_byte_limit`, `evidence_record_limit`, `evidence_work_limit` and `evidence_selection_limit`

- **What you see:** a learning task or a log line shows the code `evidence_byte_limit`, `evidence_record_limit`, `evidence_work_limit` or `evidence_selection_limit`.
- **What caused it:** a part of your chat was larger than one of the read limits of Muninn. The four limits count bytes, lines, database work and selected text. The code names the limit.
- **What happens if you do nothing:** no problem occurs. Muninn cuts a large part to fit, or it skips that part one time. If a task stops with one of these codes, Muninn does not learn from that part only. Muninn learns from the other parts of the chat.
- **How to repair it:** you do not have to do anything.
- **Steps:** none.
- **Result:** Muninn continues to learn from your chats.

### Tell your maintainer about a size limit

`prompt_limit`

- **What you see:** a learning task stopped with the code `prompt_limit`.
- **What caused it:** the text that Muninn must send for the task was larger than its limit.
- **What happens if you do nothing:** Muninn does not learn from that part of the chat. If you start the same task again, it stops again.
- **How to repair it:** you cannot repair this. The person who looks after your Muninn install must check the limit.
- **Steps:**
  1. Run `muninn learn explain R_ID --human`, with the ID of the task.
  2. Send the output to the person who looks after your Muninn install.
  3. Do not start the same task again. It stops again with the same code.
- **Result:** that person can make a decision about the limit.

### Start a stopped task again after a time-out

`deadline`, `unavailable` and `rejected_revision`

- **What you see:** a learning task stopped with the code `deadline`, `unavailable` or `rejected_revision`.
- **What caused it:** for `deadline`, the task used more time than its limit. For `unavailable`, Codex did not finish the task. For `rejected_revision`, your memories changed while the task ran.
- **What happens if you do nothing:** Muninn tries the task again by itself. After its last try, Muninn stops the task. Muninn then does not learn from that part of the chat. Other tasks continue.
- **How to repair it:** start the stopped task again by hand.
- **Steps:**
  1. Run `muninn learn list --state failed`, and write down the ID of the task.
  2. Run `muninn learn retry R_ID`, with your ID.
  3. If the task stops again, run `muninn learn explain R_ID --human`.
  4. Send the output to the person who looks after your Muninn install.
- **Result:** Muninn tries the task again. If it works, Muninn learns from that part of the chat.

### Find a chat again

`source_unavailable`

- **What you see:** a learning task stopped with the code `source_unavailable`.
- **What caused it:** Muninn cannot find the chat of the task in its index. The index is the list of chats that Muninn has read. The chat file can be moved, changed or erased.
- **What happens if you do nothing:** Muninn does not learn from that chat. If you start the same task again, it stops again.
- **How to repair it:** tell Muninn to read your chat files again. If the chat file is gone, you cannot repair this.
- **Steps:**
  1. Run `muninn ingest`.
  2. Run `muninn sessions`, and find the chat in the list.
  3. If you find the chat, write down its name. It starts with `claude:` or `codex:`.
  4. Run `muninn learn request --session REF`, with that name.
  5. If you do not find the chat, run `muninn doctor --pretty`.
  6. Send the output to the person who looks after your Muninn install.
- **Result:** Muninn can learn from the chat again.

### Do nothing when Muninn rejects a result

`rejected_authority`, `rejected_validation` and `rejected_scope`

- **What you see:** a learning task shows the code `rejected_authority`, `rejected_validation` or `rejected_scope`.
- **What caused it:** Codex proposed a new memory, and a safety check refused it. For `rejected_authority`, Muninn found no proof that you said it. For `rejected_validation`, the result had an incorrect format. For `rejected_scope`, the memory was not in the project of the task.
- **What happens if you do nothing:** no problem occurs. Muninn drops that result and continues with other tasks.
- **How to repair it:** you do not have to do anything. Do not start the task again. A new try gives the same result.
- **Steps:** none.
- **Result:** Muninn keeps only results that its checks accept. To hide the notice, run `muninn learn dismiss R_ID`, with the ID of the task.

### Do nothing when Muninn skips a task

`rejected_evidence` and `rejected_erased`

- **What you see:** a learning task shows the code `rejected_evidence` or `rejected_erased`.
- **What caused it:** for `rejected_evidence`, the chat text of the task changed, or Muninn cannot find it. Muninn cannot check that text. For `rejected_erased`, an erase record applies to the chat text of the task.
- **What happens if you do nothing:** no problem occurs. Muninn skips the task and continues with other tasks.
- **How to repair it:** you do not have to do anything.
- **Steps:** none.
- **Result:** Muninn does not learn from text that Muninn cannot find or that someone erased.

### Do nothing when Codex changed during a task

`runtime_race_exhausted`

- **What you see:** a learning task shows the code `runtime_race_exhausted`.
- **What caused it:** Codex changed again as the task started. Muninn stopped the task. Muninn did not try again.
- **What happens if you do nothing:** Muninn does not learn from that part of the chat. Other tasks continue.
- **How to repair it:** you do not have to do anything.
- **Steps:** none.
- **Result:** other learning tasks continue.

### Sign in to Codex again

`auth`

- **What you see:** a notice says: "Muninn learning: Codex sign-in is unavailable. Open Codex and sign in. The background program can check again only while running and learning is enabled. Reading this notice starts no timer."
- **What caused it:** Codex has no sign-in on this computer. Muninn cannot start learning tasks without it.
- **What happens if you do nothing:** Muninn starts no new learning task. Tasks in the queue wait.
- **How to repair it:** sign in to Codex.
- **Steps:**
  1. Open Codex.
  2. Sign in to your Codex account.
  3. Wait for Muninn to check again. This can continue for more than one minute.
  4. Run `muninn doctor --pretty`, and make sure that it shows no error.
- **Result:** Muninn checks again, and the notice goes away. Learning tasks start again.

### Get help when setup has no known cause

`config`

- **What you see:** a learning task stopped with the code `config`. A notice can say: "Muninn has no safe cause code for this setup failure."
- **What caused it:** Muninn has a setup problem, but it has no safe code for the cause. Muninn did not keep the cause.
- **What happens if you do nothing:** Muninn cannot learn until setup works. Muninn checks again by itself while learning is on.
- **How to repair it:** you cannot repair this. Send the safe facts to the person who looks after your Muninn install.
- **Steps:**
  1. Run `muninn doctor --pretty`, and copy the output.
  2. If you have the ID of a task, run `muninn learn explain R_ID --human`.
  3. Copy the output of that command.
  4. Send the outputs to the person who looks after your Muninn install.
  5. Do not start the task again to find the cause. Muninn did not keep it.
- **Result:** that person can find the cause.

### Get help when a learning setup file is not correct

`capability_changed`, `capability_unavailable`, `capability_invalid`, `capability_path`, `capability_storage`, `capability_executable_storage`, `capability_executable_provenance` and `capability_limit`

- **What you see:** a learning task stopped with one of the codes in the line above.
- **What caused it:** Muninn checks the files that it uses to start Codex for learning. Muninn cannot find a file, or a file changed, or a file is not safe. Or it is different from the setup that someone approved.
- **What happens if you do nothing:** Muninn starts no new learning task. Tasks in the queue wait. Muninn checks again by itself while learning is on.
- **How to repair it:** at this time, Muninn has no command that puts back the approved files. Do not edit, move or remove them.
- **Steps:**
  1. Run `muninn doctor --pretty`, and copy the output.
  2. If you have the ID of a task, run `muninn learn explain R_ID --human`.
  3. Copy the output of that command.
  4. Send the outputs to the person who looks after your Muninn install.
- **Result:** that person can put back the approved files.

### Get help when a launch file is not correct

`capability_unverified`, `capability_config`, `launch_input_unavailable`, `launch_input_binding`, `launch_input_invalid`, `launch_input_storage`, `native_prompt_instruction` and `runner_unsupported`

- **What you see:** a learning task stopped with one of the codes in the line above.
- **What caused it:** Muninn checks the files that it uses to start Codex for learning. Muninn cannot find a file, or a file changed, or a file is not safe. Or it is different from the setup that someone approved.
- **What happens if you do nothing:** Muninn starts no new learning task. Tasks in the queue wait. Muninn checks again by itself while learning is on.
- **How to repair it:** at this time, Muninn has no command that puts back the approved files. Do not edit, move or remove them.
- **Steps:**
  1. Run `muninn doctor --pretty`, and copy the output.
  2. If you have the ID of a task, run `muninn learn explain R_ID --human`.
  3. Copy the output of that command.
  4. Send the outputs to the person who looks after your Muninn install.
- **Result:** that person can put back the approved files.

### Get help when Codex or your computer differs from the approved setup

`capability_platform`, `launch_input_platform`, `codex_platform_changed`, `codex_host_changed`, `codex_host_identity_unavailable`, `codex_data_home_changed`, `codex_capability_status`, `codex_project_required`, `codex_approval_required`, `codex_experimental_boundary` and `effective_host_changed`

- **What you see:** a learning task stopped with one of the codes in the line above.
- **What caused it:** someone approved learning for one computer, one data folder and one Codex setup. What Muninn finds today is not the same. For `codex_host_changed` and `codex_experimental_boundary`, Muninn does not know why.
- **What happens if you do nothing:** Muninn starts no new learning task. Your memories stay.
- **How to repair it:** you cannot repair this. Do not change the approval files. A change can break the safety check of Muninn.
- **Steps:**
  1. Run `muninn doctor --pretty`, and copy the output.
  2. If you have the ID of a task, run `muninn learn explain R_ID --human`.
  3. Copy the output of that command.
  4. Send the outputs to the person who looks after your Muninn install.
- **Result:** that person can check the approval and decide what to do.

### Get help when Codex cannot start for learning

`runtime_selection_unavailable`, `runtime_selection_unsafe`, `runtime_interface_unsupported`, `runtime_probe_unavailable` and `runtime_structure_limit`

- **What you see:** a learning task stopped with one of the codes in the line above.
- **What caused it:** Muninn cannot use the Codex that is on your computer. For `runtime_selection_unavailable`, Muninn finds no trusted Codex. For `runtime_selection_unsafe`, the Codex install is not safe. For `runtime_interface_unsupported`, this Codex has no learning interface. For `runtime_probe_unavailable`, the safety checks did not finish. For `runtime_structure_limit`, the Codex package is larger than the limit.
- **What happens if you do nothing:** Muninn starts no new learning task. Your memories stay.
- **How to repair it:** you cannot repair this. Send the safe facts to the person who looks after your Muninn install.
- **Steps:**
  1. Run `muninn doctor --pretty`, and copy the output.
  2. If you have the ID of a task, run `muninn learn explain R_ID --human`.
  3. Copy the output of that command.
  4. Send the outputs to the person who looks after your Muninn install.
- **Result:** that person can check which Codex Muninn must use.

### Keep learning off after a safety stop

`native_tool_event` and `process_identity_unverified`

- **What you see:** a learning task stopped with the code `native_tool_event` or `process_identity_unverified`.
- **What caused it:** for `native_tool_event`, Codex used a tool during a learning task. For `process_identity_unverified`, Muninn cannot check that an earlier learning process stopped.
- **What happens if you do nothing:** learning stays off. Muninn learns nothing new. Your memories stay.
- **How to repair it:** you cannot repair this. The person who looks after your Muninn install must check the cause first.
- **Steps:**
  1. Run `muninn learn list --state failed`, and write down the ID of the task.
  2. Run `muninn learn explain R_ID --human`, with your ID, and copy the output.
  3. Run `muninn doctor --pretty`, and copy the output.
  4. Send the two outputs to the person who looks after your Muninn install.
  5. Do not run `bin/muninn-install --enable-learning`. It turns learning on before anyone checks the cause.
- **Result:** that person can check the cause and decide when learning can start again.

### Keep learning off when Muninn cannot check its policy

`control_absent`, `control_invalid`, `policy_absent`, `policy_invalid` and `policy_digest_mismatch`

- **What you see:** a notice says: "Muninn learning is blocked. Muninn cannot verify its saved policy. The agent can inspect the saved setup with muninn doctor --pretty."
- **What caused it:** the learning rules that Muninn kept are not correct or changed, or Muninn cannot find them. Muninn cannot trust them.
- **What happens if you do nothing:** learning stays blocked. Muninn learns nothing new. Your memories stay.
- **How to repair it:** you cannot repair this. Do not edit the database or the policy files.
- **Steps:**
  1. Run `muninn doctor --pretty`, and copy the output.
  2. Send the output to the person who looks after your Muninn install.
- **Result:** that person can repair the learning rules.

### Turn learning on again after a pause

`paused`

- **What you see:** a notice says: "Muninn learning is paused. The agent can inspect its saved policy."
- **What caused it:** learning does not run. You paused it, or Muninn paused it after a safety stop.
- **What happens if you do nothing:** Muninn learns nothing new. Your memories stay.
- **How to repair it:** find out why learning stopped, and then turn it on again.
- **Steps:**
  1. Run `muninn learn list --state failed`.
  2. If the list shows a task, run `muninn learn explain R_ID --human`, with its ID.
  3. Find the code in this guide, and do the steps for that code.
  4. Run `muninn doctor --pretty`, and make sure that it shows no error.
  5. Do the steps in [Turn learning on](#turn-learning-on).
- **Result:** Muninn learns from your chats again.
