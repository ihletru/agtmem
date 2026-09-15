"""The store: plain Markdown files with a restricted frontmatter block.

This module owns the *contract* layer. It must stay dependency-free and boring,
because everything else (index, CLI, MCP server) is replaceable but these files
are not.

Frontmatter is a deliberately restricted YAML subset — `key: value` lines only,
no nesting, no block scalars — so it can be parsed with the standard library and
still be comfortable to edit by hand. Lists (tags) are space-separated.
"""
from __future__ import annotations

import contextlib
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------- locations

def _resolve_home() -> Path:
    """AGTMEM_HOME wins; MEM_HOME is accepted as a legacy alias."""
    for var in ("AGTMEM_HOME", "MEM_HOME"):
        value = os.environ.get(var)
        if value:
            return Path(value).expanduser()
    return Path.home() / ".agtmem"


MEM_HOME = _resolve_home()
INDEX_PATH = MEM_HOME / ".index.sqlite"
LOCK_PATH = MEM_HOME / ".lock"
EVAL_PATH = MEM_HOME / "eval.txt"

TYPES = ("fact", "decision", "bug", "candidate", "anatomy", "project", "session", "log")
ORIGINS = ("user", "agent", "tool", "import", "shared")
STATUSES = ("active", "superseded")

# where each type lives, relative to MEM_HOME
TYPE_DIRS = {
    "fact": "facts",
    "decision": "decisions",
    "bug": "bugs",
    "candidate": "candidates",
    "anatomy": "anatomy",
    "session": "sessions",
    "log": "log",
    "project": "projects",
}

FM_KEYS = (
    "id", "title", "type", "scope", "tags", "status",
    "origin", "detail", "supersedes", "superseded_by",
    "captured", "updated",
)

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def today() -> str:
    return date.today().isoformat()


_TRANSLIT = str.maketrans({
    "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n",
    "ó": "o", "ś": "s", "ź": "z", "ż": "z",
    "Ą": "a", "Ć": "c", "Ę": "e", "Ł": "l", "Ń": "n",
    "Ó": "o", "Ś": "s", "Ź": "z", "Ż": "z",
})


def slug(text: str, limit: int = 60) -> str:
    """Filesystem-safe, typeable id fragment.

    Polish diacritics are folded to ASCII rather than dropped, so
    "wskaźniki" becomes "wskazniki" and not "wska-niki" — ids get typed by
    hand, and a hyphen in the middle of a word is a trap.
    """
    text = text.strip().translate(_TRANSLIT).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:limit] or "untitled"


# ------------------------------------------------------------- frontmatter

