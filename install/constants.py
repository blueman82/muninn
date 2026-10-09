"""Names, paths and limits shared by every installer module."""

from __future__ import annotations

import os

LABEL = "com.muninn"
MKT_NAME = "muninn-local"
PLUGIN_ID = f"muninn@{MKT_NAME}"
MARKETPLACE = f"[marketplaces.{MKT_NAME}]"
PLUGIN = f'[plugins."{PLUGIN_ID}"]'
EVENTS = ("pre_tool_use", "session_start", "user_prompt_submit")
# Codex keys a hook's trust record by plugin, file, event and position; we
# only ever manage the first group and handler (``:0:0``) of each event.
TRUST = {
    e: f'[hooks.state."{PLUGIN_ID}:hooks/hooks.json:{e}:0:0"]' for e in EVENTS
}
# The only keys we may read or write in each of our config.toml sections.
CODEX_KEYS = {
    MARKETPLACE: {"source_type", "source", "last_updated", "last_revision"},
    PLUGIN: {"enabled"},
    **{h: {"enabled", "trusted_hash"} for h in TRUST.values()},
}
# Substrings that mark a line as ours; none may appear outside our sections.
MARKERS = (MKT_NAME, "muninn@")
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit")
CURSOR_DOC_URL = (
    "https://github.com/blueman82/muninn/blob/main/docs/QUICKSTART.md"
    "#cursor-history"
)
# Codex versions whose trust-hash algorithm we have checked: 0.159.2 against
# the Codex source and binary, 0.159.3 only against a live ``hooks/list``
# currentHash. Any other version leaves trusting the hooks to the owner
# instead of writing a hash that might not match.
CODEX_VERIFIED = ("codex-cli 0.159.2", "codex-cli 0.159.3")
PLIST = "Library/LaunchAgents/com.muninn.plist"
# An uninstall moves the data dir to a sibling of this name plus the run's
# timestamp instead of deleting it.
REMOVED_PREFIX = "muninn-removed-"
# The run timestamp that names per-run dirs, and the mode every file we
# create gets masked to: nobody but the owner may read them.
TS_FORMAT = "%Y%m%dT%H%M%SZ"
PRIVATE_UMASK = 0o077
# The mode of every directory we create that holds our own state.
PRIVATE_DIR_MODE = 0o700
# The value-free summary an install leaves in the lib dir for support.
INSTALL_RECORD = "install-record.json"
# The store file in the data dir, and the prefix of the copy an upgrade makes
# before the new release migrates it (muninn/store.py repeats the prefix; a
# test keeps them equal, since the runtime cannot import the installer).
STORE_FILE = "muninn.sqlite"
PRE_UPGRADE_PREFIX = "muninn.sqlite.pre-upgrade-"
# Longest age of status.json that still counts as a live heartbeat.
HEARTBEAT_S = 120
# Files read from git at the pinned commit, never from the working tree. The
# first entry is the Claude hook fragment; preflight relies on that order.
PINNED = (
    "integrations/claude/settings-hooks.json",
    "integrations/codex/.agents/plugins/marketplace.json",
    "integrations/codex/.codex-plugin/plugin.json",
    "integrations/codex/hooks/hooks.json",
    "integrations/cursor/hooks.json",
    "launchd/com.muninn.plist",
)
OWNER_STEP = (
    "OWNER STEP: start a new Codex session, run /hooks and trust the two "
    "muninn hooks (session_start, user_prompt_submit)."
)
# The per-prompt recall switch muninn checks for in its data dir
# (muninn/hook.py).
# A fresh install creates it, so recall starts off; an upgrade leaves it be.
RECALL_OFF = "recall.off"
# Read-only git calls must not take index.lock, or they would race the
# owner's own git commands in the same repository.
# The repository is always named with -C, so an inherited GIT_DIR,
# GIT_INDEX_FILE or GIT_WORK_TREE (set inside a git hook) must not redirect
# these calls: a commit hook running the tests once staged a temp repo into
# the real index.
GIT_ENV = {
    k: v
    for k, v in os.environ.items()
    if k not in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")
} | {"GIT_OPTIONAL_LOCKS": "0"}
