# Architecture

Why `agtmem` is built the way it is, and what the alternatives cost.

The short version: **the store is the contract, the index is a cache, and access
is a plug-in.** Everything below is a consequence of taking that seriously.

---

## 1. Why not an existing product

Four architectures were evaluated before writing anything, and each failed for a
different structural reason rather than a matter of taste.

| Approach | The problem |
|---|---|
| Hosted memory service (namespaces + vectors behind an MCP server) | The free tier caps *storage*. The cap does not move when you change your LLM provider, because it is a property of the backend. Migrating out means rewriting everything that accumulated. |
| Local-first note tool with its own database | The store is the tool's format. If the tool dies, you have a database you cannot read, not notes you own. |
| Vector search over an embedded model | Embeddings are on the *high-frequency* path: every write and every recall needs one. That is a model download, a runtime, and a GPU or an API call on the hot path — for a corpus of a few thousand notes. |
| Just grep | Works, and is the honest baseline. It fails on ranking, on supersession, and on not knowing what exists. |

The design that survives all four constraints is boring: text files you own, and
an index you can throw away.

## 2. The four rules

### Rule 1 — deleting the index must not be a loss

This is the load-bearing rule and it is a *test*, not a promise:

```bash
rm ~/.agtmem/.index.sqlite*
agtmem search "anything"      # works; the index rebuilds itself
```

`index.ensure_schema()` compares `PRAGMA user_version` against the code's
`SCHEMA_VERSION`. On a mismatch — a fresh file, a deleted file, or a schema
change — it drops the note tables, recreates them, and **repopulates from disk**
before returning. So a cold database heals on first use, and a schema change can
never silently return wrong results from stale columns.

For a cache, the right migration is to rebuild rather than to `ALTER`.

Two tables are deliberately **not** dropped on a version bump: `symbols` and
`usage`. They are not derived from the note files, and discarding the injection
accounting would destroy the only evidence of what memory costs.

### Rule 2 — the server never calls an LLM

Writing a note is `open()` and `os.replace()`. Searching is a SQL query. There is
no API key, no per-recall token cost, and no network in the request path.

This is not an anti-LLM position; it is a placement decision. Summarising and
distilling *are* LLM work — they belong to the agent, which already has a model
in hand and can be asked to do it. Putting a model inside the store would mean
the store needs credentials, has a bill, and fails when the provider does.

### Rule 3 — zero runtime dependencies

Standard library only. The reason is durability, not minimalism: `import
sqlite3` and `import os` will still work in ten years, and a lock file plus
`os.replace()` will still work on whatever the platform becomes.

The cost is real and paid explicitly:

- **No `tree-sitter`** for the code map. `anatomy.py` uses per-language regexes
  instead. It is less precise — it will miss dynamically generated symbols and
  anything unusual — but every result carries a `file:line` the agent can verify,
  and it does not reject a file it cannot understand the way a parser can.
- **No `pydantic`** for frontmatter. A restricted flat `key: value` subset is
  parsed by hand, which is why the frontmatter format is deliberately flat. No
  nesting, no block scalars, no multi-line strings.

### Rule 4 — one write path

`index.index_note(note)` is the only correct way to persist a note into the
index. It upserts the note **and its supersession predecessor**.

This rule exists because of a real bug. Marking a predecessor superseded happens
in the *store* (rewriting its file), so its index row goes stale. The code that
originally did this was a five-line
`connect / ensure_schema / upsert / close` block copy-pasted into five CLI
commands and the MCP server. One copy forgot the predecessor, and replaced notes
kept appearing in search results.

The lesson generalises: **when the same block appears six times, one of the
copies is wrong.** Consolidating it was not tidying, it was the fix.

## 3. Data model

### Note types

Eight types, each with a directory. The type is not decoration — it changes what
the note is expected to contain and how it should be trusted.

| Type | What it holds |
|---|---|
| `decision` | A choice and its rationale. **The most valuable category** — it is the thing that is expensive to reconstruct. |
| `fact` | A durable truth about the world or the project. |
| `bug` | Symptom, cause, fix. Fixed structure so entries are comparable. |
| `candidate` | An inference the agent made that a human has not validated. |
| `anatomy` | A generated codebase map. |
| `project` | A project overview. |
| `session` | An imported context-compaction summary. Raw material. |
| `log` | Append-only journal. |

