"""§7 analysis: exact McNemar, pre-registered reading, report skeleton."""

from __future__ import annotations

from math import comb

TABLE_LABEL = "DEVELOPMENT EVIDENCE — exposed questions; confirmation NOT_RUN"
HEADLINE_LABEL = (
    "DEVELOPMENT EVIDENCE (exposed questions); concealed confirmation NOT_RUN"
)
MIN_VALID_PAIRS, MAX_FATAL_UNITS, MIN_NET, ALPHA = 30, 2, 6, 0.05
LOW_KAPPA = 0.60


def mcnemar_exact(b: int, c: int) -> float:
    """p = min(1, 2 * sum_{i <= min(b, c)} C(b + c, i) / 2^(b + c))."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(min(b, c) + 1))
    return min(1.0, 2 * tail / 2**n)


def interpret(b: int, c: int) -> str:
    """§7.2: six one-directional net flips and p < 0.05, else noise."""
    p = mcnemar_exact(b, c)
    if c - b >= MIN_NET and p < ALPHA:
        return "NEW_BETTER_DEV"
    if b - c >= MIN_NET and p < ALPHA:
        return "NEW_WORSE_DEV"
    return "NO_DETECTABLE_DIFFERENCE"


def primary(
    old: dict[str, bool],
    new: dict[str, bool],
    exclusions: dict[str, str],
    fatal_units: int,
) -> dict:
    """§7.1 over valid pairs (answerable questions less §7.3 exclusions).

    b = OLD passes and NEW fails; c = OLD fails and NEW passes.
    """
    pairs = sorted((set(old) & set(new)) - set(exclusions))
    b = [q for q in pairs if old[q] and not new[q]]
    c = [q for q in pairs if new[q] and not old[q]]
    if fatal_units > MAX_FATAL_UNITS:
        status = "VOID"
    elif len(pairs) < MIN_VALID_PAIRS:
        status = "INCONCLUSIVE"
    else:
        status = interpret(len(b), len(c))
    return {
        "valid_pairs": len(pairs),
        "old_pass": sum(old[q] for q in pairs),
        "new_pass": sum(new[q] for q in pairs),
        "b": len(b),
        "c": len(c),
        "net": len(c) - len(b),
        "p": mcnemar_exact(len(b), len(c)),
        "flips": {q: "OLD_ONLY" for q in b} | {q: "NEW_ONLY" for q in c},
        "excluded": dict(sorted(exclusions.items())),
        "status": status,
    }


def _table(rows: list[tuple]) -> str:
    head, *body = rows
    lines = [TABLE_LABEL, "", "| " + " | ".join(map(str, head)) + " |"]
    lines.append("|" + "---|" * len(head))
    lines += ["| " + " | ".join(map(str, row)) + " |" for row in body]
    return "\n".join(lines)


def render(data: dict) -> str:
    """Markdown report; the first paragraph is the headline."""
    result = data["primary"]
    notes = [
        f"**{HEADLINE_LABEL}.** Primary rule X over {result['valid_pairs']}"
        f" valid pairs: {result['status']} (b={result['b']},"
        f" c={result['c']}, p={result['p']:.4g}).",
        "No production-gain or generalisation claim follows from this trial.",
    ]
    kappa = data.get("kappa_x")
    if kappa is None:
        notes.append("Kappa on X undefined or not yet computed.")
    elif kappa < LOW_KAPPA:
        notes.append(f"LOW_GRADER_AGREEMENT (kappa on X = {kappa:.2f}).")
    for finding in data.get("safety", []):
        notes.append(f"Safety finding: {finding}.")
    replication = data.get("replication")
    if replication and abs(replication["c"] - replication["b"]) >= MIN_NET:
        notes.append("round-four baseline did not replicate under isolation.")
    for label in data.get("labels", []):
        notes.append(f"{label}.")
    sections = [
        "# Trial-1 report\n" + "\n".join(notes),
        "## Primary (X, paired OLD vs NEW)",
        _table(
            [
                ("metric", "value"),
                ("valid pairs", result["valid_pairs"]),
                ("OLD pass", result["old_pass"]),
                ("NEW pass", result["new_pass"]),
                ("NEW - OLD", result["net"]),
                ("b (OLD only)", result["b"]),
                ("c (NEW only)", result["c"]),
                ("exact McNemar p", f"{result['p']:.4g}"),
                ("reading", result["status"]),
            ]
        ),
        "## Per-question flips",
        _table(
            [("question", "flip")]
            + sorted(result["flips"].items())
            + ([("(none)", "-")] if not result["flips"] else [])
        ),
    ]
    if replication:
        sections += [
            "## Replication (OLD vs HIST, descriptive)",
            _table(
                [
                    ("b (OLD only)", "c (HIST only)", "p"),
                    (replication["b"], replication["c"], replication["p"]),
                ]
            ),
        ]
    if result["excluded"]:
        sections += [
            "## Exclusions (§7.3)",
            _table(
                [("question", "reason")] + sorted(result["excluded"].items())
            ),
        ]
    return "\n\n".join(sections) + "\n"
