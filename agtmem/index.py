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

# Function words carry no retrieval signal, and because the FTS query is an OR,
# every one of them widens the candidate pool with noise. Measured effect on the
# eval set: R@5 went from 0.60 to 0.87 when these were filtered.
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


def _search_fts(conn, query: str, limit: int, scope=None, note_type=None):
    match = _fts_query(query)
    if not match:
        return []
    sql = """
        SELECT id, bm25(notes_fts) AS rank,
               snippet(notes_fts, -1, '[', ']', ' … ', 14) AS snip
        FROM notes_fts WHERE notes_fts MATCH ?
        ORDER BY rank LIMIT ?
    """
    rows = conn.execute(sql, (match, limit)).fetchall()
    return [(r["id"], r["rank"], r["snip"]) for r in rows]


def _search_trigram(conn, query: str, limit: int):
    match = _trigram_query(query)
    if not match:
        return []
    try:
        rows = conn.execute(
            "SELECT id, bm25(notes_tri) AS rank FROM notes_tri "
            "WHERE notes_tri MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
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


def _coverage(conn, ids: list[str], terms: list[str]) -> dict[str, int]:
    """How many distinct query terms each candidate actually contains.

    BM25 rewards a rare term heavily but does not directly reward matching
    *several* terms, so a long note that repeats one common word can outrank a
    short note that answers the whole question. Counting distinct term coverage
    and sorting on it first fixes that, and it is cheap: the candidates are
    already fetched, and a substring test needs no index.

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
    out: dict[str, int] = {}
    for row in rows:
        blob = f"{row['title']} {row['body']} {row['tags']}".lower()
        words = set(re.findall(r"\w+", blob))
        hits = 0
        for term in terms:
            stem = term[: max(4, len(term) - 2)]
            if term in blob or any(w.startswith(stem) for w in words):
                hits += 1
        out[row["id"]] = hits
    return out


def search(
    query: str,
    *,
    scope: str | None = None,
    note_type: str | None = None,
    limit: int = 10,
    include_superseded: bool = False,
) -> list[dict]:
    """Hybrid search over both indexes. Superseded notes are excluded by default.

    Two stages. **Recall** lets both rankers contribute up to RECALL candidates,
    fused with RRF. **Precision** then re-orders them by how many distinct query
    terms each note actually contains, with the fused score as the tie-break.

    The second stage is what makes a natural-language question work. BM25 rewards
    a rare term heavily but does not reward matching *several* terms, so without
    it a long note repeating one common word outranks a short note that answers
    the whole question.
    """
    conn = connect()
    try:
        ensure_schema(conn)
        fts = _search_fts(conn, query, RECALL)
        ranked = _rrf([fts, _search_trigram(conn, query, RECALL)])
        if not ranked:
            return []

        terms = [t.lower() for t in _tokens(query)]
        coverage = _coverage(conn, list(ranked), terms)
        order = sorted(
            ranked.items(),
            key=lambda kv: (-coverage.get(kv[0], 0), -kv[1]),
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
            if scope and row["scope"] != scope:
                continue
            if note_type and row["type"] != note_type:
                continue
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
                "coverage": coverage.get(note_id, 0),
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