`candidate` exists to solve a specific problem: an agent that cannot distinguish
"the user told me this" from "I inferred this" will eventually assert the second
as if it were the first. Candidates are retrievable but flagged, and
`agtmem candidates` / `promote` is the review queue.

### Provenance instead of confidence

Notes record `origin: user | agent | tool | import | shared`.

A numeric confidence score was considered and rejected: nothing in the system can
calibrate it, so it would be a number that looks like information and is not.
Where a note came from is checkable.

### Supersession instead of deletion

```markdown
status: superseded
supersedes: previous-note-id
superseded_by: newer-note-id
```

The predecessor is kept, marked, and linked in both directions. Default search
filters on `status = 'active'`; `--all` includes the rest.

The alternative — overwriting the note — destroys the most useful thing in the
file: *what changed and why*. Keeping the chain costs a few kilobytes and makes
the history of a decision readable.

## 4. Index design

```sql
CREATE TABLE notes (id, path, title, type, scope, tags, status, origin,
                    detail, supersedes, superseded_by, captured, updated,
                    mtime, hash);

CREATE VIRTUAL TABLE notes_fts USING fts5(
  id UNINDEXED, title, body, tags,
  tokenize = "unicode61 remove_diacritics 2");

CREATE VIRTUAL TABLE notes_tri USING fts5(
  id UNINDEXED, path, title, body, tokenize = "trigram");
```

**Two FTS tables because there are two different retrieval needs.**

`notes_fts` handles words, with diacritics removed so a query without accents
matches text with them. `notes_tri` handles substrings that tokenisation
destroys — a Windows path, a hyphenated package name, an identifier — which a
word tokenizer splits into pieces that no longer match the whole.

**Fusion by Reciprocal Rank Fusion**, not by summing scores:

```
score(note) = Σ  1 / (k + rank_in_list)     k = 60
```

BM25 scores from two different indexes are not comparable, so combining them
directly requires normalisation that has to be tuned. RRF only uses *rank*, so
it needs no calibration and cannot be dominated by one index's scale.

**Snippets use `snippet(notes_fts, -1, ...)`.** Column `-1` lets FTS5 pick the
best-matching column. Hardcoding the body column returns `NULL` whenever the hit
was in the title, which silently degrades every result to its title.

### Two-stage retrieval

**Stage 1 — recall.** Both rankers contribute up to `RECALL = 60` candidates,
fused with RRF. The FTS query is an OR of prefix-matched tokens, which is
deliberately recall-oriented: precision is not this stage's job.

**Stage 2 — precision.** Candidates are re-ordered by how many *distinct query
terms* they contain per unit of length, with the fused score as the tie-break.

Stage 2 exists because BM25 rewards a rare term heavily but does not reward
matching *several* terms. Without it, a long note repeating one common word
outranks a short note that answers the whole question. Measured effect, on the
ground truth that has since been superseded: R@5 went from 0.600 to 0.667 — one
case in fifteen, which is why it was measured rather than assumed.

Counting terms alone was only half the fix, and the other half was found much
later. *Presence* cannot distinguish a note that answers a question from a
transcript that contains everything: a raw session (median 21 160 B) holds every
query term simply by being long, so it beat the 2 321 B note that actually
answered. The score is now

    terms_matched / (1 + log2(max(1, size / COVERAGE_FREE_BYTES)))

— full credit at or below `COVERAGE_FREE_BYTES`, logarithmic above it. Log rather
than linear for the same reason BM25 saturates: the tenth repetition of a word
adds nothing. The penalty is *relative*, so it cannot hide a genuinely relevant
long document — a 35 kB register that really does match more of the question than
anything else still wins. It only stops length from being mistaken for relevance.
See §7 for the before/after numbers and how the constant was chosen.

Matching in stage 2 is prefix-tolerant (`term[:max(4, len-2)]`), so "derived"
still credits "derive". That is a cheap stand-in for a stemmer, which FTS5 does
not ship.

