# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-09-15

First public release.

### Added

- **Store** (`store.py`): Markdown notes with restricted flat frontmatter, eight
  note types, `origin` provenance, supersession with forward/backward pointers,
  atomic writes, and a re-entrant cross-process lock.
- **Index** (`index.py`): disposable SQLite cache with FTS5 (`unicode61
  remove_diacritics=2`) and trigram tables, Reciprocal Rank Fusion, supersession
  filtering, `PRAGMA user_version` schema versioning with automatic rebuild, and
  injection accounting.
- **Code map** (`anatomy.py`): regex-based symbol extraction for Python, JS/TS,
  Kotlin, Java, Go, Rust, and C#, ranked by importer count.
- **Session ingest** (`ingest.py`): read-only harvest of existing
  context-compaction summaries from JSONL transcripts.
- **Eval harness** (`eval.py`): R@5 / P@5 for `agtmem` and for a plain grep
  baseline over the same files.
- **CLI** (`cli.py`): 18 subcommands.
- **MCP server** (`mcp_server.py`): JSON-RPC 2.0 over stdio, eight tools, zero
  dependencies.
- **Tests** (`tests/test_e2e.py`): 42 checks including the delete-the-index
  acceptance test, twelve concurrent writers, and assertions that the MCP
  server's stdout carries only JSON-RPC.

### Fixed during development

Four defects that were invisible in the design and appeared within the first hour
of real use. Documented in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#8-bugs-found-only-at-runtime).

- **Deadlock against its own lock.** `append_to()` acquired the lock and then
  called `save()`, which acquired it again. With an `O_CREAT|O_EXCL` marker the
  second acquisition waited on a lock the same process held, then died on
  timeout. Every `append` was broken while `add` worked. Fixed with a
  per-process re-entrancy depth counter.
- **Superseded notes kept appearing in results.** The predecessor's *file* was
  rewritten but its *index row* was not, so the supersession filter had nothing
  to filter. Root cause was duplication: the same
  `connect/ensure_schema/upsert/close` block was copy-pasted into five CLI
  commands and the MCP server. Consolidated into `index.index_note()`.
- **Stale-lock recovery was unreachable.** `stale_after=90s` with `timeout=15s`
  meant a leaked lock guaranteed failure for 90 seconds while the process gave up
  after 15. Replaced the existence-marker lock with an OS advisory lock, which
  the kernel releases on process death.
- **Writing to a byte-range-locked file fails on Windows.** A leftover "ensure
  the lock file has a byte" step raised `PermissionError: [Errno 13]` whenever
  another process already held byte 0. Intermittent — roughly two failures in six
  runs, on a different line each time. Fixed by never writing to the lock file.

### Notes

- Token counts from `stats --usage` are `len(text) // 4` estimates, labelled as
  such, never presented as measured provider counts.
- `~/.agtmem` is the default store location. `AGTMEM_HOME` overrides it;
  `MEM_HOME` is accepted as a legacy alias.

[0.1.0]: https://github.com/willhack-g/agtmem/releases/tag/v0.1.0
