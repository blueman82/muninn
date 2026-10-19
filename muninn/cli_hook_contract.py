"""Share data-only provider hook command names without loading runtimes."""

from __future__ import annotations

HOOK_EVENTS = ("session-start", "session-end", "pre-compact", "prompt")
PROVIDERS = ("claude", "codex", "cursor")
