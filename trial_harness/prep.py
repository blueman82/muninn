"""Trial-1 data preparation (protocol §0, §1.1, §1.2, §1.3, §2).

Every output goes under TRIAL; REPO is only ever read.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import random
import subprocess
from pathlib import Path

from trial_harness import fence, grading
from trial_harness.extractor import Corpus, record_hash, validate_file
from trial_harness.manifest import (
    make_dirs,
    sha256_file,
    write_json,
    write_private,
)
from trial_harness.origin import OriginIndex, validate

TRIAL = Path(
    "/Users/garyharr/.coderails/agentic-loop/"
    "-Users-garyharr-Github-provenance-context-.git/"
    "1a23fdfe-87cc-4053-869b-3145ef08c81c/trial"
)
REPO = Path("/Users/garyharr/Github/provenance-context-build")

# §0 frozen inputs, REPO-relative, full sha256 recomputed 2026-09-30.
FROZEN_INPUTS = {
    "v7-evidence/private-40-labels-v4.json": (
        "bc86b49301a607400fc25c4f421c99ebb617d30393967cf17b529bca392f196c"
    ),
    "v7-evidence/round3/arms/N/packets.json": (
        "6fef65e157f6359cbead98bae0092639ab7a5f89b236052c240ffeebb5ff33da"
    ),
    "v7-evidence/round4/reader_trial.py": (
        "b4b1491954d23afdfa23e2d7f2a172ec2756e8448d187121a1b19e6b73b7762c"
    ),
    "v7-evidence/direct-source-snapshot-v3.sqlite": (
        "492f872b7701c60e74ee24212a94bdb0fe5331d7f0ac233d618a98fc239c50e4"
    ),
    "v7-evidence/direct-source-inventory-v3.json": (
        "b6b106502d07aa73a174ee8a0fb7375d120dda54461fd6f3cfb643f7eab9640a"
    ),
    "v7-evidence/round4/reader_et.jsonl": (
        "559806958791dfecb048bd7b72ec40beb3e663aafe4ec5c7cea335fa1100c60f"
    ),
    "v7-evidence/round4/reader_sm.jsonl": (
        "d003726153a180412a9167898aaaf42994aafeff379f0595b15f5e7fd0c54117"
    ),
    "v7-evidence/round4/reader_trace.jsonl": (
        "520496bfe2c9f558e5f1e8faefded2167da5861f08bf6a02dd7343f64c9f2674"
    ),
    "v7-evidence/round4/grades_s.jsonl": (
        "d7a8140820a3d4d6ca4cd4cb425e5496ed1fced40e8d6a325f074f5166d9361a"
    ),
    "v7-evidence/round4/grades_et.jsonl": (
        "332f38c2f12f039c691acc19e34f9572e01bd13be570c99f137eb8facb70ae6a"
    ),
    "v7-evidence/round4/grades_mw.jsonl": (
        "26cdbbcd55b100ff94115c1ee7857e1b9d936c9fbfab2e7d46512725d69c53ea"
    ),
    "v7-evidence/round4/grades_eq.jsonl": (
        "d01afed3297405b2a1e7c1202898498a5eca6b629d30aea389a3372d26307773"
    ),
    "v7-evidence/round4/reader_protocol.md": (
        "f33bd37acb5155d8d513b508d65dcb3a85a8b8fd2280a530fc66cec9aec67e64"
    ),
    "v7-evidence/round4/grading_protocol.md": (
        "6a7695d97ae432ece1882a3b5c7fb235e7166331c04e61aa508346e6534005ec"
    ),
}
OLD_ARM_FILES = (
    "v7-evidence/round4/reader_trial.py",
    "v7-evidence/private-40-labels-v4.json",
    "v7-evidence/round3/arms/N/packets.json",
    "v7-evidence/direct-source-snapshot-v3.sqlite",
)


def copy_old_arm(repo: Path, dest: Path, expected: dict[str, str]) -> None:
    """§1.1: APFS-clone each input to dest at the same relative path.

    Copies are regular files (never links), mode 0444, and must hash to
    the frozen value; HASHES.ok is written only when every copy passes.
    """
    for rel, digest in sorted(expected.items()):
        copy = dest / rel
        if not copy.exists():
            make_dirs(copy.parent)
            subprocess.run(["cp", "-c", repo / rel, copy], check=True)
            copy.chmod(0o444)
        info, origin = copy.lstat(), (repo / rel).stat()
        if (
            copy.is_symlink()
            or info.st_nlink != 1
            or ((info.st_dev, info.st_ino) == (origin.st_dev, origin.st_ino))
        ):
            raise ValueError(f"copy is a link: {rel}")
        if sha256_file(copy) != digest:
            raise ValueError(f"sha256 mismatch: {rel}")
    lines = "".join(f"{h}  {rel}\n" for rel, h in sorted(expected.items()))
    write_private(dest / "HASHES.ok", lines.encode())


def read_manifest_tsv(path: Path) -> dict[str, tuple[int, str]]:
    rows = {}
    for line in Path(path).read_text().splitlines():
        if line and not line.startswith("#"):
            rel, size, digest = line.split("\t")
            rows[rel] = (int(size), digest)
    return rows


def verify_mirror(
    mirror: Path, manifest_tsv: Path, fenced: set[str] = frozenset()
) -> dict:
    """§1.2: every listed file is present with its size and sha256.

    Fenced files were deleted on purpose and are expected to be absent.
    """
    listed = read_manifest_tsv(manifest_tsv)
    present = {
        str(p.relative_to(mirror))
        for p in mirror.rglob("*.jsonl")
        if p.is_file()
    }
    missing, changed = [], []
    for rel, (size, digest) in sorted(listed.items()):
        if rel in fenced:
            if rel in present:
                changed.append(rel)
        elif rel not in present:
            missing.append(rel)
        elif (mirror / rel).stat().st_size != size or (
            sha256_file(mirror / rel) != digest
        ):
            changed.append(rel)
    unlisted = sorted(present - set(listed))
    return {
        "listed": len(listed),
        "present": len(present),
        "fenced_absent": len(set(fenced) - present),
        "missing": missing,
        "changed": changed,
        "unlisted": unlisted,
        "ok": not (missing or changed or unlisted),
    }


def protocol_digest(trial: Path) -> str:
    """H = sha256 of trial-protocol.md, checked against FROZEN.sha256."""
    recorded = {}
    for line in Path(trial, "FROZEN.sha256").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        recorded[name.strip()] = digest
    actual = sha256_file(Path(trial, "trial-protocol.md"))
    if recorded.get("trial-protocol.md") != actual:
        raise ValueError("trial-protocol.md does not match FROZEN.sha256")
    return actual


def unit_ids(qids: list[str], answerable: set[str]) -> list[str]:
    """§2: OLD and NEW for every question, plus NEW mirror controls."""
    units = [f"{arm}:{q}" for q in qids for arm in ("OLD", "NEW")]
    units += [f"NEW:{q}:MIRROR" for q in qids if q in answerable]
    return units


def assignment(units: list[str], digest: str) -> dict:
    """§2: sort unit ids, shuffle with random.Random(int(H[0:16], 16))."""
    seed = int(digest[0:16], 16)
    order = sorted(units)
    random.Random(seed).shuffle(order)
    return {
        "protocol_sha256": digest,
        "seed_hex": digest[0:16],
        "seed": seed,
        "rule": "sorted(unit_ids) then random.Random(seed).shuffle",
        "max_concurrent": 4,
        "order": order,
    }


IDENTITY = ("provider", "source_path", "source_line", "source_hash")


def labelled_identities(questions: dict) -> dict[tuple, dict]:
    """The distinct labelled originals: required, then control-forbidden."""
    found: dict[tuple, dict] = {}
    for qid, q in sorted(questions.items()):
        groups = [
            ("required", q.get("required_originals", [])),
            (
                "forbidden",
                q["control_expectation"].get("forbidden_originals", []),
            ),
            (
                "forbidden",
                q.get("mirror_control", {}).get("forbidden_originals", []),
            ),
        ]
        for kind, originals in groups:
            for original in originals:
                key = tuple(original[f] for f in IDENTITY)
                item = found.setdefault(
                    key, dict(zip(IDENTITY, key), questions=[], kind=kind)
                )
                if qid not in item["questions"]:
                    item["questions"].append(qid)
                if kind == "required":
                    item["kind"] = "required"
    return found


def availability(corpus, entries: list[dict], questions: dict) -> dict:
    """§1.2 at T0: file present, line hash, captured-prefix hash."""
    captured = {(e["provider"], e["source_path"]): e for e in entries}
    rows = []
    for key, item in sorted(labelled_identities(questions).items()):
        provider, source_path, line, digest = key
        path = corpus.path(provider, source_path)
        raw = corpus.line(provider, source_path, line) if path else None
        entry = captured.get((provider, source_path))
        prefix_ok = False
        if path is not None and entry is not None:
            with open(path, "rb") as source:
                prefix = source.read(entry["whole_file_bytes"])
            prefix_ok = (
                len(prefix) == entry["whole_file_bytes"]
                and hashlib.sha256(prefix).hexdigest()
                == entry["whole_file_sha256"]
            )
        line_ok = raw is not None and record_hash(raw) == digest
        rows.append(
            dict(
                item,
                file_present=path is not None,
                line_hash_ok=line_ok,
                prefix_hash_ok=prefix_ok,
                available=path is not None and line_ok and prefix_ok,
            )
        )
    drift = sorted(
        {q for r in rows if not r["available"] for q in r["questions"]}
    )
    kinds = [r["kind"] for r in rows]
    return {
        "identities": rows,
        "corpus_drift": drift,
        "summary": {
            "identities": len(rows),
            "required": kinds.count("required"),
            "forbidden": kinds.count("forbidden"),
            "available": sum(r["available"] for r in rows),
            "corpus_drift_questions": len(drift),
        },
    }


def frozen_json(trial: Path, rel: str) -> dict:
    """Load a §0 input (OLD-arm copy if present, else REPO) after hashing."""
    path = trial / "old-arm" / rel
    path = path if path.exists() else REPO / rel
    if sha256_file(path) != FROZEN_INPUTS[rel]:
        raise ValueError(f"frozen input changed: {rel}")
    return json.loads(path.read_text())


def rubric(trial: Path) -> dict:
    """grading-rubric.json, checked against FROZEN.sha256."""
    protocol_digest(trial)
    recorded = dict(
        reversed(line.split(maxsplit=1))
        for line in Path(trial, "FROZEN.sha256").read_text().splitlines()
    )
    path = Path(trial, "grading-rubric.json")
    if recorded.get("grading-rubric.json") != sha256_file(path):
        raise ValueError("grading-rubric.json does not match FROZEN.sha256")
    return json.loads(path.read_text())


def corpus(trial: Path):

    inventory = frozen_json(
        trial, "v7-evidence/direct-source-inventory-v3.json"
    )
    return Corpus(trial / "corpus-T0", inventory), inventory


def needles(questions: dict) -> dict[str, str]:
    """F2 search strings: every query and every required_verbatim."""
    out = {}
    for qid, q in sorted(questions.items()):
        out[f"query:{qid}"] = q["query"]
        for index, text in enumerate(q["required_verbatim"]):
            out[f"required:{qid}:{index}"] = text
    return out


def fenced_paths(trial: Path) -> set[str]:
    path = trial / "fence-list.tsv"
    if not path.exists():
        return set()
    rows = [line.split("\t") for line in path.read_text().splitlines()[1:]]
    return {row[0] for row in rows if row[5] == "DELETE"}


def cmd_old_arm(trial: Path, args) -> dict:
    expected = {rel: FROZEN_INPUTS[rel] for rel in OLD_ARM_FILES}
    copy_old_arm(REPO, trial / "old-arm", expected)
    return {"old_arm": str(trial / "old-arm"), "files": len(expected)}


def cmd_mirror_verify(trial: Path, args) -> dict:
    result = verify_mirror(
        trial / "corpus-T0",
        trial / "corpus-T0.manifest.tsv",
        fenced=fenced_paths(trial),
    )

    write_json(trial / "mirror-verify.json", result)
    return {k: v for k, v in result.items() if not isinstance(v, list)} | {
        k: len(v) for k, v in result.items() if isinstance(v, list)
    }


def cmd_availability(trial: Path, args) -> dict:

    questions = rubric(trial)["questions"]
    mirror, inventory = corpus(trial)
    result = availability(mirror, inventory["entries"], questions)
    write_json(trial / "availability.json", result)
    return result["summary"] | {"corpus_drift": result["corpus_drift"]}


def cmd_fence(trial: Path, args) -> dict:

    if fenced_paths(trial):
        raise ValueError("fence already applied; see fence-list.tsv")
    check = verify_mirror(
        trial / "corpus-T0", trial / "corpus-T0.manifest.tsv"
    )
    if not check["ok"]:
        raise ValueError("T0 mirror does not match its manifest")
    questions = rubric(trial)["questions"]
    mirror, _ = corpus(trial)
    labelled: dict[tuple[str, str], set[str]] = {}
    for item in labelled_identities(questions).values():
        key = (item["provider"], item["source_path"])
        labelled.setdefault(key, set()).update(item["questions"])
    rows = fence.compute(mirror, needles(questions), labelled)
    target = trial / ("fence-list.tsv" if args.apply else "fence-plan.tsv")
    write_private(target, fence.tsv(rows).encode())
    deleted = fence.apply(trial / "corpus-T0", rows) if args.apply else 0
    reasons = ["+".join(row["reasons"]) for row in rows]
    return {
        "written": str(target),
        "rows": len(rows),
        "by_reason": {r: reasons.count(r) for r in sorted(set(reasons))},
        "kept_g1": [r["path"] for r in rows if r["action"] == "KEEP_G1"],
        "deleted": deleted,
    }


def cmd_assign(trial: Path, args) -> dict:

    questions = rubric(trial)["questions"]
    answerable = {q for q, v in questions.items() if v["answerable"]}
    units = unit_ids(sorted(questions), answerable)
    result = assignment(units, protocol_digest(trial))
    write_json(trial / "assignment.json", result)
    return {"units": len(units), "seed_hex": result["seed_hex"]}


SNAPSHOT = "v7-evidence/direct-source-snapshot-v3.sqlite"


def labelled_files(questions: dict) -> dict[tuple[str, str], list[str]]:
    files: dict[tuple[str, str], list[str]] = {}
    for item in labelled_identities(questions).values():
        qids = files.setdefault((item["provider"], item["source_path"]), [])
        qids.extend(q for q in item["questions"] if q not in qids)
    return files


def cmd_validate_extractor(trial: Path, args) -> dict:

    snapshot = trial / "old-arm" / SNAPSHOT
    if sha256_file(snapshot) != FROZEN_INPUTS[SNAPSHOT]:
        raise ValueError("snapshot copy changed")
    mirror, inventory = corpus(trial)
    prefix = {
        (e["provider"], e["source_path"]): e["whole_file_bytes"]
        for e in inventory["entries"]
    }
    files = []
    for key, qids in sorted(
        labelled_files(rubric(trial)["questions"]).items()
    ):
        result = validate_file(mirror, snapshot, *key, prefix[key])
        files.append(dict(result, questions=sorted(qids)))
    summary = {
        "files": len(files),
        "passed": sum(f["pass"] for f in files),
        "snapshot_events": sum(f["snapshot_events"] for f in files),
        "matched": sum(f["matched"] for f in files),
        "rule": "E(T0 file) == snapshot v3 (role, ws-normalised text, cwd)"
        " on every captured-prefix line; no extra message rows",
    }
    write_json(
        trial / "extractor-validation.json",
        {"summary": summary, "files": files},
    )
    return summary


HIST_FILES = (
    "v7-evidence/round4/reader_et.jsonl",
    "v7-evidence/round4/reader_sm.jsonl",
)


def hist_answers() -> dict[str, dict]:
    """§1.3: the 40 saved round-four answers, keyed by question id."""
    answers = {}
    for rel in HIST_FILES:
        if sha256_file(REPO / rel) != FROZEN_INPUTS[rel]:
            raise ValueError(f"frozen input changed: {rel}")
        for line in (REPO / rel).read_text().splitlines():
            if line.strip():
                answer = json.loads(line)
                answers[answer["label_id"]] = answer
    return answers


def origin_index(trial: Path, questions: dict, rules: dict):

    mirror, _ = corpus(trial)
    labelled = {tuple(k) for k in labelled_identities(questions)}
    return mirror, OriginIndex(mirror, rules, labelled)


def cmd_validate_origin(trial: Path, args) -> dict:

    key = rubric(trial)
    questions = key["questions"]
    _, index = origin_index(trial, questions, key["origin_rules"])
    hist = {q: a["citations"] for q, a in hist_answers().items()}
    required = [
        o
        for q in sorted(questions)
        for o in questions[q].get("required_originals", [])
    ]
    result = validate(
        index,
        hist,
        replay={"Q1", "M6"},
        injected={"E2", "M1"},
        required=required,
    )
    classes = collections.Counter()
    rules = collections.Counter()
    for provider, source, line, ordinal in index.events:
        found = index.classify(provider, source, line, ordinal)
        classes[found["detector_origin"]] += 1
        rules.update(found["rules"])
    labelled = []
    for item in labelled_identities(questions).values():
        found = index.classify(
            item["provider"], item["source_path"], item["source_line"]
        )
        labelled.append(
            dict(
                item,
                origin=found["origin"],
                detector_origin=found["detector_origin"],
                rules=found["rules"],
            )
        )
    disagreements = [x for x in labelled if x["detector_origin"] != "ORIGINAL"]
    result |= {
        "rubric_clause": key["origin_rules"]["detector_validation"],
        "corpus_events": len(index.events),
        "detector_classes": dict(classes),
        "rule_counts": dict(rules),
        "label_authority_disagreements": disagreements,
    }
    write_json(trial / "detector-validation.json", result)
    return {
        "status": result["status"],
        "replay_failed": len(result["checks"]["replay"]["failed"]),
        "injected_failed": len(result["checks"]["injected"]["failed"]),
        "required_flagged": [
            (x["identity"]["source_line"], x["rules"])
            for x in result["checks"]["required_unflagged"]["flagged"]
        ],
        "disagreement_questions": sorted(
            {q for x in disagreements for q in x["questions"]}
        ),
        "detector_classes": dict(classes),
    }


TRACE = "v7-evidence/round4/reader_trace.jsonl"
POOLS = "v7-evidence/round3/arms/N/packets.json"


def fenced_keys(trial: Path, mirror) -> set[tuple[str, str]]:
    return {
        mirror.key_for(f"{mirror.home}/{path}") for path in fenced_paths(trial)
    }


def cmd_hist(trial: Path, args) -> dict:
    """§1.3: blind-gradeable packets for the 40 saved round-four answers."""

    digest = protocol_digest(trial)
    key = rubric(trial)
    questions = key["questions"]
    if sha256_file(REPO / TRACE) != FROZEN_INPUTS[TRACE]:
        raise ValueError("round-four trace changed")
    trace = [
        json.loads(line)
        for line in (REPO / TRACE).read_text().splitlines()
        if line.strip()
    ]
    pools = {
        p["label_id"]: grading.pool_refs(p["candidate_citations"])
        for p in frozen_json(trial, POOLS)["packets"]
    }
    mirror, index = origin_index(trial, questions, key["origin_rules"])
    fenced = fenced_keys(trial, mirror)
    out = trial / "hist-anonymized"
    mechanics, hashes, flags = {}, {}, collections.Counter()
    for qid, reply in sorted(hist_answers().items()):
        unit = f"HIST:{qid}"
        answer = grading.answer_id(digest, unit)
        question, refs = questions[qid], pools[qid]
        cites = [
            grading.old_citation(
                mirror,
                index,
                cite,
                question["query_scope"],
                refs,
                grading.hist_opened(
                    trace, qid, grading.pool_index(refs, cite)
                ),
                fenced,
            )
            for cite in reply["citations"]
        ]
        packet = grading.build_packet(
            answer, question, reply, cites, control=not question["answerable"]
        )
        path = out / "packets" / f"{answer}.json"
        write_json(path, packet)
        hashes[answer] = sha256_file(path)
        well_formed = (
            isinstance(reply.get("answer"), str)
            and isinstance(reply.get("citations"), list)
            and isinstance(reply.get("abstained"), bool)
        )
        mechanics[answer] = {
            "unit_id": unit,
            "arm": "HIST",
            "qid": qid,
            "control": not question["answerable"],
            "format_fail": not well_formed,
            "abstained": reply["abstained"],
            "citations": [
                {k: v for k, v in c.items() if k != "text"} for c in cites
            ],
        }
        for cite in cites:
            flags.update(
                k
                for k in ("identity_valid", "opened", "in_scope", "role_ok")
                if cite[k]
            )
            flags[f"origin:{cite['origin']}"] += 1
            flags["citations"] += 1
    write_json(out / "mechanics.json", mechanics)
    write_json(
        out / "index.json",
        {
            "protocol_sha256": digest,
            "answer_id_rule": "sha256(H[16:32] + unit_id)[:10]",
            "packets": hashes,
            "packet_dir": "packets/ (grader input, arm-blind)",
            "private": "mechanics.json maps answer_id to unit and flags;"
            " never shown to graders",
        },
    )
    return {"answers": len(mechanics), "flags": dict(flags)}


def cmd_check_prompts(trial: Path, args) -> dict:
    """The embedded grader preamble and adjudicator prefix are verbatim."""

    protocol_digest(trial)
    text = (trial / "trial-protocol.md").read_text()
    result = {
        "preamble_matches_protocol": grading.preamble_from_protocol(text)
        == grading.GRADER_PREAMBLE,
        "adjudicator_prefix_in_protocol": f'"{grading.ADJUDICATOR_PREFIX}"'
        in text,
        "sha256": {
            name: hashlib.sha256(value.encode()).hexdigest()
            for name, value in (
                ("grader_preamble", grading.GRADER_PREAMBLE),
                ("adjudicator_prefix", grading.ADJUDICATOR_PREFIX),
                ("output_schema", grading.OUTPUT_SCHEMA),
            )
        },
    }
    write_json(trial / "prompt-check.json", result)
    return result


COMMANDS = {
    "assign": cmd_assign,
    "check-prompts": cmd_check_prompts,
    "hist": cmd_hist,
    "validate-origin": cmd_validate_origin,
    "validate-extractor": cmd_validate_extractor,
    "old-arm": cmd_old_arm,
    "mirror-verify": cmd_mirror_verify,
    "availability": cmd_availability,
    "fence": cmd_fence,
}


def main(argv: list[str] | None = None) -> int:

    parser = argparse.ArgumentParser(prog="trial_harness.prep")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--trial", type=Path, default=TRIAL)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    result = COMMANDS[args.command](args.trial, args)
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
