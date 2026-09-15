# Credits and prior art

`agtmem` is not an original idea. It is a deliberate synthesis of ideas that
other people worked out first, plus a few decisions about what to leave out.

This file exists because "inspired by the open-source community" tells you
nothing. Below is what specifically came from where.

**No code was copied.** Every line here is written from scratch against the
Python standard library. What was taken is architecture, data layout, and
lessons — none of which are copyrightable, which is exactly why the honest thing
is a specific list rather than a vague acknowledgement.

---

## [OpenWolf](https://github.com/cytostack/openwolf) — AGPL-3.0-only

A TypeScript agent-memory layer built around a `.wolf/` directory containing
`anatomy.md`, `cerebrum.md`, `buglog.json`, and `handoff/`, driven by twelve
lifecycle hooks including `precompact`.

**Taken:**

- The idea that a **codebase anatomy map is a first-class memory artifact**,
  not a derived cache. This became the `anatomy/` note type and the
  `agtmem anatomy` / `agtmem map` / `agtmem find` commands.
- A **dedicated bug log with structured fields**. OpenWolf keeps
  `buglog.json`; this keeps `bugs/` notes with fixed `## Symptom` / `## Cause` /
  `## Fix` headings, so the same shape is greppable and comparable across
  entries.
- Treating **context compaction as a hook point worth designing around** rather
  than an implementation detail.

**Not taken:** the hook system. `agtmem` has no hooks, because it does not sit
inside the agent loop — it is a store the agent calls into.

### On the licence

OpenWolf is AGPL-3.0-only, which is a strong copyleft licence. Because no code
was copied, that licence does not propagate to this project, and `agtmem` is
MIT. If you are the OpenWolf author and disagree with how your ideas are
represented here, open an issue and it will be corrected.

## [agentmemory](https://github.com/rohitg00/agentmemory)

A TypeScript memory system that publishes retrieval metrics and takes an
explicit position on how memory should be versioned.

**Taken:**

- **Supersession with a version chain** instead of deletion: a replaced note
  keeps existing, gets `status: superseded`, and carries forward and backward
  pointers. This is now the central data-model decision here.
- **Provenance over confidence.** Rather than storing a numeric confidence
  score that nothing can calibrate, notes record `origin: user | agent | tool |
  import | shared`. Where a fact came from is checkable; a confidence float is
  not.
- **Publishing retrieval metrics against a baseline.** agentmemory reports R@5
  with a comparison; `agtmem eval` does the same against a plain grep over the
  same files. Without a baseline a retrieval number is uninterpretable.
- The reminder that **what an agent reads costs more than what it retrieves** —
  hence the `usage` table and `agtmem stats --usage`.

## [memanto](https://github.com/memanto-dev/memanto) / Moorcheh

An MCP memory agent backed by a hosted vector store, which this project was
originally going to use and then deliberately rejected.

**Taken:**

- The **shape of the MCP tool surface** — search / read / write / append /
  index — which is a sensible decomposition and is reused here with different
  internals.
- The **namespace concept**, reworked into a plain `scope` frontmatter field.
  Scopes are just strings, so no server has to know about them.

**The lesson, which is the real debt:** a hosted memory backend is a lock-in
trap. The free tier caps the number of namespaces, the cap is a *storage*
property, so changing your LLM provider does not lift it, and migrating out
means rewriting everything that accumulated. `agtmem` exists largely as the
response to that failure mode. Its "delete the index and lose nothing" rule is a
direct answer to it.

## [Basic Memory](https://github.com/basicmachines-co/basic-memory)

Markdown files as the store, with an index over them.

**Taken:**

- The core **"plain files are the source of truth, the index is rebuildable"**
  idea. This is the single most important borrowed idea in the project.
- The observation that pointing a tool at an existing notes folder must **index
  in place, not copy or duplicate** — the store format here is deliberately
  boring enough to adopt an existing folder of Markdown.

## Context compaction in Claude-Code-style agents

Claude Code and agents built on the same pattern compact their context window
when it fills, and the model writes a structured summary that is then **thrown
away** — it exists only to continue the current task.

**Taken:**

- The `ingest-sessions` idea: **harvest summaries that already exist** rather
  than asking a model to write new ones. That summary was generated anyway; it
  is free; it contains the task state at a point in time.
- The recognition of its limits, which is why imported notes are typed
  `session` with `origin: import` and are explicitly **raw material, not curated
  memory**. A compaction summary optimises for continuing a task and drops
  durable facts, so it is an input to distillation, not a substitute for it.

## Standard techniques, not invented here

- **SQLite FTS5**, including the `unicode61 remove_diacritics=2` tokenizer and
  the `trigram` tokenizer, plus `bm25()` ranking. Built into SQLite.
- **Reciprocal Rank Fusion** for merging result lists from different rankers:
  Cormack, Clarke & Buettcher, *Reciprocal Rank Fusion Outperforms Condorcet and
  Individual Rank Learning Methods*, SIGIR 2009.
- **`mkstemp` + `os.replace`** as the portable atomic-write idiom, and
  **`flock` / `LockFile`** as the portable advisory-lock idiom.

## The general shape

The framing that "the store is the contract, the index is a cache, and access
is a plug-in" circulates widely in the agent-tooling space and is not
attributable to any one project. It is stated here as a design rule rather than
claimed as an invention.

---

## How this was built

Designed and written in collaboration between a human (Milosz) and an AI coding
agent. The human set direction and made the consequential decisions; the agent
wrote essentially all of the code, the tests, and the documentation.

See the "How this was built" section of the [README](README.md#how-this-was-built)
for the longer version, including why the LICENSE names a human copyright holder.
