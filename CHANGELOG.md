# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Length no longer counts as relevance in re-ranking.** Stage 2 scored a
  candidate by how many distinct query terms it *contains*, which cannot tell a
  note that answers a question from a transcript that contains everything: a raw
  session (median 21 160 B) holds every query term by construction, so it beat
  the 2 321 B note that actually answered. Coverage is now divided by a
  logarithmic length penalty — `terms / (1 + log2(size / 5000))`.
  `COVERAGE_FREE_BYTES = 5000` came from a sweep against two metrics at once
  (knowledge-layer R@5, and correct-note survival with sessions left in the
  pool), which gives a plateau of 3 500–6 500 B; 5 000 is the middle of it, so it
  has margin on both sides. The first value tried was the median note size
  (2 500 B) and it *lowered* R@5 to 0.90, because the notes that answer questions
  are the substantial ones. 5 000 B is the 97th percentile of note size, so
  ordinary notes score on coverage alone. With sessions in the pool: correct note
  pushed out of the top 5 went from 15/20 to **0/20**, and top-1 transcripts from
  17/20 to **0/20**. Knowledge-layer R@5 is unchanged at 1.00.
- **Raw sessions are excluded from search by default** (`--sessions` /
  `sessions: true` to include them). A session is the transcript a note was
  distilled from — input, not knowledge — and at ~20 kB it is about ten times
  the size of a note, so BM25 hands it the top of every result list. Measured on
  a store with 92 sessions among 275 notes: all ten results for a build question
  were raw transcripts; with the exclusion, the first result is the note that
  answers it. The filter is applied in SQL, not after ranking, so sessions
  cannot eat the recall budget.

### Added

- **The eval separates a retrieval miss from a coverage gap.** A line beginning
  with `!` in `eval.txt` records a question no note answers. Those are counted
  separately and excluded from R@5, because no amount of ranking work can return
  a note that was never written — averaging the two together produces a number
  that describes neither failure. `agtmem eval --add-gap "question"` writes one.
- **`agtmem eval --add` refuses ground truth that search can never return**: a
  note id that does not exist, or one that is `superseded` (hidden from search by
  default). Both look exactly like a retrieval bug once the case is in the file,
  so they are rejected at the moment of writing. Four of the first draft's
  targets turned out to be superseded.
- **The eval set was re-pointed at distilled notes.** Ground truth had pointed at
  raw `session-*` ids, which made it measure the wrong layer and then read 0.0
  the moment sessions were excluded. The set is now 20 scored cases plus two
  known coverage gaps, every target verified to be an active note.
- **README: "Two layers, measured separately"** — the knowledge-layer number and
  the transcript-layer number, why they differ, and the before/after table.
- **README: "What this eval does not measure"** — the set is at R@5 = 1.00 and
  therefore cannot discriminate between strategies; the questions were written
  from each note's own vocabulary, so a zero-overlap paraphrase is still untested.
- **README: "Raw sessions are not knowledge"** — why the exclusion exists and
  what it costs to leave it out. Also documents the measurement above.
- **README: "Feeding it: distilling sessions" — the missing half of the loop.**
  `ingest-sessions` fills the store with raw summaries and nothing in the tool
  turns them into `decisions/`, `facts/` and `bugs/`; rule 2 forbids the server
  from calling an LLM, so that step belongs to the agent. The new section
  documents the two stages (distillation, then consolidation), the measured
  4.2-notes-per-session density and why an uncapped run just relocates the
  problem, the append-only register note used to mark processed sessions
  (a custom frontmatter key does not survive a save), and a scheduled-job
  configuration with its batch cap. Documentation only; no code change.

### Fixed

- **The retrieval numbers in the README and `docs/ARCHITECTURE.md` described an
  eval that was measuring the wrong layer.** They reported R@5 = 0.667 with a
  term-style/natural-language split of 0.867/0.667. Re-measured on corrected
  ground truth both phrasings score 1.00, so that gap was at least partly an
  artifact of ground truth pointing at raw transcripts — a transcript of
  everything being precisely what a keyword query finds and a paraphrase misses.
  The retired figures are marked as superseded rather than deleted, and the
  conclusion that survives (paraphrase robustness needs embeddings) is now
  stated on its design rationale instead of on those numbers.
- **Tests: 57 → 67.** New coverage for the length penalty (a long note with
  identical term coverage must not outrank the short note that answers) and for
  the eval's gap accounting and `--add` guard.

- **A session timestamp in the project directory name became the note's scope.**
  WorkBuddy names ad-hoc project directories after the workspace *plus* the
  moment the session started (`c-Users-milo-WorkBuddy AI-2026-09-04-11-43-59`),
  and `scope_from_dir()` took the last two slug segments — so nine sessions
  landed in a scope called `43-59`, a bucket indistinguishable from a real
  project in `agtmem stats`. A trailing timestamp is now stripped before the
  last segments are chosen, so that directory yields `workbuddy-ai`. Seven cases
  added to the test suite. Existing notes had to be re-scoped by hand; the store
  does not rewrite scope on its own.
- **README: the WorkBuddy example silently produced a server with zero tools.**
  WorkBuddy exposes no tools for a stdio server whose config entry omits the
  `args` key — the server still starts and still answers `tools/list` correctly,
  it just never reaches the agent. The example now carries `"args": []`, and the
  WorkBuddy section documents the trust gate, the config hash, and how to tell
  the four failure modes apart. Configuration and documentation only; no code
  change.

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
  whole question. Measured: R@5 0.600 → 0.667 (on ground truth since superseded
  — see [Measured](#measured) below).
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
- **Documented installation for specific agents** rather than only a generic
  snippet: WorkBuddy AI (`~/.workbuddy-ai/mcp.json`, including the one-time
  approval step), Hermes (`~/.hermes/config.yaml` under `mcp_servers:`, reload
  with `/reload-mcp`), and Claude Desktop / Cursor / other JSON clients. Also
  notes why the **full path** to the executable matters — most MCP clients spawn
  servers without inheriting PATH, so a bare `agtmem-mcp` can work in your
  terminal and still fail to start under the client.
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

> **Superseded 2026-09-16.** These figures were measured against ground truth
> that pointed at raw session transcripts rather than distilled notes, so they
> describe the wrong layer and are not comparable with the current numbers. Kept
> as a record of what was believed at the time. Current figures: see
> [Unreleased](#unreleased) and
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#7-measuring).

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

[0.1.0]: https://github.com/ihletru/agtmem/releases/tag/v0.1.0
