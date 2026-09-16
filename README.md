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

`agtmem` speaks MCP over stdio, so any MCP-capable agent can use it. Installing
the package creates **`agtmem-mcp`**, a no-argument executable:

```bash
pip install -e .
which agtmem-mcp        # note the full path; most clients do not inherit PATH
```

Use the **full path** to the executable in client configs. Many MCP clients spawn
servers with a minimal environment that does not include a virtualenv's `bin` or
`Scripts` directory, so a bare `agtmem-mcp` may resolve for you in a terminal and
still fail to start under the client.

`AGTMEM_HOME` is optional and defaults to `~/.agtmem`. Set it only if you keep the
store somewhere else.

Most clients are satisfied with `command` alone. **WorkBuddy is not** — read the
next section before writing its config, or you will lose an evening to it.

### WorkBuddy AI

Add the server to `~/.workbuddy-ai/mcp.json` (note the filename — it is **not**
`.mcp.json`). On Windows the backslashes must be escaped:

```json
{
  "mcpServers": {
    "agtmem": {
      "command": "C:\\Users\\you\\envs\\default\\Scripts\\agtmem-mcp.exe",
      "args": [],
      "env": {
        "AGTMEM_HOME": "C:\\Users\\you\\.agtmem",
        "PYTHONIOENCODING": "utf-8"
      },
      "disabled": false
    }
  }
}
```

> **`"args": []` is load-bearing — omit the key and WorkBuddy exposes zero
> tools.** The server still starts, still answers `tools/list` with all eight
> tools in under a second, and still shows as enabled and trusted in the UI. It
> just never reaches the agent, and nothing in the logs says so.
>
> This is not hypothetical: it cost an evening here, and the same missing key had
> silently disabled an unrelated third-party server in the same config — which is
> what finally gave the pattern away.
>
> The fix is free. WorkBuddy hashes
> `sha256(command + "|" + sorted(args).join(",") + "|" + sorted(env KEYS).join(","))`,
> and a missing `args` and `args: []` serialise identically — so **the hash is
> unchanged and the approval below survives**. No second Trust click.

> **You must approve it once.** WorkBuddy does not spawn a third-party MCP server
> just because it is in the config. Until you approve it, the connector shows as
> disabled with *"This third-party MCP server requires your approval before
> connecting."* Open the connector management page and click **Trust** on
> `agtmem`. Restarting the app alone does not do it.

Two more consequences of that hash:

- It covers the **`env` key names, not their values**. Changing `AGTMEM_HOME`'s
  value is free; adding or removing an env key means approving the server again.
- **Every edit to `mcp.json` needs a restart.** The file is read at startup only,
  so a correct fix applied to a running app changes nothing yet.

#### If the tools never appear

Four independent layers can each fail, and from the outside **they all look the
same**: no tools, no error. Check them in this order.

1. **Is it trusted?** `~/.workbuddy-ai/mcp-approvals.json` must contain
   `<configHash>::agtmem`. *Enabled* and *trusted* are different things.
2. **Is the entry well-formed?** Specifically, does it carry `"args": []`?
3. **Is the server itself healthy?** Rule this out *first*, by probing rather than
   by reading logs. WorkBuddy spawns stdio servers with **only the variables in
   the server's `env` block** — it does not inherit your shell — so reproduce
   exactly that: spawn the command with just those keys plus
   `SystemRoot`/`windir`/`PATH`, send `initialize` and `tools/list`, and count the
   tools. If that prints eight, the fault is in the client config, not here.
4. **Only then** read the logs. They live in a dated directory, not `main.log`:
   `~/.workbuddy-ai/logs/<YYYY-MM-DD>/`, where `skipping untrusted server "agtmem"`
   means exactly what it says. Note that current builds log **no connect line for
   stdio servers at all**, so a missing line proves nothing either way.

The one reliable test is the tool index itself: have the agent look up
`mcp__agtmem__agtmem_search`. If that name does not resolve, the tools are not
loaded — whatever the UI and the logs suggest.

