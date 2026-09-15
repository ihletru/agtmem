"""Command line interface."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import anatomy, eval as eval_mod, index, ingest, store
from .store import MEM_HOME, Note


def _utf8() -> None:
    """Windows consoles default to a legacy codepage; UTF-8 output would break."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _body_from(args) -> str:
    if getattr(args, "body_file", None):
        return Path(args.body_file).read_text(encoding="utf-8")
    if getattr(args, "body", None):
        return args.body
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _print_notes(rows: list[dict], *, verbose: bool = False) -> None:
    if not rows:
        print("(no results)")
        return
    for row in rows:
        flags = []
        if row.get("status") != "active":
            flags.append(row["status"])
        if row.get("origin") not in (None, "user"):
            flags.append(row["origin"])
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"{row['id']}  ({row['type']}, {row['scope']}){suffix}")
        # The snippet is the matched context, and it is the reason you searched.
        # The title is often auto-derived ("Session 2026-09-12 (proj): Summary:")
        # and says nothing, so it moves to verbose output instead.
        evidence = (row.get("snippet") or row.get("title") or "").strip()
        if evidence:
            print(f"    {evidence}")
        if verbose:
            title = row.get("title") or ""
            if title and title != evidence:
                print(f"    title: {title}")
            if row.get("path"):
                print(f"    {row['path']}")


# --------------------------------------------------------------- commands

def cmd_init(args) -> int:
    store.ensure_layout()
    print(f"Store: {MEM_HOME}")
    result = index.reindex()
    print(f"Indexed notes: {result['notes']}")
    print(f"Index:  {store.INDEX_PATH}")
    return 0


def _tags_from(args) -> list[str]:
    """Accept both `--tags a b` and `--tags "a,b"`."""
    raw = getattr(args, "tags", None)
    if not raw:
        return []
    out: list[str] = []
    for item in raw:
        out.extend(t for t in str(item).replace(",", " ").split() if t)
    return out


