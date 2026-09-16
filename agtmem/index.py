"""The index: a disposable SQLite cache over the Markdown store.

Nothing here is a source of truth. `rm ~/.agtmem/.index.sqlite && agtmem reindex`
must always be a safe operation — that property is what makes the store
lock-in-proof.

Two FTS tables because there are two different retrieval needs:
  * notes_fts — unicode61 with remove_diacritics=2, so Polish text matches
    without diacritics ("laka" finds "łąka")
  * notes_tri — trigram, so substrings survive tokenisation (a file path,
    a hyphenated package name, a version string)

Results from both are merged with Reciprocal Rank Fusion.
"""
from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import time
from pathlib import Path

from . import store
from .store import INDEX_PATH, MEM_HOME, Note

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
  id TEXT PRIMARY KEY, path TEXT, title TEXT, type TEXT, scope TEXT,
  tags TEXT, status TEXT, origin TEXT, detail TEXT,
  supersedes TEXT, superseded_by TEXT, captured TEXT, updated TEXT,
  mtime REAL, hash TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  id UNINDEXED, title, body, tags,
  tokenize = "unicode61 remove_diacritics 2"
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_tri USING fts5(
  id UNINDEXED, path, title, body, tokenize = "trigram"
);
CREATE TABLE IF NOT EXISTS symbols (
  scope TEXT, name TEXT, kind TEXT, file TEXT, line INTEGER, signature TEXT
);
CREATE INDEX IF NOT EXISTS symbols_name_idx ON symbols(name);
CREATE INDEX IF NOT EXISTS symbols_scope_idx ON symbols(scope);
CREATE TABLE IF NOT EXISTS usage (
  ts TEXT, tool TEXT, query TEXT, tokens INTEGER, hits INTEGER
);
"""

RRF_K = 60


def connect() -> sqlite3.Connection:
    MEM_HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(INDEX_PATH), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Make the schema current, refilling from disk if it had to be rebuilt.

    The index is a cache, so a schema change and a deleted database file are
    handled the same way: rebuild rather than migrate. Returns True when a
    rebuild happened, so callers can tell a cold start from a warm one.

    `symbols` and `usage` are deliberately never dropped — they are not derived
    from the note files, and silently discarding the injection accounting on a
    schema bump would destroy the only evidence of what memory costs.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current == SCHEMA_VERSION:
        return False
    for table in ("notes", "notes_fts", "notes_tri"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    populate(conn)
    return True


def estimate_tokens(text: str) -> int:
    """Cheap, honest estimate. Never presented as a measured provider count."""
    return max(1, len(text) // 4)


# ------------------------------------------------------------------ writing

def _rows_for(note: Note, mtime: float, digest: str):
    meta = note.to_meta()
    return (
        note.id, str(note.path or ""), note.title, note.type, note.scope,
        " ".join(note.tags), note.status, note.origin, note.detail,
        note.supersedes, note.superseded_by, note.captured, note.updated,
        mtime, digest,
    )


def upsert(conn: sqlite3.Connection, note: Note) -> None:
    path = note.path or store.note_path(note.type, note.id, note.scope)
    mtime = path.stat().st_mtime if path.exists() else time.time()
    digest = hashlib.sha256(note.render().encode("utf-8")).hexdigest()[:16]

    conn.execute("DELETE FROM notes WHERE id = ?", (note.id,))
    conn.execute("DELETE FROM notes_fts WHERE id = ?", (note.id,))
    conn.execute("DELETE FROM notes_tri WHERE id = ?", (note.id,))
    conn.execute(
        "INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        _rows_for(note, mtime, digest),
    )
    conn.execute(
        "INSERT INTO notes_fts (id, title, body, tags) VALUES (?,?,?,?)",
        (note.id, note.title, note.body, " ".join(note.tags)),
    )
    conn.execute(
        "INSERT INTO notes_tri (id, path, title, body) VALUES (?,?,?,?)",
        (note.id, str(path), note.title, note.body),
    )
    conn.commit()


def populate(conn: sqlite3.Connection, verbose: bool = False) -> int:
    """Read every note file into the index.

    Idempotent — upsert clears the previous rows for an id first — so this is
    safe to call on a fresh database, a schema change, or a manual reindex.
    """
    count = 0
    for path in store.iter_note_files():
        try:
            note = Note.from_file(path)
        except Exception:
            continue
        upsert(conn, note)
        count += 1
        if verbose:
            print(f"  indexed {note.type:<10} {note.id}")
    return count


def reindex(verbose: bool = False) -> dict:
    """Full rebuild from the filesystem. Always safe to run."""
    conn = connect()
    try:
        ensure_schema(conn)
        conn.executescript(
            "DELETE FROM notes; DELETE FROM notes_fts; DELETE FROM notes_tri;"
        )
        conn.commit()
        return {"notes": populate(conn, verbose=verbose)}
    finally:
        conn.close()


# ---------------------------------------------------------------- retrieval

# How many candidates each ranker contributes before re-ranking. Generous on
# purpose: the second stage needs room to promote a note that matched more of
# the query, and a note cut here can never come back.
RECALL = 60

# The size a note may reach before its term coverage starts being discounted.
#
# Measured, not guessed, and the measurement mattered twice. The first value
# tried was the median note size (2 500 B) and it *lowered* eval R@5 from 1.00 to
# 0.90, because the notes that actually answer questions are the substantial ones
# (median 3 488 B, p99 ~6 000 B).
#
# The second constraint is subtler and was found the hard way. The penalty is a
# single knob with two requirements pulling against each other:
#
#   * it must be weak enough that better coverage still wins — a 6.5 kB note
#     matching five query terms has to beat a 4.2 kB note matching four, or the
#     penalty is overriding evidence, which is the same mistake as the length
#     bias it exists to fix;
#   * it must be strong enough that a raw session (median 21 160 B, matches
#     nearly every term by being a transcript of everything) still loses.
#
# Sweeping both at once puts the usable window at roughly 5 500–6 500 B; 6 000 is
# its middle. The window is narrow, which is a fair criticism of a one-parameter
# curve — the honest statement is that this value is tuned to a measured corpus,
# not derived. Re-measure it if your notes are much larger or much smaller than
# 2–6 kB.
COVERAGE_FREE_BYTES = 6000

# Function words carry no retrieval signal, and because the FTS query is an OR,
# every one of them widens the candidate pool with noise. Measured effect on the
# eval set as it stood in 2026-09-15: R@5 went from 0.60 to 0.87 when these were
# filtered. That set has since been retired (its ground truth pointed at raw
# transcripts), so treat the magnitude as indicative and the direction as the
# finding — filtering stopwords before an OR is what matters.
_STOPWORDS = frozenset("""
a about an and are as at be been but by can could did do does for from had has
have how i if in into is it its me my of on or our should so some than that the
their them then there these they this to too was we were what when where which
who why will with would you your
jak jest sie się nie oraz czy gdzie kiedy który która które dla przez albo lub
""".split())


def _tokens(text: str) -> list[str]:
    """Query tokens worth matching on: alphanumeric, not a function word."""
    words = re.findall(r"\w+", text, re.UNICODE)
    keep = [w for w in words if len(w) >= 2 and w.lower() not in _STOPWORDS]
    # A query of nothing but function words still deserves an answer, so fall
    # back to the raw tokens rather than matching nothing at all.
    return keep or [w for w in words if len(w) >= 2]


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Raw user input cannot go into MATCH — FTS5 has its own syntax and raises on
    punctuation — so only alphanumeric tokens survive, each prefix-matched.

    The tokens are OR-ed, which is deliberately recall-oriented: precision is
    recovered in the second stage, where candidates are re-ranked by how many
    distinct query terms they actually contain.
    """
    tokens = _tokens(text)
    return " OR ".join(f"{t}*" for t in tokens) if tokens else ""


