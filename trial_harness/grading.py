"""Grading pipeline (protocol §5): blinding, packets, rules, kappa.

H is the sha256 of trial-protocol.md. Graders are launched by the H1
launcher; this module builds their prompts from packets and turns
their JSON outputs into mechanical P/R/X and control outcomes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

from trial_harness.extractor import record_hash, render

POOL_FIELDS = (
    "provider",
    "source_path",
    "source_line",
    "source_ordinal",
    "source_hash",
)
IDENTITY_FIELDS = ("provider", "source_path", "line", "record_sha256")


def answer_id(digest: str, unit_id: str) -> str:
    """§5.2: sha256(H[16:32] || unit_id)[:10]."""
    return hashlib.sha256((digest[16:32] + unit_id).encode()).hexdigest()[:10]


def grading_order(answer_ids: list[str], digest: str) -> list[str]:
    """§5.3: sorted answer ids shuffled with Random(int(H[16:32], 16))."""
    order = sorted(answer_ids)
    random.Random(int(digest[16:32], 16)).shuffle(order)
    return order


def ab_order(digest: str, answer: str) -> tuple[str, str]:
    """§5.4: which grader is shown as A, keyed by H[32:48] per answer."""
    bit = int(
        hashlib.sha256((digest[32:48] + answer).encode()).hexdigest(), 16
    )
    return ("G1", "G2") if bit % 2 == 0 else ("G2", "G1")


def pool_refs(candidates: list[dict]) -> list[tuple]:
    """The reader CLI's event ids: pool rows deduped in first-seen order."""
    refs, seen = [], set()
    for item in candidates:
        key = tuple(item[f] for f in POOL_FIELDS)
        if key not in seen:
            seen.add(key)
            refs.append(key)
    return refs


def pool_index(refs: list[tuple], citation: dict) -> int | None:
    key = tuple(citation.get(f) for f in POOL_FIELDS)
    return refs.index(key) if key in refs else None


def hist_opened(trace: list[dict], qid: str, index: int | None) -> bool:
    """HIST opened: the round-four trace opened that pool index for qid."""
    return index is not None and any(
        row["question"] == qid
        and row["command"] == "open"
        and row["args"][:1] == [str(index)]
        for row in trace
    )


def opened_enough(intervals: list[list[int]], text_length: int) -> bool:
    """OLD/NEW opened: >= min(500, len) contiguous delivered characters."""
    need, best, end = min(500, text_length), 0, None
    start = None
    for low, high in sorted(map(tuple, intervals)):
        if end is None or low > end:
            start, end = low, high
        else:
            end = max(end, high)
        best = max(best, end - start)
    return need > 0 and best >= need


def build_packet(
    answer: str,
    question: dict,
    reply: dict,
    citations: list[dict],
    control: bool,
) -> dict:
    """§5.2 arm-blind packet; citations carry `source`/`line` only for
    same-file ordering, and those fields are not copied into it."""
    by_file: dict = {}
    for item in citations:
        by_file.setdefault(item["source"], []).append(item["line"])
    rendered = []
    for number, item in enumerate(citations, start=1):
        lines = sorted(by_file[item["source"]])
        rendered.append(
            {
                "citation": number,
                "role": item["role"],
                "timestamp": item["timestamp"],
                "same_file_order": lines.index(item["line"]) + 1,
                "same_file_count": len(lines),
                "identity_valid": item["identity_valid"],
                "opened": item["opened"],
                "in_scope": item["in_scope"],
                "origin": item["origin"],
                "text": item["text"],
            }
        )
    packet = {
        "answer_id": answer,
        "control": control,
        "question": question["query"],
        "answer": {"text": reply["answer"], "abstained": reply["abstained"]},
        "citations": rendered,
    }
    if not control:
        packet["key"] = {
            "required_verbatim": list(question["required_verbatim"]),
            "atomic_claims": [
                {"claim": n, "text": text}
                for n, text in enumerate(question["atomic_claims"], start=1)
            ],
        }
    return packet


class FencedSourceError(ValueError):
    """A citation points into a clone the T0 fence deleted (§1.2).

    The fenced mirror cannot render or verify it; the protocol does not
    say how to grade that, so the harness stops instead of guessing.
    """