### Hermes

Add a block under the top-level `mcp_servers:` key in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  agtmem:
    command: "/absolute/path/to/agtmem-mcp"
    env:
      AGTMEM_HOME: "~/.agtmem"
    enabled: true
    timeout: 120
    connect_timeout: 60
```

Then reload with `/reload-mcp` (or verify with `hermes mcp test agtmem`).

### Claude Desktop, Cursor, and other JSON clients

```json
{
  "mcpServers": {
    "agtmem": {
      "command": "/absolute/path/to/agtmem-mcp"
    }
  }
}
```

For clients that only accept `command` plus `args`, the module works too:

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

### The eight tools

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

`initialize` returns an `instructions` field telling the agent to call
`agtmem_search` before starting work and to record conclusions with
`agtmem_write`. Clients that surface it will nudge the agent to use memory
without any prompt engineering on your side.

### If you write your own MCP server

Two things that cost real debugging time here:

- **stdout carries JSON-RPC and nothing else.** One stray `print()` corrupts the
  stream and the client drops the connection. All human-readable output goes to
  stderr. The test suite asserts that every stdout line parses as JSON.
- **Tool failures are results, not protocol errors.** A missing note comes back
  as `isError: true` with the connection intact, not as a JSON-RPC error that
  kills the session.

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

## Raw sessions are not knowledge

`type: session` notes are **excluded from search by default**. Pass `--sessions`
(or `sessions: true` over MCP) to get them back.

A session is the transcript a note was distilled *from* — input, not knowledge.
It is also roughly ten times the size of a note (median 20 kB against 2 kB), and
BM25 rewards length: a transcript repeats every term the question uses, so it
outranks the note that actually answers it. The effect is not subtle. On a store
with 92 sessions among 275 notes, *every one of the top ten results* for
`"How do I build the Android APK without gradlew?"` was a raw session. With
sessions excluded, the first result is `verbigem-android-build-invocation` — the
note that answers the question.

The exclusion happens in SQL rather than after ranking. Filtering afterwards
would let sessions consume the recall budget and hand back fewer results than
asked for; the note has to surface even when a transcript would have outranked
it.

## Feeding it: distilling sessions

`ingest-sessions` imports raw session summaries. That is the *input*, not the
answer — a store holding 92 raw sessions is a store nobody reads. Turning them
into `decisions/`, `facts/` and `bugs/` is a two-stage job, and both stages
matter.

### Stage 1 — distillation

Read a raw session and keep only what has lasting value: a decision, a fact
about the system, a bug with symptom/cause/fix. Skip narrative, plans, and
one-off exploration.

Measured density on a real corpus: **4.2 notes per session**. Across 92 sessions
that is ~390 notes — which is the trap. Distillation on its own swaps one
problem ("too many raw sessions") for another ("too many notes").

### Stage 2 — consolidation

Merge notes covering the same subject into one stronger note, and mark the
sources superseded. Nothing is lost: superseded files stay on disk and
`search --all` still finds them, they simply stop competing for rank with the
note that replaced them.

On the same corpus, 21 distilled notes collapsed to 12 — three Play Console
notes into one, three Firebase identity notes into one, two admin-panel notes
into one, and so on. **A consolidation pass roughly halves the count without
dropping a fact.**

### Cap the batch

Five to eight sessions per run. Not a performance limit — a review limit. 390
notes nobody reads is a worse deliverable than 21 notes somebody does.

### Mark what has been processed

The store cannot do this for you, and the obvious place does not work: a custom
frontmatter key is **silently dropped** on the next save, because
`Note.from_file()` reads only the fixed `FM_KEYS` tuple and `Note.render()`
writes `to_meta()` back over it.

Use an append-only register note instead (`log/session-distillation-audit`).
Every run appends a section listing the session ids it consumed, and *no entry
means not processed*. It is greppable, human-readable, and survives a
serialization round-trip.

### Where `scope` comes from

`ingest-sessions` derives a session's scope from the transcript's project
directory, not from the conversation: the directory is slugified, a leading
`users/<name>` or `home/<name>` is dropped, and the last two segments are kept.
A trailing session timestamp is stripped first — WorkBuddy names ad-hoc project
directories after the workspace *plus* the moment the session started, so
`c-Users-milo-WorkBuddy AI-2026-09-04-11-43-59` yields `workbuddy-ai` and not
the clock reading `43-59`.

Two consequences worth knowing:

- **A session run from a scratch workspace gets the scratch scope**, not the
  project it was actually about. The directory cannot know what was discussed.
  If that matters, re-scope the note by hand — `scope` is a frontmatter field and
  nothing in the store depends on it for file layout (except `type: project`).
- **Nothing validates a scope.** A bad one is not an error, it is a new bucket,
  and `agtmem stats` will list it next to your real projects as if it were one.
  Glance at that list occasionally.

### Running it as a scheduled job

The server deliberately cannot do this — rule 2 above forbids LLM calls from
`agtmem` itself, and that is the right call. Distillation belongs to whatever
agent is already running, expressed as a scheduled prompt. The configuration
used here:

| | |
|---|---|
| schedule | daily, 07:00 local |
| cap | 8 sessions per run |
| scope | one scope per run, in a fixed order; move to the next when the current one runs out |
| consolidation | mandatory, in the same run |
| register | append to `log/session-distillation-audit` |

The prompt hands the agent six steps: (1) read the register to learn what is
already done, (2) take the **first scope in the list that still has unprocessed
sessions** and pick at most 8 of them, (3) distill them into
decisions/facts/bugs, (4) consolidate duplicates and mark the sources
superseded, (5) append a register section, (6) run `reindex` and `doctor`.

**One scope per run is deliberate.** The consolidation step has to notice that
two notes say the same thing, and that judgement is much easier inside a single
project's vocabulary than across three of them. A run that drains a scope
early is a short run, not a wasted one.

The list itself is just an ordered set of scope names, and the order should
match whatever you care about. It only has to be explicit: "the largest
remaining scope" sounds reasonable and drifts, because it depends on the agent
noticing that the previous scope is empty.

Two things to get right if you copy it:

- **Pass every field when updating a note.** `write_note(..., update=True)`
  replaces `type`, `scope` and `tags` instead of merging them, and the file
  moves to a different directory. An update that omits them silently resets the
  note's type and relocates it — which is how a `log/` note ends up in `facts/`.
- **Cap the run.** An uncapped job produces more notes than anyone will read,
  which is the exact failure this section exists to prevent.

Nothing in the store depends on the schedule. It is a convenience layer over
`agtmem add` and `agtmem search`: turn it off and the store keeps working.

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
agtmem eval --add-gap "a question no note answers"
agtmem eval
```

