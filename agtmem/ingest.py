"""Import WorkBuddy's own context-compaction summaries as session notes.

WorkBuddy compacts its context when a conversation outgrows the window, and the
model produces a structured summary ("Primary Request and Intent: …") wrapped in
<conversation_history_summary>. That summary is generated anyway, it is free, and
it is thrown away. This module harvests it.

Strictly read-only on the input logs. Nothing here writes outside the store.

Caveat that matters: the summary optimises for *continuing that task*, not for
*remembering forever*. It keeps task state and drops durable facts. So these land
as `origin: import`, type `session` — raw material, not curated memory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from . import store

# Session logs are read-only input. Agents that compact their context
# window tend to leave JSONL transcripts in a
# <project>/<session>.jsonl layout; these are the locations checked,
# in order. AGTMEM_SESSIONS_ROOT overrides the search entirely.
_SESSIONS_CANDIDATES = (
    Path.home() / ".claude" / "projects",
    Path.home() / ".workbuddy-ai" / "projects",
)


def _sessions_root() -> Path:
    override = os.environ.get("AGTMEM_SESSIONS_ROOT")
    if override:
        return Path(override).expanduser()
    for candidate in _SESSIONS_CANDIDATES:
        if candidate.is_dir():
            return candidate
    return _SESSIONS_CANDIDATES[0]


PROJECTS_ROOT = _sessions_root()

TAG_RE = re.compile(
    r"<(?P<tag>conversation_history_summary|cb_summary|compact-request)>\s*(?P<body>.*?)"
    r"</(?P=tag)>",
    re.DOTALL,
)
BARE_RE = re.compile(
    r"Please summarize the conversation above(?P<body>.*)", re.DOTALL
)

# WorkBuddy (and Claude) name ad-hoc project directories after the workspace
# plus the moment the session started, e.g.
# ``c-Users-milo-WorkBuddy AI-2026-09-04-11-43-59``. Without this the derived
# scope is the trailing clock reading ("43-59"), which is not a project and
# silently becomes its own bucket in the store.
_TIMESTAMP_TAIL = re.compile(r"(?:-\d{4}-\d{2}-\d{2}(?:-\d{2}){0,3})+$")


def scope_from_dir(dirname: str) -> str:
    """Derive a short scope name from a slugified project directory.

    Transcript directories are slugified absolute paths, e.g.
    ``c-Users-alice-code-myproject`` or ``home-alice-code-myproject``.
    The leading ``users/<name>`` or ``home/<name>`` is dropped so the
    scope is the project rather than the whole path, and only the last
    two segments are kept to stay short. A trailing session timestamp is
    dropped before that, so an ad-hoc workspace yields the workspace name
    rather than a clock reading.
    """
    slug = dirname.lower().replace("\\", "/").strip("/")
    slug = re.sub(r"^[a-z]:", "", slug)  # drop a drive letter
    slug = re.sub(r"[^a-z0-9-]+", "-", slug).strip("-")
    slug = _TIMESTAMP_TAIL.sub("", slug).strip("-")
    parts = [p for p in slug.split("-") if p]
    # Only strip when the marker is at the front, so a project that
    # happens to be called "home" is left alone.
    for marker in ("users", "home"):
        if marker in parts[:2]:
            parts = parts[parts.index(marker) + 2:]
            break
    return "-".join(parts[-2:]) or "sessions"


def _text_of(record: dict) -> str:
    """Flatten a record's content into plain text."""
    content = record.get("content")
    chunks: list[str] = []
    if isinstance(content, str):
        chunks.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                for key in ("text", "input_text", "output_text", "content"):
                    value = item.get(key)
                    if isinstance(value, str):
                        chunks.append(value)
                    elif isinstance(value, list):
                        for sub in value:
                            if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                                chunks.append(sub["text"])
    if not chunks and isinstance(record.get("message"), str):
        chunks.append(record["message"])
    return "\n".join(chunks)


def extract_summary(record: dict) -> str | None:
    """Pull a compaction summary out of a record, whatever tag shape it uses."""
    blob = _text_of(record)
    if not blob:
        return None
    match = TAG_RE.search(blob)
    if match:
        body = match.group("body").strip()
        return body or None
    bare = BARE_RE.search(blob)
    if bare:
        body = bare.group("body").strip()
        return body or None
    return None


def _is_compaction(record: dict) -> bool:
    pd = record.get("providerData")
    if isinstance(pd, dict):
        if pd.get("isSummary") or pd.get("isCompacted") or pd.get("compactType"):
            return True
    return False


def _ts_to_date(record: dict) -> str:
    raw = record.get("timestamp")
    if isinstance(raw, (int, float)) and raw > 0:
        seconds = raw / 1000 if raw > 10_000_000_000 else raw
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    return date.today().isoformat()


def iter_compactions(projects_root: Path = PROJECTS_ROOT, since: str | None = None):
    """Yield (session, line_no, scope, when, summary, self_reported).

    `self_reported` is True when the record's own providerData marks it as a
    compaction summary, as opposed to being recognised only by its tag. Both are
    imported; the flag is kept so provenance can record which was which.
    """
    if not projects_root.exists():
        return
    for path in sorted(projects_root.glob("*/*.jsonl")):
        scope = scope_from_dir(path.parent.name)
        session = path.stem
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for lineno, line in enumerate(handle, 1):
                line = line.strip()
                if not line or "conversation_history_summary" not in line and "cb_summary" not in line:
                    # cheap prefilter; the tag must be present for it to be interesting
                    if "compact-request" not in line and "summarize the conversation" not in line:
                        continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue
                summary = extract_summary(record)
                if not summary:
                    continue
                when = _ts_to_date(record)
                if since and when < since:
                    continue
                yield session, lineno, scope, when, summary, _is_compaction(record)


def note_id_for(session: str, lineno: int, summary: str) -> str:
    digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:8]
    return f"session-{session[:8]}-{lineno}-{digest}"


def ingest(
    *,
    projects_root: Path = PROJECTS_ROOT,
    since: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict:
    """Import compaction summaries that are not stored yet.

    Never creates a duplicate: the id is a hash of the summary itself, so a
    summary already in the store is skipped.

    `limit` caps how many *new* notes this call creates, not how many records it
    scans. That makes a large backlog resumable — run it again and it continues
    where it stopped, instead of re-scanning the same window forever. The price
    is that a second run over a partially imported backlog reports both created
    and skipped counts, which is expected, not evidence of duplication.

    Returns {"created", "skipped", "hit_limit"}.
    """
    created, skipped = 0, 0
    hit_limit = False
    for session, lineno, scope, when, summary, self_reported in iter_compactions(
        projects_root, since
    ):
        note_id = note_id_for(session, lineno, summary)
        if store.find_by_id(note_id) is not None:
            skipped += 1
            continue
        provenance = (
            "self-reported by providerData" if self_reported
            else "matched by tag"
        )
        if dry_run:
            created += 1
            print(f"  [dry-run] {note_id}  scope={scope}  {when}  ({len(summary)} chars)")
        else:
            title = summary.splitlines()[0].strip("# *").strip() or "Session summary"
            title = f"Session {when} ({scope}): {title[:70]}"
            store.write_note(
                title,
                summary,
                note_type="session",
                scope=scope,
                tags=["session", "compaction"],
                origin="import",
                detail=(
                    f"{session} line {lineno}, context compaction ({provenance})"
                ),
                note_id=note_id,
            )
            created += 1
        if limit and created >= limit:
            hit_limit = True
            break
    return {"created": created, "skipped": skipped, "hit_limit": hit_limit}
