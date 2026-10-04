"""Shared fixtures for the hook tests: the frame constants and base cases.

Synthetic rows in temp dirs; the providers' payloads are built by hand.
Nothing touches a live data dir or a provider root.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from muninn import hook, obs
from tests import test_cli as tcli
from tests import test_knowledge as tk

OPEN = '<muninn-memory source="muninn" trust="untrusted-data">'
CLOSE = "</muninn-memory>"
USAGE = (
    "Before answering about earlier work or re-deciding a recorded choice, run"
    ' `muninn search "<words>"` and open what you cite. Record an owner'
    " decision"
    " only if the owner said it: `muninn know add --kind decision --text …"
    ' --cite REF --quote "<verbatim>"` (REF from `muninn search`; check the'
    " quote with `muninn quote-check`; see `muninn know add --help`)."
)
TAG = re.compile(r"(?i)<\s*/?\s*muninn-(?:memory|recall)")
AKIA = "AKIA" + "ABCDEFGHIJKLMNOP"
RECALL_OPEN = (
    '<muninn-memory source="muninn" trust="untrusted-data" kind="recall">'
)
HINT = "`muninn open <ref> --context 3`"
Q = "alphaterm betaterm gammaterm deltaterm"
LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "muninn"
HOOKS_JSON = LAUNCHER.parent.parent / "integrations/codex/hooks/hooks.json"


def notice(code: str, event: str = "SessionStart") -> dict[str, object]:
    """Build the fail-open notice a hook answers when it cannot read.

    Args:
        code: Reason code shown in the notice text.
        event: Hook event name echoed in the output.

    Returns:
        The provider JSON the hook prints.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": (
                '<muninn-memory source="muninn" trust="untrusted-data"'
                f' kind="notice">\nmuninn: store unavailable ({code})\n'
                + CLOSE
            ),
        },
        "systemMessage": f"muninn: memory unavailable ({code})",
    }


def hook_output(out: object) -> dict[str, Any]:
    """Return the ``hookSpecificOutput`` object of a hook answer.

    Args:
        out: Parsed provider JSON.

    Returns:
        The inner object, narrowed from ``object`` for indexing.
    """
    assert isinstance(out, dict)
    found = out["hookSpecificOutput"]
    assert isinstance(found, dict)
    return found


class HookCase(tk.KnowCase):
    """A repo scope with a fresh poller heartbeat."""

    def setUp(self) -> None:
        super().setUp()
        self.env = {"MUNINN_HOME": str(self.home)}
        obs.write_status(
            self.home, {"last_pass_at": time.time(), "interval_s": 60}
        )

    def add(self, **kw: Any) -> dict[str, Any]:
        """Add an entry whose user quote also holds its text.

        Hook tests care about rendering, not about whether the user's words
        back the text; ``backed=False`` keeps the plain quote.

        Args:
            **kw: Arguments for ``knowledge.add``, plus ``backed``.

        Returns:
            The result of ``knowledge.add``.
        """
        backed = kw.pop("backed", True)
        out = super().add(**kw)
        if backed:
            self.rw.execute(
                "UPDATE citation SET quote = quote || ' ' || ?"
                " WHERE knowledge_id = ?",
                (out["entry"]["text"], tk.kid(out)),
            )
        return out

    def payload(self, **kw: object) -> dict[str, object]:
        """Build a SessionStart payload for the repo.

        Args:
            **kw: Fields that override the defaults.

        Returns:
            The payload dict.
        """
        base: dict[str, object] = {
            "hook_event_name": "SessionStart",
            "cwd": "/repo",
            "session_id": "sess-now",
            "source": "startup",
        }
        return base | kw

    def start(
        self,
        provider: str = "claude",
        env: dict[str, str] | None = None,
        **kw: object,
    ) -> dict[str, object]:
        """Run the SessionStart hook in process.

        Args:
            provider: Provider the payload is for.
            env: Environment entries that override the test environment.
            **kw: Payload fields that override the defaults.

        Returns:
            The provider JSON the hook returns.
        """
        return hook.session_start(
            self.payload(**kw), provider, self.env | (env or {})
        )

    @staticmethod
    def body(out: object) -> str:
        """Return the ``additionalContext`` text of a hook answer.

        Args:
            out: Parsed provider JSON.

        Returns:
            The context text.
        """
        text = hook_output(out)["additionalContext"]
        assert isinstance(text, str)
        return text

    def raw_text(self, number: int, text: str) -> None:
        """Store text the way an older writer might: unescaped.

        The entry's quotes get the text appended so the user's words still
        back it and it stays pushable.

        Args:
            number: Knowledge entry id.
            text: Text to write unchanged.
        """
        self.rw.execute(
            "UPDATE knowledge SET text = ? WHERE id = ?", (text, number)
        )
        self.rw.execute(
            "UPDATE citation SET quote = quote || ' ' || ?"
            " WHERE knowledge_id = ?",
            (text, number),
        )