def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (metadata, body). A file without frontmatter yields ({}, text)."""
    match = _FM_RE.match(text)
    if not match:
        return {}, text
    meta: dict = {}
    for line in match.group(1).splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = _unquote(value)
    return meta, text[match.end():]


def render_frontmatter(meta: dict) -> str:
    """Canonical key order first, then any extras alphabetically."""
    ordered = [k for k in FM_KEYS if k in meta]
    ordered += sorted(k for k in meta if k not in FM_KEYS)
    lines = ["---"]
    for key in ordered:
        value = meta.get(key)
        if value is None:
            value = ""
        value = str(value)
        # quote when the value could confuse the parser or a human
        if value != value.strip() or ":" in value or value.startswith(("-", "#", "*")):
            value = '"' + value.replace('"', "'") + '"'
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------- notes

@dataclass
class Note:
    id: str
    title: str
    type: str = "fact"
    scope: str = "global"
    tags: list[str] = field(default_factory=list)
    status: str = "active"
    origin: str = "agent"
    detail: str = ""
    supersedes: str = ""
    superseded_by: str = ""
    captured: str = ""
    updated: str = ""
    body: str = ""
    path: Path | None = None

    def to_meta(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "scope": self.scope,
            "tags": " ".join(self.tags),
            "status": self.status,
            "origin": self.origin,
            "detail": self.detail,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "captured": self.captured or today(),
            "updated": self.updated or today(),
        }

    def render(self) -> str:
        body = self.body.rstrip("\n")
        return render_frontmatter(self.to_meta()) + "\n" + body + "\n"

    @classmethod
    def from_file(cls, path: Path) -> "Note":
        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        tags = meta.get("tags", "")
        return cls(
            id=meta.get("id") or path.stem,
            title=meta.get("title") or path.stem,
            type=meta.get("type") or "fact",
            scope=meta.get("scope") or "global",
            tags=[t for t in re.split(r"[,\s]+", tags) if t],
            status=meta.get("status") or "active",
            origin=meta.get("origin") or "agent",
            detail=meta.get("detail", ""),
            supersedes=meta.get("supersedes", ""),
            superseded_by=meta.get("superseded_by", ""),
            captured=meta.get("captured", ""),
            updated=meta.get("updated", ""),
            body=body.strip("\n"),
            path=path,
        )


# ------------------------------------------------------- locking + writing

class LockTimeout(RuntimeError):
    pass


# Re-entrancy bookkeeping. `append_to` takes the lock and then calls `save`,
# which takes it again. Without this, the second acquisition would block on the
# lock it is itself holding and die after `timeout` seconds.
_LOCK_DEPTH = 0
_LOCK_GUARD = threading.Lock()

# An OS advisory lock rather than a "does the file exist?" marker.
#
# The existence-marker approach has a failure mode that is not worth living
# with: if a process dies between creating the marker and removing it, the store
# stays read-only until someone notices. Reclaiming it needs a staleness
# heuristic, and any heuristic has a window where it is either too eager (two
# writers at once) or too slow (writes fail for no visible reason).
#
# The kernel releases an advisory lock when the holding process exits, for any
# reason including SIGKILL. So a crashed writer cannot wedge the store, and no
# heuristic is needed. The lock file itself is never deleted — deleting a file
# another process may be blocked on is how you end up with two processes
# holding locks on two different inodes.
if os.name == "nt":  # pragma: no cover - platform branch
    import msvcrt

    def _lock_fd(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock_fd(fd: int) -> None:
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_fd(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock_fd(fd: int) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def store_lock(timeout: float = 20.0):
    """A bounded, re-entrant, cross-process lock around read-modify-write.

    Atomic writes prevent torn files; they do NOT prevent lost updates when two
    clients read the same note and both write back. This lock is that guard.
    Re-entrant because nested callers are the same writer, not a competitor.
    """
    global _LOCK_DEPTH
    with _LOCK_GUARD:
        if _LOCK_DEPTH > 0:
            _LOCK_DEPTH += 1
            nested = True
        else:
            nested = False

    if nested:
        try:
            yield
        finally:
            with _LOCK_GUARD:
                _LOCK_DEPTH -= 1
        return

    MEM_HOME.mkdir(parents=True, exist_ok=True)
    # Opened, never written to. Both msvcrt's LockFile and fcntl's flock happily
    # lock byte 0 of an empty file, and writing here would be a bug: if another
    # process already holds the byte-0 lock, Windows refuses the write with
    # EACCES, so the "prepare the file" step would itself be the crash.
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR)
    start = time.time()
    try:
        while not _lock_fd(fd):
            if time.time() - start > timeout:
                raise LockTimeout(
                    f"could not acquire {LOCK_PATH} within {timeout}s. "
                    f"Another agtmem process is writing. "
                    f"Run `agtmem doctor` to see "
                    f"whether the lock is currently held."
                )
            time.sleep(0.05)
        with _LOCK_GUARD:
            _LOCK_DEPTH = 1
        try:
            yield
        finally:
            with _LOCK_GUARD:
                _LOCK_DEPTH = 0
            _unlock_fd(fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then os.replace()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ------------------------------------------------------------------ access

def note_path(note_type: str, note_id: str, scope: str = "global") -> Path:
    base = TYPE_DIRS.get(note_type, "facts")
    if note_type == "project":
        return MEM_HOME / base / slug(scope) / f"{note_id}.md"
    return MEM_HOME / base / f"{note_id}.md"


def iter_note_files():
    """Every note file in the store, excluding the index and dotfiles.

    `index.md` at the store root is the hand-curated entry point for a human
    reader — it is navigation, not a note, so it is not part of the corpus.
    """
    if not MEM_HOME.exists():
        return
    entry_point = MEM_HOME / "index.md"
    for path in MEM_HOME.rglob("*.md"):
        if path.name.startswith("."):
            continue
        if path == entry_point:
            continue
        if ".index" in path.parts:
            continue
        yield path


def find_by_id(note_id: str) -> Path | None:
    for path in iter_note_files():
        if path.stem == note_id:
            return path
    return None


def load(note_id: str) -> Note | None:
    path = find_by_id(note_id)
    return Note.from_file(path) if path else None


def load_all() -> list[Note]:
    notes = []
    for path in iter_note_files():
        try:
            notes.append(Note.from_file(path))
        except Exception:
            continue
    return notes


def save(note: Note, *, new: bool = False) -> Path:
    """Persist a note. Under the store lock, so concurrent writers cannot clobber.

    On update the existing file is located by id, so moving a note between
    types/ dirs does not leave a stale duplicate behind.
    """
    with store_lock():
        existing = find_by_id(note.id)
        target = note_path(note.type, note.id, note.scope)
        note.updated = today()
        if not note.captured:
            note.captured = today()
        if new and existing is not None:
            raise FileExistsError(f"note '{note.id}' already exists at {existing}")
        if existing is not None and existing != target:
            existing.unlink()
        atomic_write(target, note.render())
        note.path = target
        return target


def write_note(
    title: str,
    body: str,
    *,
    note_type: str = "fact",
    scope: str = "global",
    tags: list[str] | None = None,
    origin: str = "agent",
    detail: str = "",
    note_id: str | None = None,
    supersedes: str = "",
    update: bool = False,
) -> Note:
    """Create or update a note. Returns the stored Note."""
    if note_type not in TYPES:
        raise ValueError(f"unknown type '{note_type}'; expected one of {', '.join(TYPES)}")
    if origin not in ORIGINS:
        raise ValueError(f"unknown origin '{origin}'; expected one of {', '.join(ORIGINS)}")
    note_id = note_id or slug(title)
    existing = load(note_id)
    if existing and not update:
        raise FileExistsError(
            f"note '{note_id}' already exists (pass update=True / --update to overwrite)"
        )
    note = existing or Note(id=note_id, title=title)
    note.title = title
    note.type = note_type
    note.scope = scope
    note.tags = tags or []
    note.body = body
    note.origin = origin
    note.detail = detail
    if supersedes:
        note.supersedes = supersedes
    note.status = "active"
    save(note)
    if supersedes:
        supersede_old(supersedes, note.id)
    return note


def supersede_old(old_id: str, new_id: str) -> None:
    """Mark the predecessor superseded and stamp the forward pointer."""
    old = load(old_id)
    if old is None:
        return
    old.status = "superseded"
    old.superseded_by = new_id
    save(old)


def append_to(note_id: str, text: str) -> Note:
    """Append to a note's body. Under the lock — this is a classic lost-update site."""
    with store_lock():
        note = load(note_id)
        if note is None:
            raise FileNotFoundError(f"note '{note_id}' not found")
        note.body = (note.body.rstrip("\n") + "\n\n" + text.strip("\n")).strip("\n")
        note.updated = today()
        save(note)
        return note