def _mechanics(corpus, index, identity: dict, ordinal: int, scope: str):
    """Flags shared by every arm once the canonical identity is known."""
    key = (identity["provider"], identity["source_path"])
    line = identity["line"]
    raw = corpus.line(*key, line) if isinstance(line, int) else None
    resolved = (
        raw is not None and record_hash(raw) == identity["record_sha256"]
    )
    event = render(corpus, *key, line, ordinal) if resolved else None
    found = index.classify(*key, line, ordinal) if event else None
    return {
        "identity": identity,
        "resolved": resolved,
        "role": event.role if event else None,
        "role_ok": event is not None,
        "timestamp": event.timestamp if event else None,
        "cwd": event.cwd if event else None,
        "in_scope": event is not None and event.cwd == scope,
        "origin": found["origin"] if found else None,
        "detector_origin": found["detector_origin"] if found else None,
        "rules": found["rules"] if found else [],
        "text": event.text if event else "",
        "source": key,
        "line": line,
    }


def old_citation(
    corpus,
    index,
    citation: dict,
    scope: str,
    refs: list[tuple],
    opened: bool,
    fenced: set[tuple[str, str]],
) -> dict:
    """Mechanical record for a 5-field OLD/HIST citation."""
    key = (citation["provider"], citation["source_path"])
    if key in fenced:
        raise FencedSourceError(f"{key[0]}:{key[1]}")
    identity = {
        "provider": key[0],
        "source_path": key[1],
        "line": citation["source_line"],
        "record_sha256": citation["source_hash"],
    }
    item = _mechanics(
        corpus, index, identity, citation.get("source_ordinal", 1), scope
    )
    in_pool = pool_index(refs, citation) is not None
    return item | {
        "in_pool": in_pool,
        "identity_valid": item["resolved"] and in_pool,
        "opened": opened,
    }


def physical_path(corpus, provider: str, path: str) -> str | None:
    """§1.2 step 1: strip the mirror root, or join a root-relative path."""
    mirror = f"{corpus.mirror}/"
    if path.startswith(mirror):
        return f"{corpus.home}/{path[len(mirror):]}"
    if path.startswith("/"):
        return path
    archived = path.startswith("archive/")
    for owner, origin, root in corpus.roots:
        if owner == provider and archived == (origin == "archive"):
            return f"{root}/{path.removeprefix('archive/')}"
    return None


def canonicalize(corpus, ref: dict) -> tuple[dict | None, str | None]:
    """§1.2 identity mapping: (provider, logical key, line, record_sha256)."""
    provider = ref.get("provider")
    physical = physical_path(corpus, provider, str(ref.get("source_path")))
    key = corpus.key_for(physical) if physical else None
    if key is None or key[0] != provider or corpus.path(*key) is None:
        return None, "IDENTITY_INVALID:path"
    line = ref.get("line")
    if line is None and isinstance(ref.get("byte_offset"), int):
        line = corpus.offset_line(*key, ref["byte_offset"])
        if line is None:
            return None, "IDENTITY_INVALID:offset"
    identity = {
        "provider": provider,
        "source_path": key[1],
        "line": line,
        "record_sha256": ref.get("record_sha256"),
    }
    raw = corpus.line(*key, line) if isinstance(line, int) else None
    if raw is None:
        return identity, "IDENTITY_INVALID:line"
    if record_hash(raw) != identity["record_sha256"]:
        return identity, "IDENTITY_INVALID:hash"
    return identity, None


def new_citation(
    corpus,
    index,
    ref: dict,
    scope: str,
    opened: set[tuple],
    forbidden: set[tuple],
) -> dict:
    """Mechanical record for a NEW citation (identity mapping rule)."""
    identity, reason = canonicalize(corpus, ref)
    if identity is None:
        identity = {
            "provider": ref.get("provider"),
            "source_path": ref.get("source_path"),
            "line": ref.get("line"),
            "record_sha256": ref.get("record_sha256"),
        }
    item = _mechanics(corpus, index, identity, 1, scope)
    key = tuple(identity[f] for f in IDENTITY_FIELDS)
    return item | {
        "identity_valid": reason is None,
        "identity_error": reason,
        "opened": key in opened,
        "forbidden_match": key in forbidden,
    }