A line beginning with `!` records a **known coverage gap**: a question the store
cannot answer because no note was ever distilled for it. Those are counted
separately and excluded from R@5, because no amount of ranking work can return a
note that was never written — mixing them in would make a distillation gap look
like a retrieval failure and send you tuning the wrong component.

`agtmem stats --usage` reports how many tokens each retrieval actually injected.

### Measured on a real corpus

Numbers below come from a 275-note store (183 distilled notes plus 92 raw
session transcripts) with 20 scored cases. Ground truth was established by
**grepping the corpus for a distinctive phrase** and confirming it occurs in
exactly one active note — never by reading search results, which would make the
eval score 1.0 by construction.

| | R@5 | P@5 |
|---|---|---|
| `agtmem` | **1.00** | 0.21 |
| grep baseline | 0.05 | 0.01 |

P@5 looks low and is not a defect: most questions have exactly one right answer,
so 0.2 is the best achievable score. It is reported anyway, because a metric
that can only go up is not a metric.

Two rules that cost real debugging time, and are worth copying if you build your
own set:

- **Never point ground truth at a raw session.** It is input, not knowledge, and
  once sessions are excluded from search the case fails for a reason unrelated to
  ranking quality.
- **Never point it at a `superseded` note.** Superseded notes are hidden by
  default, so the case is unanswerable by construction. Four of the first
  draft's targets had been superseded and had to be re-pointed at their
  successors. `agtmem eval --add` now refuses both mistakes instead of writing
  the case.

