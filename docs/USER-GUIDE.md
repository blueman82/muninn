# Muninn user guide

This guide is for you if you use Muninn with Claude Code or Codex. It shows you how to use Muninn. It also tells you what to do when Muninn shows a message.

Run each command in a terminal.

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

Learning lets Muninn read your finished chats and write memories from them. Muninn sends parts of your chats to the Claude program to do this. You need a Mac, and the `claude` program must be signed in. If it is not, learning stays off.

To turn learning on, do these steps:

1. Run `muninn learn on`.
2. Run `muninn learn list` to see your learning tasks.

Muninn learns only from chats that start after you turn learning on. New memories appear in `muninn know list`. If a learning task stops, see [Fix a problem](#fix-a-problem).

To stop learning, run `muninn learn off`. Your memories stay. The file `learning.json` in the Muninn data folder holds the setting, the provider and the model.

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

If the installer prints `FAILED:` and a code, do the steps under `What to do:` that the installer prints after the code.

## Fix a problem

When a learning task stops, Muninn shows a short code. Find the code in this section. Do the steps of that entry.

To see the code of a task, do these steps:

1. Run `muninn learn list --state failed`.
2. Find the task in the list, and write down its ID. The ID starts with `R_`.
3. Run `muninn learn explain R_ID --human`, with your ID.

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

### Start a stopped task again after a time-out

`deadline`, `unavailable` and `rejected_revision`

- **What you see:** a learning task stopped with the code `deadline`, `unavailable` or `rejected_revision`.
- **What caused it:** for `deadline`, the task used more time than its limit. For `unavailable`, the Claude program did not finish the task. For `rejected_revision`, your memories changed while the task ran.
- **What happens if you do nothing:** Muninn tries the task again by itself. After its last try, Muninn stops the task. Muninn then does not learn from that part of the chat. Other tasks continue.
- **How to repair it:** start the stopped task again by hand.
- **Steps:**
  1. Run `muninn learn list --state failed`, and write down the ID of the task.
  2. Run `muninn learn retry R_ID`, with your ID.
  3. If the task stops again, run `muninn learn explain R_ID --human`.
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
  3. Write down its name. It starts with `claude:` or `codex:`.
  4. Run `muninn learn request --session REF`, with that name.
- **Result:** Muninn can learn from the chat again.

### Do nothing when Muninn rejects a result

`rejected_authority`, `rejected_validation` and `rejected_scope`

- **What you see:** a learning task shows the code `rejected_authority`, `rejected_validation` or `rejected_scope`.
- **What caused it:** the Claude program proposed a new memory, and a safety check refused it. For `rejected_authority`, Muninn found no proof that you said it. For `rejected_validation`, the result had an incorrect format. For `rejected_scope`, the memory was not in the project of the task.
- **What happens if you do nothing:** no problem occurs. Muninn drops that result and continues with other tasks.
- **How to repair it:** you do not have to do anything. Do not start the task again. A new try gives the same result.
- **Steps:** none.
- **Result:** Muninn keeps only results that its checks accept.

### Get help when learning shows a code

`auth`, `config` and any other code that no entry names

- **What you see:** a learning task stopped with a code that no other entry names.
- **What caused it:** Muninn found a problem that this guide does not cover. For `auth`, the `claude` program is not signed in.
- **What happens if you do nothing:** Muninn does not learn from that part of the chat. Other tasks continue.
- **How to repair it:** read the explanation of the task. For `auth`, sign in to the `claude` program, and then start the task again.
- **Steps:**
  1. Run `muninn learn explain R_ID --human`, with the ID of the task.
  2. Run `muninn doctor --pretty`, and read the output.
  3. Send both outputs to the person who looks after your Muninn install.
- **Result:** that person can find the cause.