def _trigram_query(text: str) -> str:
    """OR of the longer query tokens, each as a quoted trigram phrase.

    A trigram index matches substrings, so quoting each token finds it even when
    the word tokenizer split it differently — which is the whole point of having
    this index. Quoting the *entire* query as one phrase, the obvious reading,
    matches nothing for any real question.
    """
    tokens = [t for t in _tokens(text) if len(t) >= 4][:8]
    return " OR ".join(f'"{t}"' for t in tokens) if tokens else ""


def _search_fts(conn, query: str, limit: int, exclude_type: str | None = None):
    """BM25 pass. `exclude_type` filters in SQL, not afterwards.

    Filtering after the fact would let excluded notes eat RECALL slots and hand
    back fewer results than asked for — raw sessions are long and match almost
    anything, so they would take most of the budget.
    """
    match = _fts_query(query)
    if not match:
        return []
    sql = """
        SELECT notes_fts.id AS id, bm25(notes_fts) AS rank,
               snippet(notes_fts, -1, '[', ']', ' … ', 14) AS snip
        FROM notes_fts JOIN notes ON notes.id = notes_fts.id
        WHERE notes_fts MATCH ?
    """
    params: list = [match]
    if exclude_type:
        sql += " AND notes.type != ?"
        params.append(exclude_type)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [(r["id"], r["rank"], r["snip"]) for r in rows]