**Query construction matters in two non-obvious ways:**

- **Stopwords are dropped before the OR is built.** Because the query is an OR,
  every function word widens the candidate pool with noise. The list carries both
  English and Polish words, since the store is multilingual by design.
- **The trigram query ORs the longer tokens as individual quoted phrases.** The
  obvious reading — quoting the entire query as one phrase — matches nothing for
  any real question. Per-token quoting is what makes the trigram index useful:
  `ONSOLE_FILL` returns 0 hits from FTS and 4 from the trigram index.

Note the asymmetry: the stopword and trigram changes moved R@5 by *zero*
(0.600 → 0.600, on the retired ground truth — see §7) and were kept anyway on
separate, direct evidence. See §7.

## 5. Concurrency

Two mechanisms, each solving a different failure:

- **Atomic publish** (`tempfile.mkstemp` in the target directory + `os.replace`)
  prevents *torn files*. A reader never sees a half-written note.
- **An OS advisory lock** prevents *lost updates*. Atomic writes do not help
  when two clients read the same note and both write back: the second write
  silently wins and the first is gone.

The lock design went through three iterations, and the failures are instructive:

| Approach | Why it failed |
|---|---|
| Lock file as an existence marker (`O_CREAT\|O_EXCL`), reclaim if older than 90s | A crashed writer wedged the store. Worse, `stale_after=90s` with `timeout=15s` made the recovery branch **unreachable** — the process gave up long before the lock was considered stale. And deleting a marker file is itself a race: another process may be blocked on it, and you end up with two writers on two different inodes. |
| OS advisory lock, plus writing the holder's PID into the lock file | Writing to the file being byte-range locked fails with `EACCES` on Windows if anyone already holds byte 0. Intermittent: roughly two failures in six runs, a different line each time. |
| **OS advisory lock, file opened and never written to** | Correct. The kernel releases the lock when the process dies, for any reason including `SIGKILL`, so no staleness heuristic is needed. |

The lock is also **re-entrant within a process**, tracked by a depth counter.
Without that, `append_to()` — which takes the lock and then calls `save()`, which
takes it again — would wait for a lock it is itself holding and die on timeout.

The `.lock` file persists on disk forever. That is intentional.

## 6. Code map

`anatomy.py` scans a tree into two things: a **symbol index** (`name`, `kind`,
`file`, `line`, `signature`) and a **rendered map** stored as an `anatomy` note.

Symbols are extracted with per-language regexes for Python, JS/TS, Kotlin, Java,
Go, Rust, and C#. This is explicitly a trade: less precise than an AST, but it
needs no parser dependency and every result is verifiable at a `file:line`.

Files are ranked by **how many other files import them** — a crude PageRank that
is cheap to compute and a reasonable proxy for "what should I read first".

The payoff is `agtmem find <symbol>`: one line of output, no file read. Reading
whole files to find a function is the most common way an agent burns its context
window, and this replaces it.

## 7. Measuring

Two instruments, both necessary:

**`agtmem eval`** reports R@5 and P@5 for `agtmem` *and* for a plain grep over
the same files. A retrieval number without a baseline is uninterpretable — grep
is fast, free, and often surprisingly good, so the only honest question is
whether this beats it.

**`agtmem stats --usage`** reports how many tokens each retrieval injected. A
memory system that costs more context than it saves is a net loss, and you cannot
know which you have without counting.

Token counts are a `len(text) // 4` estimate, labelled as an estimate. It is
never presented as a measured provider count.

### What the numbers actually said

Measured on a 275-note store (183 distilled notes plus 92 raw transcripts), 20
scored cases, ground truth established by grepping the corpus for a distinctive
phrase rather than by reading search results:

| | R@5 | P@5 |
|---|---|---|
| `agtmem` | 1.00 | 0.21 |
| grep | 0.05 | 0.01 |

P@5 is bounded by the fact that most questions have exactly one right answer, so
0.2 is the ceiling; it is reported because a metric that can only rise is not a
metric.