def cmd_add(args) -> int:
    body = _body_from(args)
    if not body.strip() and not args.title:
        print("Provide text: --body, --body-file, or stdin.", file=sys.stderr)
        return 2
    try:
        note = store.write_note(
            args.title or body.splitlines()[0][:70],
            body,
            note_type=args.type,
            scope=args.scope,
            tags=_tags_from(args),
            origin=args.origin,
            detail=args.detail or "",
            note_id=args.id,
            supersedes=args.supersedes or "",
            update=args.update,
        )
    except (FileExistsError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    index.index_note(note)
    print(f"{note.id}  ({note.type}, {note.scope})  ->  {note.path}")
    if note.supersedes:
        print(f"supersedes: {note.supersedes} (marked superseded)")
    return 0


def cmd_bug(args) -> int:
    body = (
        f"## Symptom\n\n{args.symptom.strip()}\n\n"
        f"## Cause\n\n{args.cause.strip()}\n\n"
        f"## Fix\n\n{args.fix.strip()}\n"
    )
    if args.note:
        body += f"\n## Notes\n\n{args.note.strip()}\n"
    title = args.title or args.symptom.strip().splitlines()[0][:70]
    try:
        note = store.write_note(
            title, body,
            note_type="bug",
            scope=args.scope,
            tags=(_tags_from(args) or ["bug"]),
            origin=args.origin,
            detail=args.detail or "",
            note_id=args.id,
            update=args.update,
        )
    except (FileExistsError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    index.index_note(note)
    print(f"{note.id}  (bug, {note.scope})  ->  {note.path}")
    return 0


def cmd_search(args) -> int:
    rows = index.search(
        args.query,
        scope=args.scope,
        note_type=args.type,
        limit=args.limit,
        include_superseded=args.all,
    )
    text = json.dumps(rows, ensure_ascii=False) if args.json else " ".join(
        f"{r['id']} {r['title']} {r['snippet']}" for r in rows
    )
    index.log_usage("search", args.query, index.estimate_tokens(text), len(rows))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        _print_notes(rows, verbose=args.verbose)
    return 0


def cmd_show(args) -> int:
    note = store.load(args.id)
    if note is None:
        print(f"No such note: '{args.id}'", file=sys.stderr)
        return 1
    text = note.render()
    index.log_usage("show", args.id, index.estimate_tokens(text), 1)
    if args.json:
        data = note.to_meta()
        data["body"] = note.body
        data["path"] = str(note.path)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(text)
    return 0


def cmd_list(args) -> int:
    notes = [n for n in store.load_all()
             if (not args.type or n.type == args.type)
             and (not args.scope or n.scope == args.scope)
             and (args.all or n.status == "active")]
    notes.sort(key=lambda n: (n.type, n.id))
    _print_notes([
        {"id": n.id, "title": n.title, "type": n.type, "scope": n.scope,
         "status": n.status, "origin": n.origin, "path": str(n.path or "")}
        for n in notes
    ], verbose=args.verbose)
    return 0


def cmd_append(args) -> int:
    text = args.text or (sys.stdin.read() if not sys.stdin.isatty() else "")
    if not text.strip():
        print("Nothing to append.", file=sys.stderr)
        return 2
    try:
        note = store.append_to(args.id, text)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    index.index_note(note)
    print(f"appended to {note.id} (now {len(note.body)} chars)")
    return 0


def cmd_candidates(args) -> int:
    notes = store.candidates()
    if not notes:
        print("(candidate queue is empty)")
        return 0
    _print_notes([
        {"id": n.id, "title": n.title, "type": n.type, "scope": n.scope,
         "status": n.status, "origin": n.origin, "path": str(n.path or "")}
        for n in notes
    ], verbose=True)
    print(f"\n{len(notes)} candidate(s). Promote with: agtmem promote <id> [--to fact]")
    return 0


def cmd_promote(args) -> int:
    try:
        note = store.promote(args.id, note_type=args.to)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    index.index_note(note)
    print(f"{note.id}: candidate -> {note.type}")
    return 0


def cmd_anatomy(args) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"No such directory: {root}", file=sys.stderr)
        return 1
    scope = args.scope or store.slug(root.name)
    print(f"Scanning {root} (scope={scope}) ...")
    result = anatomy.scan(root, scope)
    print(f"  files: {result['files']}, symbols: {len(result['symbols'])}")

    conn = index.connect()
    try:
        index.ensure_schema(conn)
        index.replace_symbols(conn, scope, result["symbols"])
    finally:
        conn.close()

    body = anatomy.render_map(result)
    note = store.write_note(
        f"Mapa kodu — {scope}",
        body,
        note_type="anatomy",
        scope=scope,
        tags=["anatomy", scope],
        origin="tool",
        detail=f"agtmem anatomy scan {root}",
        note_id=f"anatomy-{scope}",
        update=True,
    )
    index.index_note(note)
    print(f"  symbols indexed: {len(result['symbols'])}")
    print(f"  note: {note.id}  ->  {note.path}")
    return 0


def cmd_find(args) -> int:
    rows = index.find_symbols(args.symbol, scope=args.scope, limit=args.limit)
    if not rows:
        print(f"(no symbols matching {args.symbol!r})")
        return 0
    lines = []
    for row in rows:
        lines.append(f"{row['file']}:{row['line']}  {row['name']}  ({row['kind']})")
        if args.signatures:
            lines.append(f"    {row['signature']}")
    text = "\n".join(lines)
    # keep the output bounded — the whole point is not to dump a file
    if index.estimate_tokens(text) > args.max_tokens:
        keep, total = [], 0
        for line in lines:
            total += index.estimate_tokens(line) + 1
            if total > args.max_tokens:
                break
            keep.append(line)
        text = "\n".join(keep) + f"\n… truncated (limit {args.max_tokens} tokens)"
    index.log_usage("find", args.symbol, index.estimate_tokens(text), len(rows))
    print(text)
    return 0


def cmd_map(args) -> int:
    root = Path(args.path).resolve()
    scope = args.scope or store.slug(root.name)
    result = anatomy.scan(root, scope)
    if args.focus:
        text = anatomy.render_focus(result, args.focus, limit=args.limit)
    else:
        text = anatomy.render_map(result, max_files=args.limit)
    if index.estimate_tokens(text) > args.max_tokens:
        text = text[: args.max_tokens * 4] + "\n… truncated"
    index.log_usage("map", args.focus or scope, index.estimate_tokens(text), 1)
    print(text)
    return 0


def cmd_ingest(args) -> int:
    result = ingest.ingest(
        since=args.since,
        dry_run=args.dry_run,
        limit=args.limit,
    )
    verb = "would import" if args.dry_run else "new notes"
    print(
        f"{verb}: {result['created']}, "
        f"already stored (skipped): {result['skipped']}"
    )
    if result.get("hit_limit"):
        print(
            "Stopped at the limit — run again to import the rest "
            "(order is stable, nothing is duplicated)."
        )
    if not args.dry_run and result["created"]:
        index.reindex()
        print("Index refreshed.")
    return 0


def cmd_reindex(args) -> int:
    result = index.reindex(verbose=args.verbose)
    print(f"Indexed notes: {result['notes']}")
    return 0


def cmd_doctor(args) -> int:
    problems: list[str] = []
    store.ensure_layout()

    files = list(store.iter_note_files())
    notes = []
    for path in files:
        try:
            notes.append(Note.from_file(path))
        except Exception as exc:
            problems.append(f"cannot parse {path}: {exc}")

    seen: dict[str, Path] = {}
    for note in notes:
        if note.id in seen:
            problems.append(f"zduplikowane id '{note.id}': {seen[note.id]} i {note.path}")
        seen[note.id] = note.path or Path("?")

    for note in notes:
        if note.supersedes and store.find_by_id(note.supersedes) is None:
            problems.append(f"'{note.id}' supersedes missing '{note.supersedes}'")
        if note.superseded_by and store.find_by_id(note.superseded_by) is None:
            problems.append(f"'{note.id}' points at missing '{note.superseded_by}'")

    index_count = None
    if store.INDEX_PATH.exists():
        conn = index.connect()
        try:
            index.ensure_schema(conn)
            index_count = conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"]
        finally:
            conn.close()
        if index_count != len(notes):
            problems.append(
                f"index holds {index_count} notes, disk has {len(notes)} "
                f"— run `agtmem reindex`"
            )
    else:
        problems.append("no index — run `agtmem reindex`")

    # The lock file persists by design; what matters is whether anyone holds it.
    if store.LOCK_PATH.exists():
        try:
            with store.store_lock(timeout=0.5):
                print("Lock:         free")
        except store.LockTimeout:
            print("Lock:         HELD — another process is writing")
            problems.append("someone holds the lock; wait, or close that process")

    print(f"Store:        {MEM_HOME}")
    print(f"Notes on disk: {len(notes)}")
    print(f"In index:     {index_count if index_count is not None else '(none)'}")
    print(f"Candidates:   {len(store.candidates())}")
    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nOK — no problems found.")
    return 0


def cmd_stats(args) -> int:
    data = index.stats()
    print(f"Store: {MEM_HOME}")
    print(f"Index: {data['index_bytes'] / 1024:.1f} KB  ({data['index_path']})")
    print(f"Symbols: {data['symbols']}, superseded: {data['superseded']}")
    print()
    if data["by_type"]:
        print(f"{'type':<12}{'total':>7}{'active':>9}")
        for row in data["by_type"]:
            print(f"{row['type']:<12}{row['n']:>7}{row['active']:>9}")
    if data["by_scope"]:
        print()
        print("Scopes:")
        for row in data["by_scope"]:
            print(f"  {row['scope']:<24}{row['n']:>5}")
    if args.usage:
        usage = index.usage_summary()
        print()
        print(f"Injections: {usage['total_calls']} calls, ~{usage['total_tokens']} tokens")
        if usage["by_tool"]:
            print(f"{'tool':<12}{'calls':>9}{'tokens':>10}{'hits':>9}")
            for row in usage["by_tool"]:
                print(f"{row['tool']:<12}{row['calls']:>9}{row['tokens'] or 0:>10}{row['hits'] or 0:>9}")
    return 0


def cmd_eval(args) -> int:
    if args.add:
        question, _, ids = args.add.partition("=>")
        if not ids.strip():
            print("Format: --add \"question => note-id\"", file=sys.stderr)
            return 2
        eval_mod.add_case(question.strip(), [i.strip() for i in ids.split(",") if i.strip()])
        print(f"Added case to {store.EVAL_PATH}")
        return 0
    result = eval_mod.run()
    print(eval_mod.render(result))
    return 0


def cmd_mcp(args) -> int:
    from . import mcp_server
    mcp_server.serve()
    return 0


# ------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agtmem",
        description=(
            "Memory you own: Markdown on disk, an index you can delete."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the directory layout and the index").set_defaults(
        func=cmd_init
    )

    p = sub.add_parser("add", help="create or overwrite a note")
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.add_argument("--id")
    p.add_argument("--type", default="fact", choices=store.TYPES)
    p.add_argument("--scope", default="global")
    p.add_argument("--tags", nargs="*", default=None, help="one or more tags")
    p.add_argument("--origin", default="agent", choices=store.ORIGINS)
    p.add_argument("--detail", default="")
    p.add_argument("--supersedes", default="", help="id of the note this replaces")
    p.add_argument("--update", action="store_true", help="overwrite an existing note")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("bug", help="record a bug: symptom, cause, fix")
    p.add_argument("--symptom", required=True)
    p.add_argument("--cause", required=True)
    p.add_argument("--fix", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--title")
    p.add_argument("--id")
    p.add_argument("--scope", default="global")
    p.add_argument("--tags", nargs="*", default=None, help="one or more tags")
    p.add_argument("--origin", default="agent", choices=store.ORIGINS)
    p.add_argument("--detail", default="")
    p.add_argument("--update", action="store_true")
    p.set_defaults(func=cmd_bug)

    p = sub.add_parser("search", help="search (FTS5 + trigram, fused with RRF)")
    p.add_argument("query")
    p.add_argument("--scope")
    p.add_argument("--type", choices=store.TYPES)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--all", action="store_true", help="include superseded notes")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="print one note")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("list", help="list notes")
    p.add_argument("--type", choices=store.TYPES)
    p.add_argument("--scope")
    p.add_argument("--all", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("append", help="append to a note")
    p.add_argument("id")
    p.add_argument("text", nargs="?")
    p.set_defaults(func=cmd_append)

    sub.add_parser("candidates", help="list the candidate queue").set_defaults(
        func=cmd_candidates
    )

    p = sub.add_parser("promote", help="promote a candidate to a confirmed note")
    p.add_argument("id")
    p.add_argument("--to", default="fact", choices=store.TYPES)
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("anatomy", help="scan a codebase and build its map")
    p.add_argument("path")
    p.add_argument("--scope")
    p.set_defaults(func=cmd_anatomy)

    p = sub.add_parser("find", help="locate a symbol without reading the file")
    p.add_argument("symbol")
    p.add_argument("--scope")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--max-tokens", type=int, default=1000, dest="max_tokens")
    p.add_argument("--signatures", action="store_true")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("map", help="print the code map")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--scope")
    p.add_argument("--focus")
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--max-tokens", type=int, default=1200, dest="max_tokens")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser(
        "ingest-sessions",
        help="import context-compaction summaries from session logs",
    )
    p.add_argument("--since", help="only on or after YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p.add_argument("--limit", type=int, help="how many NEW notes to create per run")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("reindex", help="rebuild the index from the files")
    p.add_argument("--verbose", "-v", action="store_true")
    p.set_defaults(func=cmd_reindex)

    sub.add_parser("doctor", help="check store consistency").set_defaults(func=cmd_doctor)

    p = sub.add_parser("stats", help="store statistics")
    p.add_argument("--usage", action="store_true", help="show injection accounting")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("eval", help="measure retrieval quality")
    p.add_argument("--add", help='add a case: "question => note-id"')
    p.set_defaults(func=cmd_eval)

    sub.add_parser("mcp", help="serve MCP over stdio").set_defaults(func=cmd_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