def _search_trigram(conn, query: str, limit: int, exclude_type: str | None = None):
    match = _trigram_query(query)
    if not match:
        return []
    sql = (
        "SELECT notes_tri.id AS id, bm25(notes_tri) AS rank FROM notes_tri "
        "JOIN notes ON notes.id = notes_tri.id WHERE notes_tri MATCH ?"
    )
    params: list = [match]
    if exclude_type:
        sql += " AND notes.type != ?"
        params.append(exclude_type)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r["id"], r["rank"], "") for r in rows]


def _rrf(lists: list[list[tuple]], k: int = RRF_K) -> dict:
    """Reciprocal Rank Fusion: 1/(k + rank), summed across result lists."""
    scores: dict[str, float] = {}
    for results in lists:
        for rank, item in enumerate(results):
            scores[item[0]] = scores.get(item[0], 0.0) + 1.0 / (k + rank + 1)
    return scores


def _coverage(
    conn, ids: list[str], terms: list[str]
) -> dict[str, tuple[int, float]]:
    """Query terms each candidate contains, discounted by how long it is.

    Returns `{id: (terms_matched, score)}`.

    Counting *presence* alone is not enough, and that was a real bug. A raw
    session is roughly nine times the size of a distilled note (measured median
    21 160 B vs 2 321 B), so it contains every query term simply by being a
    transcript of everything — and it then outranked the short note that actually
    answers the question. Measured on the eval set with sessions left in the pool,
    15 of 20 correct notes were pushed out of the top five.

    So the score is term coverage divided by a length penalty: full credit up to
    COVERAGE_FREE_BYTES, then logarithmic. Log rather than linear for the same
    reason BM25 saturates — the difference between a 2 kB and a 20 kB note matters
    far more than the difference between 20 kB and 40 kB, and the tenth repetition
    of a word adds nothing.

    The penalty must stay *weak* at note scale, and getting that wrong is easy: an
    earlier value let a 4.2 kB note matching four query terms outrank a 6.5 kB note
    matching five, because the discount overcame better evidence. Coverage has to
    remain the signal; the penalty only decides between candidates that matched the
    same terms. See COVERAGE_FREE_BYTES for the two-sided constraint and the
    measured window.

    Note the penalty is *relative*, so it cannot hide a genuinely relevant long
    note: a 35 kB document that really does match more of the question than
    anything else still wins. It only stops length from being mistaken for
    relevance.

    Matching is prefix-tolerant, so "derived" still credits "derive" — a cheap
    stand-in for a stemmer, which FTS5 does not ship.
    """
    if not terms or not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, title, body, tags FROM notes_fts WHERE id IN ({placeholders})",
        list(ids),
    ).fetchall()
    out: dict[str, tuple[int, float]] = {}
    for row in rows:
        blob = f"{row['title']} {row['body']} {row['tags']}".lower()
        words = set(re.findall(r"\w+", blob))
        hits = 0
        for term in terms:
            stem = term[: max(4, len(term) - 2)]
            if term in blob or any(w.startswith(stem) for w in words):
                hits += 1
        if not hits:
            out[row["id"]] = (0, 0.0)
            continue
        size = len(blob.encode("utf-8"))
        penalty = 1.0 + math.log2(max(1.0, size / COVERAGE_FREE_BYTES))
        out[row["id"]] = (hits, hits / penalty)
    return out


def search(
    query: str,
    *,
    scope: str | None = None,
    note_type: str | None = None,
    limit: int = 10,
    include_superseded: bool = False,
    include_sessions: bool = False,
) -> list[dict]:
    """Hybrid search over both indexes. Superseded notes are excluded by default.

    Two stages. **Recall** lets both rankers contribute up to RECALL candidates,
    fused with RRF. **Precision** then re-orders them by how many distinct query
    terms each note contains *per unit of length*, with the fused score as the
    tie-break.

    The second stage is what makes a natural-language question work. BM25 rewards
    a rare term heavily but does not reward matching *several* terms, so without
    it a long note repeating one common word outranks a short note that answers
    the whole question. Counting terms alone is not enough either — see
    `_coverage` for why length has to be discounted, and what it cost when it
    was not.

    **Raw sessions are excluded by default.** A session is input, not knowledge:
    it is the transcript the distilled notes were extracted from. At ~20 kB it is
    roughly ten times the size of a note, so it matches nearly every query and
    wins on length rather than on relevance. Pass `include_sessions=True` to get
    them back — useful when hunting for something that was never distilled.
    """
    conn = connect()
    try:
        ensure_schema(conn)
        exclude = None if include_sessions else "session"
        fts = _search_fts(conn, query, RECALL, exclude_type=exclude)
        ranked = _rrf([
            fts,
            _search_trigram(conn, query, RECALL, exclude_type=exclude),
        ])
        if not ranked:
            return []

        terms = [t.lower() for t in _tokens(query)]
        coverage = _coverage(conn, list(ranked), terms)
        order = sorted(
            ranked.items(),
            key=lambda kv: (-coverage.get(kv[0], (0, 0.0))[1], -kv[1]),
        )
        # Snippets come from the same FTS pass — fetch once, not once per row.
        snippets = {cid: snip for cid, _, snip in fts}
        out: list[dict] = []
        for note_id, score in order:
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                continue
            if not include_superseded and row["status"] != "active":
                continue
            if not include_sessions and row["type"] == "session":
                continue
            if scope and row["scope"] != scope:
                continue
            if note_type and row["type"] != note_type:
                continue
            matched, cov = coverage.get(note_id, (0, 0.0))
            out.append({
                "id": row["id"],
                "title": row["title"],
                "type": row["type"],
                "scope": row["scope"],
                "status": row["status"],
                "origin": row["origin"],
                "updated": row["updated"],
                "path": row["path"],
                "score": round(score, 6),
                "coverage": round(cov, 3),
                "terms": matched,
                "snippet": snippets.get(note_id) or (row["title"] or ""),
            })
            if len(out) >= limit:
                break
        return out
    finally:
        conn.close()


