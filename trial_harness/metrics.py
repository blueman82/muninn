"""§6 metrics per arm and question, stage attribution and criteria.

Wrapper-log contract (§4.1, written by the H1 launcher): one JSON
record per call with seq, prev_sha256 (sha256 of the previous record's
raw line; 64 zeros first), argv, t_start/t_end (ms), bytes_out, error
and returned_ids = [{identity: {provider, source_path, line,
record_sha256}, class: "listing"|"open", chars: [start, end] | null}],
where chars is the half-open range of original text an open delivered.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter

from trial_harness.grading import opened_enough, validity

NON_ORIGINAL = ("REPLAY_COPY", "INJECTED", "GENERATED_OR_REDACTED")


def identity_key(identity: dict) -> tuple:
    return (
        identity["provider"],
        identity["source_path"],
        identity["line"],
        identity["record_sha256"],
    )


def chain_ok(lines: list[str]) -> bool:
    previous = "0" * 64
    for line in lines:
        if json.loads(line).get("prev_sha256") != previous:
            return False
        previous = hashlib.sha256(line.encode()).hexdigest()
    return True


def surfaced(log: list[dict]) -> set[tuple]:
    """Identities returned to the reader by any call (candidate recall)."""
    return {
        identity_key(item["identity"])
        for call in log
        for item in call["returned_ids"]
    }


def _open_ranges(log: list[dict]) -> dict[tuple, list]:
    ranges: dict[tuple, list] = {}
    for call in log:
        for item in call["returned_ids"]:
            if item["class"] == "open" and item.get("chars"):
                key = identity_key(item["identity"])
                ranges.setdefault(key, []).append(item["chars"])
    return ranges


def opened(log: list[dict], text_length: dict[tuple, int]) -> set[tuple]:
    """rubric `opened` for OLD/NEW: enough contiguous original text."""
    return {
        key
        for key, ranges in _open_ranges(log).items()
        if key in text_length and opened_enough(ranges, text_length[key])
    }


def first_open_listing_count(
    log: list[dict], text_length: dict, classes: dict
) -> dict[tuple, int]:
    """Listing-class calls made before each identity was first opened."""
    listings, first = 0, {}
    for call in sorted(log, key=lambda c: c["seq"]):
        for item in call["returned_ids"]:
            key = identity_key(item["identity"])
            if item["class"] == "open" and key not in first:
                first[key] = listings
        if classes.get(call["argv"][0]) == "listing":
            listings += 1
    return first


def cost(log: list[dict], classes: dict, usage: dict | None = None) -> dict:
    commands = [call["argv"][0] for call in log]
    latency = [call["t_end"] - call["t_start"] for call in log]
    return {
        "calls": len(log),
        "by_command": {c: commands.count(c) for c in sorted(set(commands))},
        "listing_calls": sum(classes.get(c) == "listing" for c in commands),
        "open_calls": sum(classes.get(c) == "open" for c in commands),
        "bytes": sum(call["bytes_out"] for call in log),
        "wall_ms": (
            max(c["t_end"] for c in log) - min(c["t_start"] for c in log)
            if log
            else 0
        ),
        "median_latency_ms": statistics.median(latency) if latency else None,
        "mean_latency_ms": statistics.mean(latency) if latency else None,
        "reader_tokens": usage if usage is not None else "NOT_RECORDED",
        "ceiling_hit": any(
            "call limit reached" in str(call.get("error") or "")
            for call in log
        ),
    }


def _second_hand_only(judgement: dict) -> bool:
    return any(
        {"SECOND_HAND"} <= {s["type"] for s in claim["support"]}
        and "FIRST_HAND" not in {s["type"] for s in claim["support"]}
        for claim in judgement["claims"]
    )


def _non_conversational(judgement: dict) -> bool:
    return any(c["non_conversational"] for c in judgement["citations"])


def contaminated(cites: list[dict], judgement: dict | None) -> bool:
    return any(
        c["origin"] in NON_ORIGINAL or not c["in_scope"] for c in cites
    ) or (
        judgement is not None
        and (_non_conversational(judgement) or _second_hand_only(judgement))
    )


def stage(
    required: list[tuple],
    cites: list[dict],
    judgement: dict,
    surfaced: set | None,
    opened: set | None,
    reachable: dict[tuple, bool],
) -> str:
    """§6 first matching stage for an X-failed answerable answer.

    With several unsurfaced originals, NOT_REACHABLE wins if any of
    them was unreachable. HIST has no per-reader log (surfaced None),
    so only CONTAMINATION_CITED can be attributed there.
    """
    if surfaced is not None:
        missing = [key for key in required if key not in surfaced]
        if missing:
            split = (
                "NOT_REACHABLE"
                if any(not reachable.get(key) for key in missing)
                else "REACHABLE_NOT_SURFACED"
            )
            return f"NOT_SURFACED:{split}"
    if contaminated(cites, judgement):
        return "CONTAMINATION_CITED"
    if surfaced is None or opened is None:
        return "NOT_RECORDED"
    if any(key not in opened for key in required):
        return "SURFACED_NOT_OPENED"
    return "OPENED_BUT_ANSWER_WRONG"


def failing_criteria(
    cites: list[dict],
    judgement: dict | None,
    abstained: bool,
    format_fail: bool,
) -> list[str]:
    """§6 failing-criteria labels (several may apply)."""
    if format_fail:
        return ["FORMAT_FAIL"]
    if abstained:
        return ["ABSTAINED"]
    _, vx = validity(cites, judgement)
    claims, additions = judgement["claims"], judgement["additions"]
    first_hand = {
        n for n, ok in vx.items() if ok
    }  # citations that can carry X support
    checks = (
        ("MISSING_CLAIM", any(not c["present"] for c in claims)),
        ("CONTRADICTION", any(c["contradicted"] for c in claims)),
        (
            "UNSUPPORTED_ADDITION",
            any(
                not any(
                    s["type"] == "FIRST_HAND" and s["citation"] in first_hand
                    for s in a["support"]
                )
                for a in additions
            ),
        ),
        (
            "NON_ORIGINAL_SUPPORT",
            any(c["origin"] != "ORIGINAL" for c in cites)
            or _non_conversational(judgement)
            or _second_hand_only(judgement),
        ),
        ("OUT_OF_SCOPE", any(not c["in_scope"] for c in cites)),
        (
            "INVALID_CITATION",
            any(
                not (c["identity_valid"] and c["opened"] and c["role_ok"])
                for c in cites
            ),
        ),
    )
    return [label for label, hit in checks if hit]


def funnel(cites: list[dict], judgement: dict) -> dict:
    """identity_valid -> valid_P -> valid_X -> >= 1 FIRST_HAND support."""
    vp, vx = validity(cites, judgement)
    backed = {
        s["citation"]
        for item in judgement["claims"] + judgement["additions"]
        for s in item["support"]
        if s["type"] == "FIRST_HAND"
    }
    return {
        "citations": len(cites),
        "identity_valid": sum(bool(c["identity_valid"]) for c in cites),
        "valid_P": sum(vp.values()),
        "valid_X": sum(vx.values()),
        "first_hand": sum(vx[n] and n in backed for n in vx),
    }


def _supporting_keys(cites: list[dict], judgement: dict) -> set[tuple]:
    numbers = {
        s["citation"]
        for item in judgement["claims"] + judgement["additions"]
        for s in item["support"]
        if s["type"] == "FIRST_HAND"
    }
    return {
        identity_key(c["identity"])
        for n, c in enumerate(cites, start=1)
        if n in numbers
    }


def unit_summary(unit: dict) -> dict:
    """§6 per-unit metrics from a closed, graded unit.

    unit: arm, qid, kind (ANSWER|W|MIRROR), abstained, format_fail,
    citations (mechanical records), judgement (final item judgement or
    None), final (rules), required and reachable (identity keys),
    surfaced/opened (sets, None for HIST), first_open_listings, cost,
    scope_leak, control_label.
    """
    out = {k: unit[k] for k in ("unit_id", "arm", "qid", "kind")}
    out |= {"scope_leak": unit.get("scope_leak"), "cost": unit.get("cost")}
    if unit["kind"] != "ANSWER":
        return out | {
            "CONTROL": unit["final"]["CONTROL"],
            "control_label": unit.get("control_label"),
        }
    final, cites, judged = unit["final"], unit["citations"], unit["judgement"]
    required, seen, read = unit["required"], unit["surfaced"], unit["opened"]

    def recall(found: set | None) -> float | None:
        if found is None or not required:
            return None
        return sum(key in found for key in required) / len(required)

    cited = {identity_key(c["identity"]) for c in cites}
    firsts = unit.get("first_open_listings") or {}
    passed = bool(final["X"])
    out |= {k: final[k] for k in ("P", "R", "X")}
    out |= {
        "reachable_all": all(unit["reachable"].get(k) for k in required),
        "reachable_ids": sum(bool(unit["reachable"].get(k)) for k in required),
        "required_ids": len(required),
        "candidate_recall": recall(seen),
        "opened_recall": recall(read),
        "required_cited": sum(key in cited for key in required),
        "over_abstained": unit["abstained"],
        "stage": (
            None
            if passed
            else stage(required, cites, judged, seen, read, unit["reachable"])
        ),
        "criteria": (
            []
            if passed
            else failing_criteria(
                cites, judged, unit["abstained"], unit["format_fail"]
            )
        ),
        "funnel": funnel(cites, judged) if judged else None,
        "unsupported_additions": (
            judged.get("unsupported_additions") if judged else None
        ),
        "sensitivity_15": passed
        and all(
            firsts.get(key, 16) <= 15
            for key in _supporting_keys(cites, judged)
        ),
    }
    return out


def arm_summary(units: list[dict]) -> dict[str, dict]:
    """§6 secondary metrics per arm (descriptive only, no tests)."""
    arms: dict[str, dict] = {}
    for row in map(unit_summary, units):
        arm = arms.setdefault(
            row["arm"],
            {
                "answerable": 0,
                "X_pass": 0,
                "P_pass": 0,
                "R_pass": 0,
                "reachable_all_required": 0,
                "reachable_identities": 0,
                "required_identities": 0,
                "required_cited": 0,
                "over_abstention": 0,
                "sensitivity_15": 0,
                "unsupported_addition_answers": 0,
                "unsupported_additions": 0,
                "funnel": Counter(),
                "W": {"pass": 0, "units": 0, "labels": Counter()},
                "MIRROR": {"pass": 0, "units": 0},
                "scope_leak": 0,
                "stages": Counter(),
                "criteria": Counter(),
            },
        )
        arm["scope_leak"] += row["scope_leak"] or 0
        if row["kind"] != "ANSWER":
            control = arm[row["kind"]]
            control["units"] += 1
            control["pass"] += bool(row["CONTROL"])
            if row["kind"] == "W":
                control["labels"][row["control_label"]] += 1
            continue
        arm["answerable"] += 1
        for rule in ("X", "P", "R"):
            arm[f"{rule}_pass"] += bool(row[rule])
        arm["reachable_all_required"] += row["reachable_all"]
        arm["reachable_identities"] += row["reachable_ids"]
        arm["required_identities"] += row["required_ids"]
        arm["required_cited"] += row["required_cited"]
        arm["over_abstention"] += bool(row["over_abstained"])
        arm["sensitivity_15"] += row["sensitivity_15"]
        extra = row["unsupported_additions"] or 0
        arm["unsupported_addition_answers"] += extra > 0
        arm["unsupported_additions"] += extra
        arm["funnel"].update(row["funnel"] or {})
        if row["stage"]:
            arm["stages"][row["stage"]] += 1
        arm["criteria"].update(row["criteria"])
    return arms