# --- Final answers and grader outputs ----------------------------------


def top_level_objects(text: str) -> list:
    """Every JSON value that starts at a '{' outside earlier objects."""
    decoder, found, index = json.JSONDecoder(), [], 0
    while (index := text.find("{", index)) != -1:
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        found.append(value)
    return found


def final_answer(text: str) -> dict | None:
    """§1.4: last top-level {answer:str, citations:list, abstained:bool}."""
    for value in reversed(top_level_objects(text)):
        if (
            isinstance(value, dict)
            and set(value) == {"answer", "citations", "abstained"}
            and isinstance(value["answer"], str)
            and isinstance(value["citations"], list)
            and isinstance(value["abstained"], bool)
        ):
            return value
    return None


SUPPORT_TYPES = ("NONE", "SECOND_HAND", "FIRST_HAND")
OUTPUT_SCHEMA = "\n".join(
    (
        '{"claims": [{"claim": <claim number>, "present": <true|false>,',
        '             "contradicted": <true|false>,',
        '             "support": [{"citation": <citation number>,',
        '                          "type": "FIRST_HAND"|"SECOND_HAND"'
        '|"NONE"}]}],',
        ' "additions": [{"text": "<at most 20 words>",',
        '                "support": [<same form as claims support>]}],',
        ' "citations": [{"citation": <citation number>,',
        '                "non_conversational": <true|false>}],',
        ' "asserts_substantive_claim": <true|false>}',
        "One claims entry per numbered atomic claim (an empty list when",
        "the packet is marked CONTROL) and one citations entry per",
        "numbered citation.",
    )
)


class GraderOutputError(ValueError):
    pass


def _check_support(items: object, citations: int) -> None:
    if not isinstance(items, list):
        raise GraderOutputError("support must be a list")
    for item in items:
        if not (
            isinstance(item, dict)
            and item.get("type") in SUPPORT_TYPES
            and isinstance(item.get("citation"), int)
            and 1 <= item["citation"] <= citations
        ):
            raise GraderOutputError(f"bad support entry: {item!r}")


def _numbers(items: list, field: str) -> list:
    return sorted(i.get(field) for i in items if isinstance(i, dict))


def parse_grader_output(text: str, packet: dict) -> dict:
    """The last top-level JSON object, validated against the packet."""
    found = [v for v in top_level_objects(text) if isinstance(v, dict)]
    if not found:
        raise GraderOutputError("no JSON object")
    out = found[-1]
    cites = len(packet["citations"])
    claims = [] if packet["control"] else packet["key"]["atomic_claims"]
    for field in ("claims", "additions", "citations"):
        if not isinstance(out.get(field), list):
            raise GraderOutputError(f"{field} must be a list")
    if _numbers(out["claims"], "claim") != list(range(1, len(claims) + 1)):
        raise GraderOutputError("claims must cover the key exactly once")
    for claim in out["claims"]:
        if not all(
            isinstance(claim.get(k), bool) for k in ("present", "contradicted")
        ):
            raise GraderOutputError("present/contradicted must be booleans")
        _check_support(claim.get("support"), cites)
    for addition in out["additions"]:
        if not isinstance(addition, dict) or not isinstance(
            addition.get("text"), str
        ):
            raise GraderOutputError("addition needs text")
        _check_support(addition.get("support"), cites)
    if _numbers(out["citations"], "citation") != list(range(1, cites + 1)):
        raise GraderOutputError("citations must cover every citation")
    if not all(
        isinstance(c.get("non_conversational"), bool) for c in out["citations"]
    ):
        raise GraderOutputError("non_conversational must be boolean")
    substantive = out.get("asserts_substantive_claim")
    if packet["control"] and not isinstance(substantive, bool):
        raise GraderOutputError("controls need asserts_substantive_claim")
    return out


# --- Frozen prompts (§5.3, §5.4) ----------------------------------------