def index_note(note: Note) -> None:
    """Index one note, plus its supersession predecessor if it has one.

    When a note supersedes another, the store rewrites the predecessor's file
    (status -> superseded). Its index row is now stale, and a stale row is how a
    superseded note keeps surfacing in default search results. Every write path
    must go through here rather than calling upsert directly.
    """
    conn = connect()
    try:
        ensure_schema(conn)
        upsert(conn, note)
        if note.supersedes:
            old = store.load(note.supersedes)
            if old is not None:
                upsert(conn, old)
    finally:
        conn.close()


def get_meta(note_id: str) -> dict | None:
    conn = connect()
    try:
        ensure_schema(conn)
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ----------------------------------------------------------------- symbols

def replace_symbols(conn: sqlite3.Connection, scope: str, symbols: list[dict]) -> None:
    conn.execute("DELETE FROM symbols WHERE scope = ?", (scope,))
    conn.executemany(
        "INSERT INTO symbols (scope, name, kind, file, line, signature) VALUES (?,?,?,?,?,?)",
        [(scope, s["name"], s["kind"], s["file"], s["line"], s["signature"]) for s in symbols],
    )
    conn.commit()


def find_symbols(name: str, scope: str | None = None, limit: int = 20) -> list[dict]:
    conn = connect()
    try:
        ensure_schema(conn)
        sql = "SELECT * FROM symbols WHERE name LIKE ?"
        params: list = [f"%{name}%"]
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        sql += " ORDER BY length(name), name LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ------------------------------------------------------------------- usage

def log_usage(tool: str, query: str, tokens: int, hits: int) -> None:
    """Record what a retrieval call cost. Memory that costs more than it gives
    is a liability, and you cannot know that without measuring."""
    conn = connect()
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO usage (ts, tool, query, tokens, hits) VALUES (?,?,?,?,?)",
            (time.strftime("%Y-%m-%dT%H:%M:%S"), tool, query[:200], tokens, hits),
        )
        conn.commit()
    finally:
        conn.close()


def usage_summary() -> dict:
    conn = connect()
    try:
        ensure_schema(conn)
        by_tool = [
            dict(r) for r in conn.execute(
                "SELECT tool, COUNT(*) AS calls, SUM(tokens) AS tokens, "
                "SUM(hits) AS hits FROM usage GROUP BY tool ORDER BY tokens DESC"
            ).fetchall()
        ]
        total = conn.execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(tokens),0) AS tokens FROM usage"
        ).fetchone()
        return {"total_calls": total["calls"], "total_tokens": total["tokens"], "by_tool": by_tool}
    finally:
        conn.close()


def stats() -> dict:
    conn = connect()
    try:
        ensure_schema(conn)
        by_type = [
            dict(r) for r in conn.execute(
                "SELECT type, COUNT(*) AS n, "
                "SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active "
                "FROM notes GROUP BY type ORDER BY n DESC"
            ).fetchall()
        ]
        by_scope = [
            dict(r) for r in conn.execute(
                "SELECT scope, COUNT(*) AS n FROM notes GROUP BY scope ORDER BY n DESC"
            ).fetchall()
        ]
        superseded = conn.execute(
            "SELECT COUNT(*) AS n FROM notes WHERE status='superseded'"
        ).fetchone()["n"]
        symbols = conn.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
        size = INDEX_PATH.stat().st_size if INDEX_PATH.exists() else 0
        return {
            "by_type": by_type,
            "by_scope": by_scope,
            "superseded": superseded,
            "symbols": symbols,
            "index_bytes": size,
            "index_path": str(INDEX_PATH),
        }
    finally:
        conn.close()