def candidates() -> list[Note]:
    return [n for n in load_all() if n.type == "candidate" and n.status == "active"]


def promote(note_id: str, *, note_type: str = "fact") -> Note:
    """Turn a candidate into a confirmed note of the given type."""
    note = load(note_id)
    if note is None:
        raise FileNotFoundError(f"note '{note_id}' not found")
    if note.type != "candidate":
        raise ValueError(f"note '{note_id}' is type '{note.type}', not a candidate")
    note.type = note_type
    note.detail = (note.detail + f"; promoted from candidate {today()}").strip("; ")
    save(note)
    return note


def ensure_layout() -> None:
    MEM_HOME.mkdir(parents=True, exist_ok=True)
    for sub in set(TYPE_DIRS.values()):
        (MEM_HOME / sub).mkdir(exist_ok=True)
    index_md = MEM_HOME / "index.md"
    if not index_md.exists():
        atomic_write(
            index_md,
            "# ~/.agtmem — entry point\n\n"
            "This file is curated by hand. Keep it short.\n\n"
            "## What lives where\n\n"
            "- `facts/` — durable truths\n"
            "- `decisions/` — why something was done\n"
            "- `bugs/` — symptom, cause, fix\n"
            "- `candidates/` — unconfirmed, awaiting review\n"
            "- `projects/` — project overviews\n"
            "- `anatomy/` — codebase maps\n"
            "- `sessions/` — imported context-compaction summaries\n"
            "- `log/` — append-only journal\n",
        )