class RecallCase(HookCase):
    """Sessions that talk about the four made-up terms."""

    def ask(
        self,
        prompt: object = Q,
        provider: str = "claude",
        env: dict[str, str] | None = None,
        **kw: object,
    ) -> dict[str, object]:
        """Run the UserPromptSubmit hook in process.

        Args:
            prompt: Prompt value; deliberately not limited to strings.
            provider: Provider the payload is for.
            env: Environment entries that override the test environment.
            **kw: Payload fields that override the defaults.

        Returns:
            The provider JSON the hook returns.
        """
        payload: dict[str, object] = {
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/repo",
            "session_id": "sess-now",
            "prompt": prompt,
        } | kw
        return hook.prompt_submit(payload, provider, self.env | (env or {}))

    def talk(
        self,
        name: str,
        text: str,
        scope_id: int | None = None,
        **kw: Any,
    ) -> int:
        """Add a root event of a made-up session.

        Args:
            name: Thread name; the session is ``<name>-root``.
            text: Event text.
            scope_id: Scope of the event; the repo when omitted.
            **kw: Extra event fields; ``provider`` picks the source's.

        Returns:
            The new event id.
        """
        src = self.add_source(
            name, session=f"{name}-root", provider=kw.pop("provider", "codex")
        )
        event = self.add_event(src, scope_id or self.repo, text, **kw)
        assert event is not None
        return event

    def lines(self, out: object) -> list[str]:
        """Return the bullet lines of a hook answer.

        Args:
            out: Parsed provider JSON.

        Returns:
            Lines of the context text that start with ``- ``.
        """
        return [x for x in self.body(out).split("\n") if x.startswith("- ")]


class HookCliCase(tcli.CliCase):
    """The installed-command form: bin/muninn in a subprocess.

    The payload goes on stdin and only the provider JSON is read from
    stdout.
    """

    def run_hook(
        self,
        event: str,
        provider: str,
        payload: Mapping[str, object],
        env: dict[str, str] | None = None,
        raw: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run ``muninn hook`` as the provider would.

        Args:
            event: Hook command name, such as ``prompt``.
            provider: Provider passed with ``--provider``.
            payload: Payload serialized to stdin unless ``raw`` is given.
            env: Environment entries that override the test environment.
            raw: Exact stdin bytes to send instead of the payload.

        Returns:
            The finished process with captured output.
        """
        body = json.dumps(payload).encode() if raw is None else raw
        return subprocess.run(
            [str(LAUNCHER), "hook", event, "--provider", provider],
            input=body,
            capture_output=True,
            env=self.env | (env or {}),
            cwd=self.repo,
            timeout=60,
        )

    def payload(self, **kw: object) -> dict[str, object]:
        """Build a UserPromptSubmit payload for the repo.

        Args:
            **kw: Fields that override the defaults.

        Returns:
            The payload dict.
        """
        base: dict[str, object] = {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(self.repo),
            "session_id": "sess-now",
        }
        return base | kw

    def parsed(self, done: subprocess.CompletedProcess[bytes]) -> Any:
        """Check a hook run printed exactly one JSON line and parse it.

        Args:
            done: The finished hook process.

        Returns:
            The parsed JSON.
        """
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, b"")
        self.assertEqual(done.stdout.count(b"\n"), 1)  # one JSON line
        return json.loads(done.stdout)