GRADER_PREAMBLE = "\n".join(
    (
        "You grade one written answer against a fixed key. You do "
        "not decide pass or fail.",
        "For each numbered atomic claim: is it present in the answer "
        "(tolerance rules below)? Is it contradicted?",
        "Which numbered citations support it, each as FIRST_HAND, "
        "SECOND_HAND or NONE (definition below)?",
        "List every factual addition (a statement about the past "
        "work that the key does not require), at most",
        "20 words each, with its supporting citations and support type.",
        "For each citation, set non_conversational if its text is "
        "not a person's or assistant's own conversational turn.",
        "If the packet is marked CONTROL there is no key: list every "
        "factual statement as an addition with its",
        "support, and say whether the answer asserts any substantive "
        "factual claim about the asked-about work.",
        "Use only this packet. Mechanical flags are facts; do not "
        "re-judge them. Return exactly one JSON object",
        "in the schema below.",
        "{TOLERANCE_RULES} {SUPPORT_DEFINITION} {OUTPUT_SCHEMA} {PACKET}",
    )
)
GRADER_PREAMBLE_HEAD = GRADER_PREAMBLE.split("\n")[0]
SLOTS = "{TOLERANCE_RULES} {SUPPORT_DEFINITION} {OUTPUT_SCHEMA} {PACKET}"
ADJUDICATOR_PREFIX = (
    "Two graders, A and B, disagreed; their outputs follow. Give the "
    "final judgement in the same schema."
)


def preamble_from_protocol(protocol: str) -> str:
    """The fenced block after 'Frozen grader preamble.' in the protocol."""
    start = protocol.index("**Frozen grader preamble.**")
    body = protocol.index("```\n", start) + 4
    return protocol[body : protocol.index("\n```", body)]


def grader_prompt(packet: dict, rubric: dict) -> str:
    blocks = (
        "TOLERANCE RULES\n" + "\n".join(rubric["tolerance_rules"]),
        "SUPPORT DEFINITION\n" + rubric["rules"]["definitions"]["support"],
        "OUTPUT SCHEMA\n" + OUTPUT_SCHEMA,
        "PACKET\n" + json.dumps(packet, indent=1, ensure_ascii=False),
    )
    return GRADER_PREAMBLE.replace(SLOTS, "\n\n".join(blocks))


def adjudicator_prompt(
    packet: dict, rubric: dict, output_a: str, output_b: str
) -> str:
    """Outputs are passed already ordered by ab_order(H, answer_id)."""
    return (
        f"{ADJUDICATOR_PREFIX}\n{grader_prompt(packet, rubric)}\n\n"
        f"GRADER A OUTPUT\n{output_a}\n\nGRADER B OUTPUT\n{output_b}"
    )


# --- Mechanical rules (rubric rules, control_rules) --------------------

FIRST, SECOND = "FIRST_HAND", "SECOND_HAND"


def valid_p(item: dict) -> bool:
    return bool(
        item["identity_valid"]
        and item["opened"]
        and item["in_scope"]
        and item["role_ok"]
        and item["origin"] not in (None, "GENERATED_OR_REDACTED")
    )


def validity(cites: list[dict], judgement: dict) -> tuple[dict, dict]:
    """{citation number: valid_P} and {citation number: valid_X}."""
    nonconv = {
        c["citation"]: c["non_conversational"] for c in judgement["citations"]
    }
    vp = {n: valid_p(c) for n, c in enumerate(cites, start=1)}
    vx = {
        n: vp[n] and c["origin"] == "ORIGINAL" and not nonconv.get(n, False)
        for n, c in enumerate(cites, start=1)
    }
    return vp, vx


def _supported(item: dict, valid: dict, kinds: tuple) -> bool:
    return any(
        s["type"] in kinds and valid.get(s["citation"], False)
        for s in item["support"]
    )


def answer_rules(
    cites: list[dict], judgement: dict, abstained: bool, format_fail: bool
) -> dict:
    """rubric rules P, R and X for one answerable answer."""
    if abstained or format_fail:
        return {"P": False, "R": False, "X": False}
    vp, vx = validity(cites, judgement)
    claims, additions = judgement["claims"], judgement["additions"]
    complete = all(c["present"] and not c["contradicted"] for c in claims)
    p_ok = (
        complete
        and all(vp.values())
        and all(_supported(c, vp, (FIRST, SECOND)) for c in claims)
    )
    r_ok = p_ok and all(_supported(a, vp, (FIRST, SECOND)) for a in additions)
    x_ok = (
        complete
        and all(vx.values())
        and all(_supported(s, vx, (FIRST,)) for s in claims + additions)
    )
    return {"P": p_ok, "R": r_ok, "X": x_ok}


