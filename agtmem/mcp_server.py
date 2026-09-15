"""MCP server over stdio. JSON-RPC 2.0, one message per line, zero dependencies.

Why hand-rolled: the entire point of `agtmem` is that it survives the death of any
single vendor. A ~60-line JSON-RPC loop over the standard library is a smaller
long-term liability than a dependency on the MCP SDK, which would itself become
something to migrate away from later.

Transport rules that actually matter:
  * stdout carries JSON-RPC and nothing else. One stray print() corrupts the
    stream and the client drops the connection. Every human-readable byte goes
    to stderr.
  * MCP stdio framing is newline-delimited JSON — not LSP's Content-Length.
  * A message without an `id` is a notification and MUST NOT be answered.
  * Tool *execution* failures are reported inside the result with isError=true.
    Only protocol-level problems become JSON-RPC errors.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

from . import anatomy, index, store

PROTOCOL_FALLBACK = "2025-06-18"

INSTRUCTIONS = (
    "Long-term memory stored as Markdown files under ~/.agtmem, owned by the user. "
    "Call agtmem_search before starting work on a project to recover prior "
    "decisions and known bugs. Record durable conclusions with agtmem_write and "
    "failures with agtmem_bug. Prefer agtmem_find over reading whole source files."
)

# --------------------------------------------------------------- tool schemas

TOOLS = [
    {
        "name": "agtmem_search",
        "description": (
            "Search long-term memory (hybrid FTS5 + trigram, RRF-merged). "
            "Returns short snippets with ids. Superseded notes are excluded. "
            "Use this first when resuming work on a project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "free text, any language"},
                "scope": {"type": "string", "description": "project scope, e.g. 'my-project'"},
                "type": {
                    "type": "string",
                    "enum": list(store.TYPES),
                    "description": "restrict to one note type",
                },
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "agtmem_read",
        "description": "Read one note in full by id. Bounded by max_tokens.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "max_tokens": {"type": "integer", "default": 1200},
            },
            "required": ["id"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "agtmem_write",
        "description": (
            "Create or update a note. Use type='decision' for choices and their "
            "rationale, 'fact' for durable truths, 'project' for project "
            "overviews, 'candidate' for unconfirmed guesses. Pass supersedes=<id> "
            "to replace an outdated note — it is kept but marked superseded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string", "description": "Markdown; ## headings become chunks"},
                "type": {"type": "string", "enum": list(store.TYPES), "default": "fact"},
                "scope": {"type": "string", "default": "global"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "origin": {"type": "string", "enum": list(store.ORIGINS), "default": "agent"},
                "detail": {"type": "string", "description": "one-line why/provenance"},
                "id": {"type": "string", "description": "explicit id; defaults to a slug of title"},
                "supersedes": {"type": "string", "description": "id of the note this replaces"},
                "update": {"type": "boolean", "default": False},
            },
            "required": ["title", "body"],
        },
    },
    {
        "name": "agtmem_append",
        "description": "Append a dated section to an existing note. Safe under concurrency.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["id", "text"],
        },
    },
    {
        "name": "agtmem_index",
        "description": (
            "Rebuild the search index from the Markdown files. Always safe: the "
            "index is a cache and deleting it loses nothing. If `path` is given, "
            "also rescan that codebase into the symbol index and refresh its "
            "code map note."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "codebase root to scan"},
                "scope": {"type": "string", "description": "scope name for the scan"},
            },
        },
    },
    {
        "name": "agtmem_bug",
        "description": (
            "Record a bug with the three parts that make it reusable: symptom, "
            "root cause, fix. Call this after solving anything non-obvious."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symptom": {"type": "string", "description": "what was observed"},
                "cause": {"type": "string", "description": "why it happened"},
                "fix": {"type": "string", "description": "what actually resolved it"},
                "note": {"type": "string", "description": "extra context, gotchas"},
                "title": {"type": "string"},
                "scope": {"type": "string", "default": "global"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "id": {"type": "string"},
            },
            "required": ["symptom", "cause", "fix"],
        },
    },
    {
        "name": "agtmem_candidates",
        "description": (
            "List unconfirmed notes (type='candidate') — inferences the agent made "
            "that a human has not validated. Pass promote=<id> to accept one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "promote": {"type": "string", "description": "candidate id to accept"},
                "to": {"type": "string", "enum": list(store.TYPES), "default": "fact"},
            },
        },
    },
    {
        "name": "agtmem_find",
        "description": (
            "Look up a symbol (function, class, method) in the codebase map and "
            "get file:line back. Cheaper and more precise than grepping or "
            "reading whole files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "scope": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
                "signatures": {"type": "boolean", "default": False},
                "max_tokens": {"type": "integer", "default": 1000},
            },
            "required": ["symbol"],
        },
        "annotations": {"readOnlyHint": True},
    },
]

HANDLERS: dict[str, Callable[[dict], str]] = {}


def tool(name: str):
    def wrap(fn):
        HANDLERS[name] = fn
        return fn
    return wrap


# ------------------------------------------------------------------- helpers

def _require(args: dict, key: str):
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"missing required parameter '{key}'")
    return value


def _tags(args: dict) -> list[str]:
    raw = args.get("tags")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [t for t in raw.replace(",", " ").split() if t]
    return [str(t) for t in raw]


def _clip(text: str, max_tokens: int) -> str:
    if max_tokens <= 0 or index.estimate_tokens(text) <= max_tokens:
        return text
    return text[: max_tokens * 4].rstrip() + "\n… (truncated)"


def _ok(msg_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


# -------------------------------------------------------------------- tools

@tool("agtmem_search")
def _t_search(args: dict) -> str:
    query = _require(args, "query")
    rows = index.search(
        query,
        scope=args.get("scope"),
        note_type=args.get("type"),
        limit=int(args.get("limit") or 10),
    )
    if not rows:
        return f"No results for {query!r}."
    lines = [f"{len(rows)} result(s) for {query!r}:"]
    for row in rows:
        lines.append(
            f"- [{row['type']}] {row['id']}  ({row['scope']}, {row['updated']})"
        )
        if row["snippet"]:
            lines.append(f"    {row['snippet']}")
    text = "\n".join(lines)
    index.log_usage("mcp-search", query, index.estimate_tokens(text), len(rows))
    return text


@tool("agtmem_read")
def _t_read(args: dict) -> str:
    note = store.load(_require(args, "id"))
    if note is None:
        raise FileNotFoundError(f"no such note: '{args['id']}'")
    head = (
        f"# {note.title}\n"
        f"id={note.id}  type={note.type}  scope={note.scope}  status={note.status}\n"
        f"origin={note.origin}  captured={note.captured}  updated={note.updated}\n"
    )
    if note.supersedes:
        head += f"supersedes={note.supersedes}\n"
    if note.superseded_by:
        head += f"superseded_by={note.superseded_by}\n"
    text = _clip(head + "\n" + note.body, int(args.get("max_tokens") or 1200))
    index.log_usage("mcp-read", note.id, index.estimate_tokens(text), 1)
    return text


@tool("agtmem_write")
def _t_write(args: dict) -> str:
    note = store.write_note(
        _require(args, "title"),
        _require(args, "body"),
        note_type=args.get("type") or "fact",
        scope=args.get("scope") or "global",
        tags=_tags(args),
        origin=args.get("origin") or "agent",
        detail=args.get("detail") or "",
        note_id=args.get("id"),
        supersedes=args.get("supersedes") or "",
        update=bool(args.get("update")),
    )
    index.index_note(note)
    out = f"written: {note.id}  ({note.type}, scope={note.scope})\n{note.path}"
    if note.supersedes:
        out += f"\nsupersedes: {note.supersedes} (marked superseded)"
    return out


@tool("agtmem_append")
def _t_append(args: dict) -> str:
    note = store.append_to(_require(args, "id"), _require(args, "text"))
    index.index_note(note)
    return f"appended to {note.id}; note is now {len(note.body)} chars."


@tool("agtmem_index")
def _t_index(args: dict) -> str:
    parts: list[str] = []
    path = args.get("path")
    if path:
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"no such directory: {root}")
        scope = args.get("scope") or store.slug(root.name)
        result = anatomy.scan(root, scope)
        conn = index.connect()
        try:
            index.ensure_schema(conn)
            index.replace_symbols(conn, scope, result["symbols"])
        finally:
            conn.close()
        note = store.write_note(
            f"Code map — {scope}",
            anatomy.render_map(result),
            note_type="anatomy",
            scope=scope,
            tags=["anatomy", scope],
            origin="tool",
            detail=f"agtmem_index scan {root}",
            note_id=f"anatomy-{scope}",
            update=True,
        )
        parts.append(
            f"scanned {root}: {result['files']} files, "
            f"{len(result['symbols'])} symbols -> note {note.id}"
        )
    rebuilt = index.reindex()
    parts.append(f"index rebuilt: {rebuilt['notes']} notes")
    return "\n".join(parts)


@tool("agtmem_bug")
def _t_bug(args: dict) -> str:
    symptom = _require(args, "symptom")
    cause = _require(args, "cause")
    fix = _require(args, "fix")
    body = (
        f"## Symptom\n\n{symptom.strip()}\n\n"
        f"## Cause\n\n{cause.strip()}\n\n"
        f"## Fix\n\n{fix.strip()}\n"
    )
    extra = (args.get("note") or "").strip()
    if extra:
        body += f"\n## Notes\n\n{extra}\n"
    title = args.get("title") or f"Bug: {symptom.strip().splitlines()[0][:70]}"
    note = store.write_note(
        title,
        body,
        note_type="bug",
        scope=args.get("scope") or "global",
        tags=_tags(args),
        origin=args.get("origin") or "agent",
        detail=args.get("detail") or "",
        note_id=args.get("id"),
        update=bool(args.get("update")),
    )
    index.index_note(note)
    return f"bug recorded: {note.id}\n{note.path}"


@tool("agtmem_candidates")
def _t_candidates(args: dict) -> str:
    promote_id = args.get("promote")
    if promote_id:
        note = store.promote(promote_id, note_type=args.get("to") or "fact")
        index.index_note(note)
        return f"promoted {note.id} -> {note.type}"
    rows = store.candidates()
    if not rows:
        return "The candidate queue is empty."
    lines = [f"{len(rows)} unconfirmed note(s):"]
    for note in rows:
        lines.append(f"- {note.id}  ({note.scope}, {note.captured})  {note.title}")
        if note.detail:
            lines.append(f"    {note.detail}")
    return "\n".join(lines)


@tool("agtmem_find")
def _t_find(args: dict) -> str:
    symbol = _require(args, "symbol")
    rows = index.find_symbols(symbol, scope=args.get("scope"), limit=int(args.get("limit") or 20))
    if not rows:
        return (
            f"No symbols matching {symbol!r}. "
            f"Run agtmem_index with a path=... first."
        )
    lines = []
    for row in rows:
        lines.append(f"{row['file']}:{row['line']}  {row['name']}  ({row['kind']})")
        if args.get("signatures"):
            lines.append(f"    {row['signature']}")
    text = _clip("\n".join(lines), int(args.get("max_tokens") or 1000))
    index.log_usage("mcp-find", symbol, index.estimate_tokens(text), len(rows))
    return text


# ------------------------------------------------------------------ protocol

def _initialize_result(params: dict) -> dict:
    requested = params.get("protocolVersion")
    return {
        "protocolVersion": requested or PROTOCOL_FALLBACK,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "agtmem", "version": _version()},
        "instructions": INSTRUCTIONS,
    }


def _version() -> str:
    from . import __version__
    return __version__


def _call_tool(name, arguments) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        return {
            "content": [{"type": "text", "text": f"unknown tool: {name}"}],
            "isError": True,
        }
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        return {"content": [{"type": "text", "text": handler(arguments)}], "isError": False}
    except Exception as exc:  # tool failures are data, not protocol errors
        return {
            "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
            "isError": True,
        }


def handle(message: dict) -> dict | None:
    """Map one JSON-RPC message to one response, or None for notifications."""
    if not isinstance(message, dict):
        return _err(None, -32600, "invalid request")
    msg_id = message.get("id")
    if msg_id is None:
        return None  # notification — never answered
    method = message.get("method")
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        return _ok(msg_id, _initialize_result(params))
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        return _ok(msg_id, _call_tool(params.get("name"), params.get("arguments")))
    if method == "resources/list":
        return _ok(msg_id, {"resources": []})
    if method == "prompts/list":
        return _ok(msg_id, {"prompts": []})
    return _err(msg_id, -32601, f"method not found: {method}")


def _emit(stream, payload: dict) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
    stream.flush()


def serve() -> None:
    """Blocking read-eval-print loop over stdin/stdout."""
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    for raw in stdin:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit(stdout, _err(None, -32700, f"parse error: {exc}"))
            continue
        response = handle(message)
        if response is not None:
            _emit(stdout, response)


if __name__ == "__main__":
    serve()
