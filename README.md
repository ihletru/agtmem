# agtmem

**Long-term memory for coding agents that you actually own.**

Plain Markdown files on disk are the source of truth. A SQLite index is a
disposable cache. Nothing here calls a model, and nothing here needs the
network.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: 0](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)
[![Tests: 44](https://img.shields.io/badge/tests-44-brightgreen.svg)](tests/test_e2e.py)
[![MCP](https://img.shields.io/badge/MCP-stdio-8a2be2.svg)](docs/ARCHITECTURE.md)

---

## The problem

Most agent-memory products want to be the place your memory lives. That means
your accumulated context — the decisions, the bug fixes, the reasons you chose
one thing over another — ends up in someone else's database, behind someone
else's pricing page, in someone else's format.

The failure mode is not a crash. It is that the free tier shrinks, or the
pricing changes, or the company pivots, and now you are paying rent on your own
notes. Or you switch tools and start over with an empty memory.

`agtmem` takes the opposite position: **memory is a folder of text files.**

## The four rules

Everything else follows from these, and each one is enforced by a test.

1. **The store is the contract; the index is a cache.**
   Deleting the database must not be a loss. `rm ~/.agtmem/.index.sqlite` and
   the next query rebuilds it from the Markdown. This is the acceptance test,
   and it is in the suite.

2. **The server never calls an LLM.**
   Writing is file I/O. Retrieval is a SQL query. There is no per-recall token
   bill and no API key. Summarising is the agent's job, not the store's.

3. **Zero runtime dependencies.**
   Python standard library only, so this still runs on a bare interpreter in ten
   years. Asserted by the test suite, which parses every module and fails if any
   import is not in the standard library.

4. **Every write goes through one code path.**
   `index.index_note()` refreshes a note *and its supersession predecessor*.
   Bypassing it is how a replaced note silently keeps showing up in search
   results — which is exactly the bug that motivated the rule.

## Install

```bash
pip install -e .
agtmem init
```

Or without installing:

```bash
python -m agtmem init
```

Requires Python 3.10+ and a SQLite build with FTS5 (standard since 3.9;
`agtmem doctor` will tell you if yours lacks it).

## Quick start

```bash
# Record a decision
agtmem add --title "Index is disposable" --type decision --scope myproject \
  --body "Deleting the DB must not lose anything, so reindex is always safe."

# Record a bug in the three parts that make it reusable
agtmem bug --symptom "append hung for 15 seconds" \
           --cause "the file lock was not re-entrant" \
           --fix "per-process depth counter"

# Search it
agtmem search "why is the index disposable"

# Replace an outdated note (the old one is kept, marked superseded)
agtmem add --title "Index is disposable (revised)" \
  --supersedes index-is-disposable --body "..."

# Map a codebase once, then look symbols up without reading files
agtmem anatomy . --scope myproject
agtmem find store_lock --signatures
```

## How it works

Three layers, each replaceable without touching the others.

```
~/.agtmem/
├── facts/         durable truths
├── decisions/     why something was done      <- the most valuable category
├── bugs/          symptom / cause / fix
├── candidates/    unconfirmed, awaiting review
├── projects/      project overviews
├── anatomy/       codebase maps
├── sessions/      imported context-compaction summaries
├── log/           append-only journal
├── index.md       hand-curated entry point (not a note)
└── .index.sqlite  DELETE ME FREELY
```

A note is a Markdown file with a deliberately restricted frontmatter block —
flat `key: value` lines only, no nesting, no block scalars, so it parses with
the standard library and stays comfortable to edit by hand:

```markdown
---
id: index-is-disposable
title: Index is disposable
type: decision
scope: myproject
tags: design storage
status: active
origin: agent
captured: 2026-01-14
updated: 2026-01-14
---

Deleting the database must not lose anything.
```

**Retrieval** is hybrid: SQLite FTS5 with `unicode61 remove_diacritics=2` for
stemmed word matching, plus a trigram index for substrings that tokenisation
destroys (a file path, a hyphenated package name, an identifier). The two
result lists are fused with
Reciprocal Rank Fusion, so neither ranker needs score calibration against the
other.

## Use it from an agent (MCP)

```json
{
  "mcpServers": {
    "agtmem": {
      "command": "python",
      "args": ["-m", "agtmem", "mcp"]
    }
  }
}
```

Eight tools:

| Tool | Purpose |
|---|---|
| `agtmem_search` | search memory; returns snippets with ids |
| `agtmem_read` | read one note, bounded by `max_tokens` |
| `agtmem_write` | create or update a note; `supersedes=<id>` to replace |
| `agtmem_append` | append to a note, concurrency-safe |
| `agtmem_index` | rebuild the index; with `path`, also scan a codebase |
| `agtmem_bug` | record symptom / cause / fix |
| `agtmem_candidates` | list the candidate queue; `promote=<id>` to accept |
| `agtmem_find` | locate a symbol, get `file:line` back |

Two implementation details that matter if you write your own MCP server:

- **stdout carries JSON-RPC and nothing else.** One stray `print()` corrupts
  the stream and the client drops the connection. All human-readable output
  goes to stderr. The test suite asserts that every stdout line parses as JSON.
- **Tool failures are results, not protocol errors.** A missing note comes back
  as `isError: true` with the connection intact, not as a JSON-RPC error.

## Supersession instead of deletion

When knowledge changes, the old note is **not** deleted. It gets
`status: superseded` and a forward pointer, and the new note gets a backward
one:

```bash
agtmem add --title "New approach" --supersedes old-note --body "..."
```

Default search skips superseded notes; `--all` includes them. You keep the
history of *what changed and why*, which is usually more valuable than the
current answer alone.

## Concurrency

Writes take an OS advisory lock (`fcntl.flock` / `msvcrt.locking`) and are
published atomically (`mkstemp` + `os.replace`).

- The lock is **re-entrant within a process**, because `append_to()` takes the
  lock and then calls `save()`, which takes it again.
- The lock is **released by the kernel when a process dies**, for any reason
  including `SIGKILL`, so a crashed writer cannot wedge the store.
- The `.lock` file is **never deleted and never written to**. Deleting a file
  another process may be blocked on is a race that ends with two writers on two
  different inodes; writing to a byte range someone else has locked fails with
  `EACCES` on Windows.

The test suite runs twelve concurrent writers and asserts no update is lost.

## Measuring whether it helps

A memory system you cannot measure is a liability, because it costs context on
every recall. `agtmem eval` reports R@5 and P@5 for both `agtmem` and a plain
grep over the same files, so the number means something:

```bash
agtmem eval --add "why is the index disposable => index-is-disposable"
agtmem eval
```

`agtmem stats --usage` reports how many tokens each retrieval actually injected.

### Measured on a real corpus

Numbers below come from a 92-note store of real project session summaries, with
15 cases whose ground truth was established by **grepping the corpus for a
distinctive phrase** — never by reading search results, which would make the
eval score 1.0 by construction.

| | R@5 | P@5 |
|---|---|---|
| `agtmem` | **0.667** | 0.147 |
| grep baseline | 0.533 | 0.120 |

The interesting result is not the aggregate but the split by how the question is
phrased, using the same 15 answers:

| Query phrasing | R@5 |
|---|---|
| Term-style (`gradlew wrapper apk build`) | **0.867** |
| Natural language (`How do I build the APK without gradlew?`) | 0.667 |

**That gap is the honest limitation of lexical retrieval, and it is worth
stating plainly.** When a query shares vocabulary with the note — which is the
normal case for an agent recalling its own work — retrieval is good. When the
query *paraphrases* the note, bag-of-words fails, because the note may contain
only two of the five words asked about.

Four alternative strategies were implemented and measured against the same
cases, and every one of them plateaued at the same 0.667 on natural language:

| Strategy | R@5 |
|---|---|
| coverage-first re-ranking (shipped) | 0.667 |
| proximity (`NEAR`) matching | 0.667 |
| coverage as a score multiplier | 0.667 |
| title/tags weighted in BM25 | 0.600 |
| AND-first with OR fallback | 0.533 |
| *grep* | *0.533* |

None of the four closed the gap, which is the expected result: paraphrase
robustness is what embeddings buy you, and this project deliberately does not
ship a model on the hot path. If your queries are paraphrases rather than terms,
you want a vector index — and you should measure it, because the difference is
not obvious from the outside.

## What it deliberately does not do

- **No embeddings, no vector search.** FTS5 plus trigram covers thousands of
  notes without a model, a GPU, or a download.
- **No LLM calls.** Summarising belongs to the agent.
- **No proprietary database format.** If SQLite disappeared tomorrow, the `.md`
  files are still yours.
- **No cloud, no account, no sync.** It is a folder. Sync it with whatever you
  already use.

## Credits and prior art

This project borrowed a lot of ideas. The specific debts are listed in
[CREDITS.md](CREDITS.md) — including which ideas came from OpenWolf, agentmemory,
memanto/Moorcheh, Basic Memory, and the context-compaction behaviour of
Claude-Code-style agents.

No code was copied; all of it is stdlib-only and written from scratch. Ideas and
architecture are not copyrightable, which is precisely why the credits are
specific rather than a vague "inspired by others".

## How this was built

This project was designed and written in collaboration between a human
(willhack-g) and an AI coding agent. The division of labour was roughly:

- **The human** set the direction, rejected the subscription-model alternatives,
  chose the design principles, and made the calls that mattered — including
  "delete the database must not be a loss" and "the server never calls an LLM".
- **The agent** wrote essentially all of the code, the tests, and this
  documentation, and found and fixed the four concurrency and index bugs
  documented in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#bugs-found-only-at-runtime).

It seems more useful to say that plainly than to pretend otherwise. Note also
that copyright in AI-generated material is unsettled in several jurisdictions
(US law, for instance, requires human authorship), which is one more reason the
LICENSE names a human copyright holder.

## License

[MIT](LICENSE) — do what you like with it.