**The two-layer split is the real finding.** An agent only ever reads distilled
notes, so that is the layer the headline number covers — but the same search can
be pointed at the raw transcripts with `--sessions`, and there the ranking used
to collapse. A session has a median size of 21 160 B against 2 321 B for a note,
so it contains every query term by construction and won on length:

| with sessions in the pool | before | after the length penalty |
|---|---|---|
| correct note pushed out of the top 5 | 15/20 | 0/20 |
| top result was a raw transcript | 17/20 | 0/20 |

`COVERAGE_FREE_BYTES` was chosen from that sweep, not from intuition. The first
value tried was the median note size (2 500 B), and it *lowered* knowledge-layer
R@5 from 1.00 to 0.90 — because the notes that answer questions are the
substantial ones, with a median of 3 488 B and a p99 near 6 000 B.

There is a second constraint, and it was found the hard way. The penalty is one
knob with two requirements pulling against each other:

- **Weak enough that better coverage still wins.** At 5 000 B the discount was
  strong enough that a 4.2 kB note matching *four* query terms outranked a 6.5 kB
  note matching *five*. That is the same class of mistake as the length bias the
  penalty exists to fix, only smaller: an artefact of size overriding evidence.
- **Strong enough that a transcript still loses.** A raw session (median
  21 160 B) matches nearly every term by construction.

Sweeping all three checks at once — knowledge-layer R@5, correct-note survival
with sessions in the pool, and "does the note with more coverage win" — puts the
usable window at **5 500–6 500 B**, with 6 000 in the middle. Below it the
coverage check fails; above it a transcript starts winning again. The window is
narrow, and that is a fair criticism of a single-parameter curve: the honest
statement is that this value is tuned to a measured corpus, not derived. If your
notes are much larger or much smaller than 2–6 kB, re-measure it.

Note also what did *not* work, because it looks like the obvious fix: sorting
lexicographically by term count first and length second. It fixes the coverage
case and immediately re-breaks the transcript case (correct notes pushed out of
the top 5 went from 0/20 back to 12/20), because a transcript matches *more*
distinct terms than a note does. The two signals have to be combined in one
score, not ranked in sequence.

An earlier version of this section reported R@5 = 0.667, with a gap between
term-style queries (0.867) and natural-language ones (0.667). **Those numbers are
superseded and were measured against ground truth that pointed at raw
transcripts** — which is why the gap appeared at all: a transcript of everything
is precisely the document a keyword query finds and a paraphrase misses.
Re-measured on corrected ground truth, both phrasings score 1.00. Four
alternative lexical strategies were tried against the older set (proximity
`NEAR`, coverage as a multiplier, title weighting, AND-first) and none beat
coverage-first re-ranking; that conclusion still stands on the design rationale
rather than on the retired numbers.

**The current set is at ceiling, which is its own limitation.** R@5 = 1.00 means
it can no longer tell two good strategies apart, and the questions were written
from each note's own vocabulary, so a paraphrase sharing no words with the note
remains untested. Paraphrase robustness is what embeddings buy, and this project
deliberately does not put a model on the hot path. The ceiling is structural.

Three methodological rules this exercise produced:

1. **Ground truth must be independent of the thing being measured.** Deriving
   expected ids from `agtmem search` would have produced a meaningless 1.0.
2. **Ground truth must point at something search can return.** Two ways to write
   a case that fails for the wrong reason: a raw session (excluded from search by
   default) and a `superseded` note (hidden by default). Four of the first
   draft's targets were superseded and had to be re-pointed at their successors.
   `agtmem eval --add` now refuses both instead of writing the case.
3. **A gap is not a miss.** A question no note answers is a distillation defect,
   not a ranking defect, and averaging the two together produces a number that
   describes neither. Gaps are marked with `!` and scored separately.

Historical note on rule 2 in the older set: filtering stopwords and rebuilding
the trigram query moved R@5 by *zero* (0.600 → 0.600), and the coverage
re-ranking moved it by +0.067 — both measured on the retired ground truth. The
trigram fix was kept anyway, on separate evidence: mid-token fragments like
`ONSOLE_FILL` return 0 hits from FTS and 4 from the trigram index, and 0 from the
old whole-query phrase. Principled changes can be worth keeping even when the
aggregate metric does not move — but you have to know that is what you are doing.

