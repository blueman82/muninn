"""Origin detector (rubric origin_rules; protocol §1.2, §5.2).

Each message event gets every rule that fires, then one class by
precedence GENERATED_OR_REDACTED > INJECTED > REPLAY_COPY > ORIGINAL.

Rubric rules: GENERATED (text markers), INJECTED_PREFIX (user rows),
EARLIER_COPY (>= 40 whitespace-normalised chars equal to a same-role
event in another logical source that is earlier by (timestamp, key)),
FORK_PARENT_EQUAL (a row of a Codex rollout with forked_from_id equal
to a same-role row of the parent rollout).

Structural rules (Codex from the LINE 1 session_meta only):
FORK_INHERITED_K (subagent fork rows before subagent_history_start_
ordinal), GUARDIAN_THREAD (thread_source guardian_review or source.
subagent.other guardian: user rows INJECTED, assistant rows
REPLAY_COPY), CLAUDE_META (isMeta user rows, INJECTED) and
CLAUDE_PARENT_COPY (a subagents/ row equal to a parent-session row).

The labelled originals stay ORIGINAL (label authority); the detector's
own class is kept alongside so disagreements are disclosed.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from trial_harness.extractor import Corpus, file_events, ws_norm

GENERATED = "GENERATED_OR_REDACTED"
INJECTED_RULES = {"INJECTED_PREFIX", "CLAUDE_META"}
REPLAY_RULES = {
    "EARLIER_COPY",
    "FORK_PARENT_EQUAL",
    "FORK_INHERITED_K",
    "CLAUDE_PARENT_COPY",
}
MIN_COPY_CHARS = 40


def _when(stamp: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _guardian(meta: dict) -> bool:
    source = meta.get("source")
    sub = source.get("subagent") if isinstance(source, dict) else None
    return meta.get("thread_source") == "guardian_review" or (
        isinstance(sub, dict) and sub.get("other") == "guardian"
    )


def _session_meta(path) -> dict | None:
    """LINE 1 session_meta payload, the only source of file class."""
    with open(path, "rb") as source:
        try:
            first = json.loads(source.readline())
        except ValueError:
            return None
    if not isinstance(first, dict) or first.get("type") != "session_meta":
        return None
    payload = first.get("payload")
    return payload if isinstance(payload, dict) else None


class OriginIndex:
    """Classifies every message event of the T0 corpus once."""

    def __init__(self, corpus: Corpus, rules: dict, labelled=frozenset()):
        self.rules, self.labelled = rules, set(labelled)
        self.events, self.by_file = {}, defaultdict(list)
        self.meta, sessions = {}, defaultdict(list)
        for (provider, key), path in sorted(corpus.files.items()):
            for event in file_events(corpus, provider, key).values():
                self.events[provider, key, event.line, event.ordinal] = event
                self.by_file[provider, key].append(event)
            if provider == "codex":
                payload = _session_meta(path)
                if payload is not None:
                    self.meta[key] = payload
                    sessions[payload.get("id")].append(key)
        self.flags = defaultdict(set)
        self._text_rules()
        self._earlier_copies()
        self._codex_structure(sessions)
        self._claude_parent_copies()

    def _flag(self, provider, key, event, rule: str) -> None:
        self.flags[provider, key, event.line, event.ordinal].add(rule)

    def _text_rules(self) -> None:
        generated = self.rules["GENERATED_OR_REDACTED"]
        injected = self.rules["INJECTED"]
        prefixes = tuple(injected["stripped_text_starts_with_any"])
        for (provider, key, _, _), event in self.events.items():
            text = event.text
            if text in generated["text_equals"] or any(
                marker in text for marker in generated["text_contains_any"]
            ):
                self._flag(provider, key, event, "GENERATED")
            if event.role == injected["role"]:
                if text.strip().startswith(prefixes):
                    self._flag(provider, key, event, "INJECTED_PREFIX")
                if provider == "claude" and event.is_meta:
                    self._flag(provider, key, event, "CLAUDE_META")

    def _earlier_copies(self) -> None:
        groups = defaultdict(list)
        for (provider, key, _, _), event in self.events.items():
            text, moment = ws_norm(event.text), _when(event.timestamp)
            if len(text) >= MIN_COPY_CHARS and moment is not None:
                source = (provider, key)
                groups[event.role, text].append((moment, source, event))
        for members in groups.values():
            members.sort(key=lambda m: (m[0], m[1], m[2].line))
            earlier: set = set()
            start = 0
            while start < len(members):
                end = start
                while end < len(members) and (
                    members[end][:2] == members[start][:2]
                ):
                    end += 1
                source = members[start][1]
                if earlier - {source}:
                    for _, _, event in members[start:end]:
                        self._flag(*source, event, "EARLIER_COPY")
                earlier.add(source)
                start = end

    def _codex_structure(self, sessions: dict) -> None:
        for key, meta in self.meta.items():
            events = self.by_file["codex", key]
            if _guardian(meta):
                for event in events:
                    self._flag("codex", key, event, "GUARDIAN_THREAD")
            parent_id = meta.get("forked_from_id")
            if not isinstance(parent_id, str) or not parent_id:
                continue
            parent = {
                (e.role, ws_norm(e.text))
                for p in sessions.get(parent_id, [])
                if p != key
                for e in self.by_file["codex", p]
            }
            start = meta.get("subagent_history_start_ordinal")
            inherits = meta.get("thread_source") == "subagent" and (
                isinstance(start, int)
            )
            for event in events:
                if (event.role, ws_norm(event.text)) in parent:
                    self._flag("codex", key, event, "FORK_PARENT_EQUAL")
                if inherits and event.line < start:
                    self._flag("codex", key, event, "FORK_INHERITED_K")

    def _claude_parent_copies(self) -> None:
        for (provider, key), events in list(self.by_file.items()):
            if provider != "claude" or "/subagents/" not in key:
                continue
            parent_key = key.split("/subagents/")[0] + ".jsonl"
            parent = {
                (e.role, ws_norm(e.text))
                for e in self.by_file.get(("claude", parent_key), [])
            }
            for event in events:
                if (event.role, ws_norm(event.text)) in parent:
                    self._flag("claude", key, event, "CLAUDE_PARENT_COPY")

    def classify(
        self, provider: str, key: str, line: int, ordinal: int = 1
    ) -> dict | None:
        event = self.events.get((provider, key, line, ordinal))
        if event is None:
            return None
        rules = sorted(self.flags.get((provider, key, line, ordinal), ()))
        guardian = "GUARDIAN_THREAD" in rules
        if "GENERATED" in rules:
            detector = GENERATED
        elif INJECTED_RULES & set(rules) or (
            guardian and event.role == "user"
        ):
            detector = "INJECTED"
        elif REPLAY_RULES & set(rules) or guardian:
            detector = "REPLAY_COPY"
        else:
            detector = "ORIGINAL"
        label = (provider, key, line, event.record_sha256) in self.labelled
        return {
            "origin": "ORIGINAL" if label else detector,
            "detector_origin": detector,
            "rules": rules,
            "label_authority": label,
        }


def _key(identity: dict) -> tuple:
    return (
        identity["provider"],
        identity["source_path"],
        identity["source_line"],
        identity.get("source_ordinal", 1),
    )


def validate(
    index: OriginIndex,
    hist: dict[str, list[dict]],
    replay: set[str],
    injected: set[str],
    required: list[dict],
) -> dict:
    """rubric origin_rules.detector_validation, on detector classes.

    Cited rows that are themselves labelled required originals are
    exempt from the INJECTED check (label authority makes them
    ORIGINAL); the exemption is counted.
    """
    required_keys = {_key(r) for r in required}

    def row(identity: dict) -> dict:
        found = index.classify(*_key(identity)) or {"detector_origin": None}
        return {
            "identity": identity,
            "detector_origin": found["detector_origin"],
            "rules": found.get("rules", []),
        }

    replays = [row(c) for q in sorted(replay) for c in hist[q]]
    cited = [c for q in sorted(injected) for c in hist[q]]
    injections = [row(c) for c in cited if _key(c) not in required_keys]
    flagged = [
        r for r in map(row, required) if r["detector_origin"] != "ORIGINAL"
    ]
    checks = {
        "replay": {
            "questions": sorted(replay),
            "rows": len(replays),
            "failed": [
                r for r in replays if r["detector_origin"] != "REPLAY_COPY"
            ],
        },
        "injected": {
            "questions": sorted(injected),
            "rows": len(injections),
            "exempt_required": len(cited) - len(injections),
            "failed": [
                r for r in injections if r["detector_origin"] != "INJECTED"
            ],
        },
        "required_unflagged": {
            "required": len(required),
            "flagged": flagged,
        },
    }
    ok = not (
        checks["replay"]["failed"] or checks["injected"]["failed"] or flagged
    )
    return {"status": "PASS" if ok else "FAIL", "checks": checks}