def w_rule(
    cites: list[dict], judgement: dict, abstained: bool, format_fail: bool
) -> bool:
    """control_rules.W (same outcome under P, R and X)."""
    return (
        not format_fail
        and abstained
        and not cites
        and not judgement["asserts_substantive_claim"]
    )


def mirror_rule(
    cites: list[dict], judgement: dict, abstained: bool, format_fail: bool
) -> bool:
    """control_rules.MIRROR; in_scope is against the mirror scope."""
    if format_fail or any(
        not c["in_scope"] or c["forbidden_match"] for c in cites
    ):
        return False
    if abstained:
        return w_rule(cites, judgement, abstained, format_fail)
    _, vx = validity(cites, judgement)
    return all(vx.values()) and all(
        _supported(a, vx, (FIRST,)) for a in judgement["additions"]
    )


def needs_adjudication(first: dict | None, second: dict | None) -> bool:
    """§5.4: G1 and G2 disagree on P, R, X or a control outcome."""
    return first is None or second is None or first != second


def intersect(first: dict, second: dict, cites: list | None = None) -> dict:
    """§5.4 unadjudicated answers: an item counts only if both agree.

    Additions are free text and cannot be aligned, so the shared count
    of unsupported additions (X sense: no FIRST_HAND support from a
    valid_X citation) is the smaller of the two graders' counts.
    """
    weaker = {t: i for i, t in enumerate(SUPPORT_TYPES)}

    def support(a: dict, b: dict) -> list:
        theirs = {s["citation"]: s["type"] for s in b["support"]}
        return [
            {
                "citation": s["citation"],
                "type": min(s["type"], theirs[s["citation"]], key=weaker.get),
            }
            for s in a["support"]
            if s["citation"] in theirs
        ]

    def unsupported(judgement: dict) -> int:
        vx = None if cites is None else validity(cites, judgement)[1]
        return sum(
            not any(
                s["type"] == FIRST
                and (vx is None or vx.get(s["citation"], False))
                for s in a["support"]
            )
            for a in judgement["additions"]
        )

    other = {c["claim"]: c for c in second["claims"]}
    nonconv = {
        c["citation"]: c["non_conversational"] for c in second["citations"]
    }
    return {
        "claims": [
            {
                "claim": c["claim"],
                "present": c["present"] and other[c["claim"]]["present"],
                "contradicted": c["contradicted"]
                and other[c["claim"]]["contradicted"],
                "support": support(c, other[c["claim"]]),
            }
            for c in first["claims"]
        ],
        "citations": [
            {
                "citation": c["citation"],
                "non_conversational": c["non_conversational"]
                and nonconv.get(c["citation"], False),
            }
            for c in first["citations"]
        ],
        "additions": min((first, second), key=unsupported)["additions"],
        "unsupported_additions": min(unsupported(first), unsupported(second)),
    }


def cohen_kappa(first: list[bool], second: list[bool]) -> dict:
    """§5.5 Cohen's kappa for two binary raters, with raw agreement."""
    n = len(first)
    if n == 0 or n != len(second):
        return {"kappa": None, "raw_agreement": None, "n": n}
    agree = sum(a == b for a, b in zip(first, second)) / n
    p1, p2 = sum(first) / n, sum(second) / n
    chance = p1 * p2 + (1 - p1) * (1 - p2)
    kappa = None if chance == 1 else (agree - chance) / (1 - chance)
    return {"kappa": kappa, "raw_agreement": agree, "n": n}


def _parse(text: str | None, packet: dict) -> tuple[dict | None, str | None]:
    try:
        return parse_grader_output(text or "", packet), None
    except GraderOutputError as error:
        return None, str(error)


def unit_rules(mech: dict, judgement: dict | None) -> dict | None:
    """P/R/X for answers, CONTROL for W and MIRROR units."""
    cites, abstained, fail = (
        mech["citations"],
        mech["abstained"],
        mech["format_fail"],
    )
    if judgement is None and not fail:
        return None
    if mech["kind"] == "ANSWER":
        return answer_rules(cites, judgement, abstained, fail)
    rule = w_rule if mech["kind"] == "W" else mirror_rule
    return {"CONTROL": rule(cites, judgement, abstained, fail)}