## 8. Bugs found only at runtime

All four of these were invisible in the design and appeared within the first
hour of use. They are documented because each is a trap that is easy to walk into
again.

1. **A non-re-entrant lock deadlocks against itself.** `append_to()` takes the
   lock, then calls `save()`, which takes it again. With an `O_EXCL` marker the
   second acquisition waits on a lock the same process holds. `add` worked,
   `append` was broken — so it looked fine.

2. **Six copies of the same block means one is wrong.** See Rule 4. The defect
   was in the copy, but the cause was the duplication.

3. **`stale_after > timeout` makes recovery unreachable.** A leaked lock
   guaranteed failure for 90 seconds because the process gave up after 15.

4. **Never write to the file you are byte-range locking.** On Windows, if
   another process holds byte 0, the *write* fails with `EACCES`. The symptom was
   intermittent and moved between lines, which is what made it expensive.

**Methodological note.** Three hypotheses were wrong before the fourth was
right, and the thing that found it was the **full traceback**. The test had
truncated stderr to 300 characters, cutting off exactly the line that mattered.
Do not truncate tracebacks in tests.

## 9. Module map

```
agtmem/store.py       the contract: Markdown + frontmatter, lock, atomic write
agtmem/index.py       the cache: SQLite FTS5 + trigram, RRF, supersession, usage
agtmem/anatomy.py     code map and symbol extraction (regex, no tree-sitter)
agtmem/ingest.py      harvests context-compaction summaries (read-only input)
agtmem/eval.py        R@5 / P@5 against a grep baseline
agtmem/cli.py         command line surface
agtmem/mcp_server.py  JSON-RPC over stdio, eight tools
```

Dependencies point one way: `cli` and `mcp_server` depend on `store`, `index`,
`anatomy`, `ingest`, `eval`. Nothing depends on `cli` or `mcp_server`. `store`
depends on nothing.

## 10. What was deliberately excluded

- **Embeddings / vector search.** The high-frequency-path argument in §1.
- **LLM calls in the store.** Rule 2.
- **A graph store.** The supersession chain plus `scope` and `tags` covers the
  relations that actually get used. A graph would need a query language and would
  make the files less readable by hand.
- **Hooks into the agent loop.** That is the agent's job; this is a store.
- **Cloud sync.** It is a folder. Use whatever sync you already have.

## 11. Known gaps

Stated plainly, because a design document that only lists strengths is marketing:

- **The ceiling is lexical, and the eval can no longer see it.** R@5 = 1.00 on
  the current 20 cases, against a 0.05 grep baseline. That number is at ceiling,
  so it cannot discriminate two good strategies, and it says nothing about
  queries that paraphrase a note without sharing its vocabulary. Four alternative
  lexical strategies were measured against the retired set without closing that
  gap. See §7.
- **The eval set is small and partly self-fulfilling.** 20 scored cases, two of
  which are known coverage gaps, and the questions were written from each note's
  own vocabulary. Enough to falsify a claim and to catch a ranking regression,
  not enough to trust a third decimal place.
- **The ground truth was wrong once and nothing caught it.** It pointed at raw
  transcripts for a day, which made the metric measure the wrong layer and then
  read 0.0 the moment sessions were excluded. The `--add` guard now rejects the
  two structural mistakes, but a wrong-but-valid target is still possible.
- **No distillation pipeline.** Imported session summaries are raw. Converting
  them into `decisions` and `bugs` is agent work, and there is no automation for
  it.
- **Consolidation can leave a paragraph in the wrong note.** A duplicated
  sentence in an off-topic note was found while auditing the eval set; it was
  ranking first for a question it does not answer. Nothing in the tool detects
  this — the duplicate-body check hashes whole files, so a single shared
  paragraph passes.
- **Symbol extraction is regex-based** and will miss generated or dynamic
  symbols.
- **Single-writer performance.** The lock serialises writes across processes.
  For an interactive agent that is irrelevant; for a bulk import it is the
  bottleneck.