### Two layers, measured separately

An agent only ever reads distilled notes, so that is the layer the headline
number covers. But the same search can be pointed at the raw transcripts with
`--sessions`, and there the ranking used to fail badly: a session has a median
size of 21 160 B against 2 321 B for a note, so it contains every query term
simply by being a transcript of everything, and it won on length rather than on
relevance.

Counting term *presence* could not fix that, because both documents contain the
same terms. What fixed it was discounting coverage by length — see
`COVERAGE_FREE_BYTES`. Measured on the same 20 cases, with sessions left in the
pool:

| | before | after |
|---|---|---|
| correct note pushed out of the top 5 | 15/20 | **0/20** |

The discount has to be gentle at note scale, and that is the part worth knowing
if you build something similar: it is one knob with two opposing requirements.
Too strong, and it overrides better evidence — a 4.2 kB note matching four query
terms must not outrank a 6.5 kB note matching five. Too weak, and a transcript
starts winning again. The usable window measured out at 5 500–6 500 B, which is
narrow. Sorting by term count first with length as a tie-break looks like the
obvious fix and is wrong: it repairs the first case and re-breaks the second,
because a transcript matches *more* distinct terms than a note does.
| top result was a raw transcript | 17/20 | **0/20** |

### What this eval does not measure

It now sits at R@5 = 1.00 on the knowledge layer, which means **it has no
headroom left and can no longer tell two good ranking strategies apart.** The
questions were written from each note's own vocabulary, so a query that
paraphrases a note without sharing any of its words is still untested.

An earlier version of this table reported a gap between keyword queries (0.867)
and natural-language ones (0.667). Re-measured on corrected ground truth, both
phrasings score 1.00 — so that gap was at least partly an artifact of the old
ground truth, which pointed at transcripts, and a transcript of everything is
exactly the document a keyword query finds and a paraphrase misses.

The underlying limitation is unchanged: this is lexical retrieval, and lexical
retrieval is only as good as the vocabulary overlap between question and note.
Four alternatives were tried against the older set — proximity (`NEAR`) matching,
coverage as a score multiplier, title/tags weighted in BM25, and AND-first with
an OR fallback — and none beat coverage-first re-ranking. Paraphrase robustness
is what embeddings buy, and this project deliberately does not ship a model on
the hot path. If your queries are paraphrases rather than terms, you want a
vector index — and you should measure it, because the difference is not obvious
from the outside.

## What it deliberately does not do

- **No embeddings, no vector search.** FTS5 plus trigram covers thousands of
  notes without a model, a GPU, or a download.
- **No LLM calls.** Summarising belongs to the agent — see
  [Feeding it: distilling sessions](#feeding-it-distilling-sessions) for the
  workflow that does it, including the batch cap and the register note.
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
(ihletru) and an AI coding agent. The division of labour was roughly:

- **The human** set the direction, rejected the subscription-model alternatives,
  chose the design principles, and made the calls that mattered — including
  "delete the database must not be a loss" and "the server never calls an LLM".
- **The agent** wrote essentially all of the code, the tests, and this
  documentation, and found and fixed the four concurrency and index bugs
  documented in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#8-bugs-found-only-at-runtime).

It seems more useful to say that plainly than to pretend otherwise. Note also
that copyright in AI-generated material is unsettled in several jurisdictions
(US law, for instance, requires human authorship), which is one more reason the
LICENSE names a human copyright holder.

## License

[MIT](LICENSE) — do what you like with it.
