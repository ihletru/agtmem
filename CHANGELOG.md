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
- **Tests** (`tests/test_e2e.py`): 44 checks including the delete-the-index
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

### Changed

Retrieval was reworked after measuring it against a real corpus, rather than
assuming the design worked. Details and numbers in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#7-measuring).

- **Two-stage retrieval.** Stage 1 recalls up to 60 candidates from both rankers
  via RRF; stage 2 re-orders them by how many distinct query terms they contain.
  BM25 rewards a rare term but not matching *several* terms, so without stage 2 a
  long note repeating one common word outranks a short note that answers the
  whole question. Measured: R@5 0.600 → 0.667.
- **Stopwords are dropped before building the FTS query.** The query is an OR, so
  every function word widens the pool with noise. The list covers English and
  Polish.
- **The trigram query now ORs individual tokens as quoted phrases.** It
  previously quoted the *entire* query as one phrase, which matched nothing for
  any real question — the trigram index was effectively dead. `ONSOLE_FILL` now
  returns 4 hits where FTS returns 0.
- **Recall pool raised from `limit * 4` to a fixed 60**, so re-ranking has room
  to promote a candidate that the first pass ranked low.
- Added `agtmem-mcp`, a no-argument MCP entry point, so MCP clients can point at
  an executable instead of passing `["-m", "agtmem", "mcp"]` as arguments.
- **Claims about other products were overstated.** The README opened with "Every
  agent-memory product wants to be the place your memory lives" — four were
  examined, not all of them. Now "Most". The same review caught "No lexical trick
  closed the gap" (four strategies were tested, not all possible ones) and a
  "Verified in CI-by-test" line for a repository that has no CI.
- **The zero-dependency claim is now asserted rather than stated.** A new test
  parses every module and fails if any import is outside the standard library, and
  checks that `pyproject.toml` still declares no dependencies. A claim like that
  rots the moment someone adds an import, so it should not rest on trust.
  Tests: 42 → 44.

### Measured

On a 92-note store of real session summaries, 15 cases with ground truth
established by grepping the corpus (not by reading search results):

| | R@5 | P@5 |
|---|---|---|
| agtmem | 0.667 | 0.147 |
| grep baseline | 0.533 | 0.120 |

By query phrasing, over the same 15 answers: **0.867 term-style**, **0.667
natural-language**. Four alternative strategies (proximity `NEAR`, coverage as a
multiplier, title weighting, AND-first) were measured; all plateaued at 0.667 or
below on natural language. The paraphrase gap is structural for lexical
retrieval, not a missing trick.

### Notes

- Token counts from `stats --usage` are `len(text) // 4` estimates, labelled as
  such, never presented as measured provider counts.
- `~/.agtmem` is the default store location. `AGTMEM_HOME` overrides it;
  `MEM_HOME` is accepted as a legacy alias.

[0.1.0]: https://github.com/willhack-g/agtmem/releases/tag/v0.1.0
