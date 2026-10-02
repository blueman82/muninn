"""Erase verification: look for what an erase may have left behind.

Erased text is kept only in memory, as short byte needles and as rare search
terms. After the commit the needles are searched for in every file under the
data directory and the rare terms in the FTS vocabularies.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pctx.erase_collect import Target

NEEDLES, NEEDLE_BYTES = 50, 48
# A shorter head would match unrelated bytes and report false residue.
MIN_NEEDLE_BYTES = 12
# A term in more erased documents than this is ordinary vocabulary, not
# something that identifies the erased text.
RARE_MAX_DOCS = 3
# SQLite's default bound-variable limit is 999 on older builds.
CHUNK = 500


def _texts(conn: sqlite3.Connection, target: Target) -> list[str]:
    """Return the texts about to be erased; they live in memory only."""
    texts: list[str] = []
    for table, column, ids in (
        ("event", "text", target.events),
        ("knowledge", "text", sorted(target.knowledge)),
        ("citation", "quote", sorted(target.citations)),
    ):
        for start in range(0, len(ids), CHUNK):
            chunk = ids[start : start + CHUNK]
            texts += [
                r[0]
                for r in conn.execute(
                    f"SELECT {column} FROM {table} WHERE id IN"
                    f" ({','.join('?' * len(chunk))}) AND {column} NOT NULL",
                    chunk,
                )
            ]
    return texts


def pick_needles(conn: sqlite3.Connection, target: Target) -> list[bytes]:
    """Pick byte needles that identify the texts about to be erased.

    Args:
        conn: Read connection, used before the erase runs.
        target: The plan.

    Returns:
        The first 48 bytes of up to 50 distinct texts, longest text first.
    """
    found: list[bytes] = []
    for text in sorted(_texts(conn, target), key=len, reverse=True):
        # surrogatepass: stored text may hold lone surrogates from a
        # transcript, which strict UTF-8 refuses to encode.
        head = text.encode("utf-8", "surrogatepass")[:NEEDLE_BYTES]
        if len(head) >= MIN_NEEDLE_BYTES and head not in found:
            found.append(head)
        if len(found) == NEEDLES:
            break
    return found


def still_stored(conn: sqlite3.Connection, needle: bytes) -> bool:
    """Tell whether a surviving row holds the same bytes.

    This happens when the same prompt was typed in an erased and a kept
    session; such a needle is not residue.

    Args:
        conn: Read connection.
        needle: Bytes to look for.

    Returns:
        True if any event, knowledge entry or citation quote contains it.
    """
    for table, column in (
        ("event", "text"),
        ("knowledge", "text"),
        ("citation", "quote"),
    ):
        # CAST to BLOB so instr compares bytes, matching the needle.
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE instr(CAST({column} AS BLOB), ?)"
            " LIMIT 1",
            (needle,),
        ).fetchone():
            return True
    return False


def _vocab(conn: sqlite3.Connection, table: str, kind: str) -> str:
    """Create (once per connection) an fts5vocab table; return its name."""
    name = f"pctx_{kind}_{table}"
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS temp.{name}"
        f" USING fts5vocab(main, {table}, {kind})"
    )
    return f"temp.{name}"


def _load_doc_ids(conn: sqlite3.Connection, ids: list[int]) -> None:
    """Fill the temp table that the vocabulary join filters on."""
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS pctx_erase_doc"
        " (id INTEGER PRIMARY KEY)"
    )
    conn.execute("DELETE FROM temp.pctx_erase_doc")
    conn.executemany(
        "INSERT OR IGNORE INTO temp.pctx_erase_doc(id) VALUES (?)",
        [(i,) for i in ids],
    )


def _rare_in(conn: sqlite3.Connection, table: str, ids: list[int]) -> set[str]:
    """Terms of one FTS table found only in a few of the given documents.

    A term counts as rare when at most ``RARE_MAX_DOCS`` documents hold it
    and all of them are among ``ids``.

    Args:
        conn: Read connection.
        table: FTS table name.
        ids: Document ids that are being erased.

    Returns:
        The rare terms.
    """
    _load_doc_ids(conn, ids)
    instance, row = (
        _vocab(conn, table, "instance"),
        _vocab(conn, table, "row"),
    )
    counts = conn.execute(
        f"SELECT v.term, count(DISTINCT v.doc) FROM {instance} v"
        " JOIN temp.pctx_erase_doc d ON d.id = v.doc GROUP BY v.term"
    ).fetchall()
    rare: set[str] = set()
    for term, erased in counts:
        if erased > RARE_MAX_DOCS:
            continue
        # The term is exclusive to the erased documents only if the whole
        # index holds it in no more documents than the erased ones.
        df = conn.execute(
            f"SELECT doc FROM {row} WHERE term = ?", (term,)
        ).fetchone()
        if df is not None and df[0] == erased:
            rare.add(term)
    return rare


def rare_terms(
    conn: sqlite3.Connection,
    event_ids: list[int],
    knowledge_ids: list[int],
) -> dict[str, set[str]]:
    """Find terms that only the erased documents contain.

    The terms stay in memory and are never stored, so they cannot become
    residue themselves.

    Args:
        conn: Read-write connection, before the erase runs.
        event_ids: Event rows about to be deleted.
        knowledge_ids: Knowledge rows about to be scrubbed.

    Returns:
        Per FTS table, the terms in at most three documents, all erased.
    """
    rare: dict[str, set[str]] = {}
    for table, ids in (
        ("event_fts", event_ids),
        ("knowledge_fts", knowledge_ids),
    ):
        rare[table] = _rare_in(conn, table, ids) if ids else set()
    return rare


def vocab_left(conn: sqlite3.Connection, rare: dict[str, set[str]]) -> int:
    """Count the rare terms an FTS index still knows after the erase.

    Args:
        conn: Connection after the commit.
        rare: Output of ``rare_terms``.

    Returns:
        How many terms remain; 0 means the indexes are clean.
    """
    left = 0
    for table, terms in rare.items():
        if terms:
            row = _vocab(conn, table, "row")
            left += sum(
                conn.execute(
                    f"SELECT 1 FROM {row} WHERE term = ?", (term,)
                ).fetchone()
                is not None
                for term in terms
            )
    return left


def residue_scan(home: Path, needles: list[bytes]) -> list[str]:
    """Search the data directory for erased bytes.

    The scan covers every regular file, the SQLite journal included, because
    deleted text can linger in a rollback journal. Symlinks are skipped so
    the scan never reads outside ``home``. Whole files are read; revisit
    with a streaming scan if databases grow far beyond the doctor's size
    warning.

    Args:
        home: Data directory.
        needles: Byte strings to look for.

    Returns:
        Paths relative to ``home`` of files holding any needle.
    """
    if not needles:
        return []
    hits: list[str] = []
    for path in sorted(home.rglob("*")):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            if any(needle in data for needle in needles):
                hits.append(path.relative_to(home).as_posix())
    return hits