def decide(
    packet: dict,
    mech: dict,
    g1: str | None,
    g2: str | None,
    adjudicator: str | None = None,
) -> dict:
    """One answer: both graders' rules, adjudication need, final rules.

    mech: {kind: ANSWER|W|MIRROR, abstained, format_fail, citations}
    with mechanical citation records (old_citation/new_citation).
    """
    if mech["format_fail"]:
        return {
            "final": unit_rules(mech, None),
            "source": "FORMAT_FAIL",
            "adjudicate": False,
        }
    j1, e1 = _parse(g1, packet)
    j2, e2 = _parse(g2, packet)
    r1, r2 = unit_rules(mech, j1), unit_rules(mech, j2)
    result = {
        "g1": r1,
        "g2": r2,
        "g1_error": e1,
        "g2_error": e2,
        "adjudicate": needs_adjudication(r1, r2),
    }
    if not result["adjudicate"]:
        return result | {
            "final": r1,
            "source": "AGREED",
            "judgement": intersect(j1, j2, mech["citations"]),
        }
    if adjudicator is None:
        return result | {"final": None, "source": "PENDING_ADJUDICATION"}
    ja, ea = _parse(adjudicator, packet)
    return result | {
        "final": unit_rules(mech, ja),
        "source": "ADJUDICATED" if ja is not None else "ADJUDICATOR_INVALID",
        "adjudicator_error": ea,
        "judgement": ja,
    }


def reliability(rows: list[dict]) -> dict:
    """§5.5 kappa G1 vs G2: X (primary), P, R pooled and per arm over
    graded answerable answers; controls reported separately."""

    def kappas(items: list[dict], rule: str) -> dict:
        arms = sorted({r["arm"] for r in items})
        pick = [(r["g1"][rule], r["g2"][rule]) for r in items]
        return {
            "pooled": cohen_kappa([a for a, _ in pick], [b for _, b in pick]),
            "by_arm": {
                arm: cohen_kappa(
                    [r["g1"][rule] for r in items if r["arm"] == arm],
                    [r["g2"][rule] for r in items if r["arm"] == arm],
                )
                for arm in arms
            },
        }

    graded = [r for r in rows if r.get("g1") and r.get("g2")]
    answers = [r for r in graded if r["kind"] == "ANSWER"]
    controls = [r for r in graded if r["kind"] != "ANSWER"]
    result = {
        "answerable": {
            rule: kappas(answers, rule) for rule in ("X", "P", "R")
        },
        "controls": kappas(controls, "CONTROL"),
    }
    kappa_x = result["answerable"]["X"]["pooled"]["kappa"]
    result["low_grader_agreement"] = kappa_x is not None and kappa_x < 0.60
    result["kappa_x_undefined"] = kappa_x is None
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI for the H1 launcher.

    prompt PACKET                     grader prompt (G1 opus, G2 sonnet)
    adjudicator-prompt PACKET G1 G2   A/B ordered by H[32:48]
    decide PACKET MECH G1 G2 [ADJ]    rules, adjudication need, final
    G1/G2/ADJ are files holding each agent's raw final message.
    """
    parser = argparse.ArgumentParser(prog="trial_harness.grading")
    parser.add_argument(
        "command", choices=("prompt", "adjudicator-prompt", "decide")
    )
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--trial", type=Path)
    args = parser.parse_args(argv)
    packet = json.loads(args.files[0].read_text())
    if args.command == "decide":
        mech = json.loads(args.files[1].read_text())
        texts = [f.read_text() for f in args.files[2:5]]
        print(json.dumps(decide(packet, mech, *texts), indent=1))
        return 0
    from trial_harness import prep  # lazy: prep imports this module

    trial = args.trial or prep.TRIAL
    rubric = prep.rubric(trial)
    if args.command == "prompt":
        print(grader_prompt(packet, rubric))
        return 0
    outputs = {
        "G1": args.files[1].read_text(),
        "G2": args.files[2].read_text(),
    }
    first, second = ab_order(prep.protocol_digest(trial), packet["answer_id"])
    print(adjudicator_prompt(packet, rubric, outputs[first], outputs[second]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
