# WorkBuddy integration — inject store hits into the prompt

`agtmem` is a store. It deliberately does not sit in the agent loop, and it has
no hooks of its own (`docs/ARCHITECTURE.md`). Reading the store at the right
moment is the *consumer's* job — and this directory is one consumer's answer.

WorkBuddy spawns the CodeBuddy CLI, and that CLI has a hook system. Two events
are enough:

| Event | What it does |
| --- | --- |
| `UserPromptSubmit` | searches the store with the prompt's content words and appends the best hits to the context, before the model sees the prompt |
| `SessionStart` | a one-line reminder that the store exists, once per session |

The result is that a question about *why* — a decision, a bug's cause, something
settled in an earlier session — arrives with the relevant note already attached,
without the agent deciding to go looking.

## What gets injected, and why it is content rather than a pointer

Each hit arrives with **the note's own content** — its opening prose, then every
later heading with the line beneath it — not just its title:

```
[agtmem] 3 trafienia w pamięci projektu (pełna treść: `agtmem show <id>`):
- fact/firebase-identity-and-rules-model · 2026-09-15 — Tożsamość i reguły w Verbigem:…
  Model tożsamości i reguł w Verbigem. Pięć rzeczy, które wracają jako pytania.
  1. Wszystko żyje w projekcie `mini-verbigem`
  Nazwa projektu Firebase: **`mini-verbigem`** (nie `verbigem-app-7k2`). …
  § 2. „Ten sam e-mail ma dwa uid" to nie dziura — to dwa projekty Firebase
    Firebase Auth jest **per projekt**. …
  § 3. Po co są dwie kolekcje: `users` i `usersPublic`
    `users/{uid}` jest **owner-read-only** (`request.auth.uid == uid`) …
  § Lista pól, których klient nie może zapisać w `users/{uid}`
    Reguła `update` używa `affectedKeys().hasAny([...])`:
  § 4. Podkolekcje NIE kaskadują
    Usunięcie `users/{uid}` **nie** usuwa podkolekcji. Zostają:
```

The first version of this hook injected `- fact/<id> — <title>` plus a
110-character snippet. That is a **pointer**: it tells the agent a note exists
and asks it to run `agtmem show <id>`. Over one measured day, **none of the nine
injections was followed by a `show`** — the decision never happened, so every
note was delivered and never read. That is the original failure this project
exists to fix, merely moved one step later.

The second version carried the note's **head** — seven lines of prose — on the
reasoning that by the store's own convention the first section is the essence.
That failed the same way, one level down, and it failed *silently*: the block
held the note's id, its title and its first two sections, and none of its answer,
because `affectedKeys().hasAny([...])` sits at character 2 046 of a 3 289-character
body, under a heading in section 3. The agent read the block, did not find the
answer, searched the repository instead and answered `allow write: if false;`.
A block that carries a note's name but not its answer looks exactly like a
healthy injection.

So the excerpt is the head, **then every later heading with its lead line**. A
heading names its section in a few words; that is what tells a reader whether the
rest is worth fetching. Measured on the same five questions:

| excerpt rule | cap needed to reach every answer | block then |
| --- | --- | --- |
| head, 7 lines (before) | never | 2 541 chars |
| head 2 100 chars, flat | 7 000 | 6 115 chars |
| **head 200 + section leads** | **2 800** | **2 794 chars** |

The structural rule reaches the answer at less than half the price of a flat
raise, and at a *lower* median block than the rule it replaces — 2 311 characters
against 2 356 — while answering **5/5 instead of 4/5**, with junk prompts still
at 0/14. That is why it is not a budget increase: on the ~27 % of prompts where
the notes answer nothing, a bigger flat budget is pure cost, and this one is not.

Notes cannot be injected whole (median ~2.5 kB, max 96 kB). `search --json`
returns a `path` but **not the body**, so the hook reads the file itself — one
local read per injected note, no second search. Excerpts are indented, which is
what keeps a Markdown bullet inside a note from being mistaken for a note header.

| | pointer version | head only | **head + section leads** |
| --- | --- | --- | --- |
| per note | ~30 tokens | ~180 tokens | ~330 tokens |
| whole block | 105–284 tokens | ~250–520 tokens | **~640 tokens** |
| one `agtmem search --json` it replaces | 1234 tokens | 1234 tokens | 1234 tokens |

The cost is real and the trade is deliberate: a day of 33 prompts costs roughly
20 k tokens of injected context, against 474 k tokens spent that same day on
*maintaining* the store. The measurement lives in
`~/.workbuddy-ai/skills/workbuddy-hooks/`.

## Install

```bash
python install_hooks.py --status      # what is wired up, and where
python install_hooks.py               # write every settings file that exists
python install_hooks.py --dry-run     # show, write nothing
python install_hooks.py --uninstall   # remove our entries
```

The installer resolves the interpreter and the script path from
`sys.executable` and `__file__`, merges its entries into `settings.json`
**without clobbering foreign keys or foreign hooks**, and is idempotent. It
records what cannot be derived — the store, the checkout, the runtime directory —
in `agtmem_inject.config.json` beside the script, which is gitignored.

Nothing machine-specific is ever written into the source. A test enforces that:
`test_agtmem_inject.py` fails if the shipped code contains a Windows user
directory, a POSIX home directory, or a `.workbuddy-ai` path.

**A hook added mid-conversation does not fire.** Settings are read when the CLI
process starts, and the desktop keeps an idle prewarm pool of workers — a worker
started before your edit ignores the hook *silently*. Open a new conversation, or
restart the app. Editing the hook *script* needs no restart: it runs fresh on
every event.

### Which settings file

The CLI resolves its user scope from a product-name constant, and the value of
`WORKBUDDY_CONFIG_DIR` depends on who is spawning it. Rather than reason about
it, register the hook in every candidate with a distinct label:

```
~/.workbuddy/settings.json     →  … agtmem_inject.py --src=workbuddy
~/.codebuddy/settings.json     →  … agtmem_inject.py --src=codebuddy
~/.workbuddy-ai/settings.json  →  … agtmem_inject.py --src=workbuddy-ai
```

The log then says which one fired. On the machine this was developed on the
answer was `workbuddy-ai` — the agent runtime points the variable at its own home,
which is *not* the directory the desktop uses for plugin storage. The other two
registrations are inert and left in place as cheap insurance.

Do not trust `WORKBUDDY_CONFIG_DIR` first in an installer: in a shell it resolves
to a different product's home, whose `settings.json` holds unrelated keys.

## The gate, and why it is tuned the way it is

Injecting an irrelevant note is worse than injecting nothing — the model treats
injected text as authoritative. So the gate is tuned for **precision over
recall**: a miss costs nothing, because the agent can still run `agtmem search`
itself.

`score` from the search is not usable as a gate at all. On a labelled set of
relevant and junk prompts it overlaps completely (relevant 0.0307–0.0328, junk
0.0325–0.0328). The usable signal is `terms` — how many distinct query terms the
note contains — after the query has been reduced to content words.

Current behaviour on the labelled set in `test_agtmem_inject.py`: **5/5 relevant
prompts inject, 0/14 junk prompts do not.** Two relevant prompts are pinned as
`KNOWN_MISS` — they fall below the gate, and the test asserts they stay silent, so
the trade-off is documented rather than hidden.

Three findings came out of running it against a real store, each of which changed
the code:

1. **A threshold scaled to the whole prompt kills long prompts.** `need = max(3,
   0.3 × content_words)` demanded 111 matched terms for a 367-word prompt, which
   no note can have — so long prompts were *silently never injected*. The
   denominator is the query actually sent.
2. **Function words buy matches.** `jego` appears in nearly every Polish note, so
   counting it as content gave irrelevant notes a free term. The pronoun family is
   now in `STOPWORDS`.
3. **Short tokens match inside identifiers.** The index uses trigram matching, so
   `doc` matched `documentId` and `hook` matched `paddleWebhook`. `query_words()`
   now requires four characters — measured as the boundary, not chosen: at five, a
   relevant prompt is lost.

## Known limitation

The gate is not a relevance oracle. Ask a **meta-question about the session
itself** — "did you push that to the repo?" — and the store has no answer; it
returns its best lexical matches anyway, and some of them will be nonsense. The
fixes above reduced the noise but cannot remove the class: no threshold can tell
"this note is lexically close" from "this note answers the question".

The signal that would close it is **document frequency** or **match kind**
(whole-word vs substring) exposed per hit in `search --json`. A consumer could
then require at least one rare term matched as a whole word. Until then, treat an
injected block as a *pointer*, not as an answer — `agtmem show <id>` before
relying on it.

### A second limitation, and it is the one that bites in practice

**This hook is downstream of the distillation pipeline, so its usefulness can
decay without a single failed event.** It searches the *distilled* layer — raw
sessions are excluded from default search — which means the hook cannot inject
anything that has not been harvested and distilled first. Measured over one day:
nine sessions of real work sat unharvested while the hook kept firing and kept
logging healthy lines. **A working hook is not evidence that its source is
current.** Check the upstream feed before evaluating the hook:

```bash
python -m agtmem ingest-sessions --dry-run   # the only honest backlog count
```

The same day also showed an inverted selection effect worth knowing before tuning
the gate: the maintenance job's prompt is built from the store's own vocabulary,
so it is the conversation that gets an injection **most reliably** (4 of 4 runs) —
and the injection is worthless there. Lexical proximity is anti-correlated with
"needs recall" in exactly the two cases that matter most.

## Runtime files

The log and the suppression state are **not** written beside this script. The
script belongs in a checkout under version control, and a hook that appends a log
line next to itself makes `git status` dirty on every prompt. They live in the
runtime directory instead — `~/.workbuddy/hooks` by default, recorded in the
sidecar, overridable with `AGTMEM_HOOK_RUNTIME`.

| File | Purpose |
| --- | --- |
| `agtmem-inject.log` | one line per event: the query, the gate decision, the session, the ids injected |
| `agtmem-inject-state.json` | repeat suppression, keyed per conversation |

The log is the only way to see what the hook decided, because its stdout goes
into the conversation context rather than to a terminal. Read it as a funnel:

```
18:03:40  query='trochę pracowałem wiecej danych oceny systemu' cw=6/7 need=3 rows=6 kept=0
18:03:40  silent event=UserPromptSubmit prompt='trochę już pracowałem, masz wiecej danych…'
10:12:58  query='wypchnąłeś hook jego opis doc repo' cw=6 need=3 rows=6 kept=6
10:12:58  INJECT event=UserPromptSubmit 2007c ids=workbuddy-prompt-hook-injects-agtmem-hits,… prompt=…
```

`cw=` is content words used out of words seen, `need=` the gate, `rows=` what search
returned, `kept=` what survived the gate. The `INJECT` line names the **ids actually
emitted** and the byte count — `kept` is not the id set, because the character budget
can still drop a candidate, and without the ids the question "was the right note
delivered?" becomes unanswerable within `REPEAT_WINDOW` (45 min), when the suppression
state that would have named them is pruned.

A silent downgrade to snippets would look exactly like a healthy injection, only ~6×
smaller — so when an excerpt cannot be read, the hook says so:
`excerpt unavailable for 1/2 notes — snippet fallback`.

Suppression is **per conversation**. It used to be global, which was a real bug:
a note injected while answering one question was silenced when it was the right
answer again in a *different* conversation, and a weaker note was injected in its
place. A flat state file from the old schema is migrated on load and still
honoured, so upgrading does not re-inject everything once.

## Configuration

Environment beats sidecar beats default:

| Variable | Meaning | Default |
| --- | --- | --- |
| `AGTMEM_PYTHON` | interpreter to run `agtmem` with | the interpreter running the hook |
| `AGTMEM_REPO` | checkout to run it from | whatever the import resolves to |
| `AGTMEM_HOME` | store location | agtmem's own default |
| `AGTMEM_HOOK_RUNTIME` | log + state directory | `~/.workbuddy/hooks` |

`repo` and `store` are deliberately allowed to be absent — that means "let agtmem
decide", which is the right answer for an installed package.

One sidecar key is not a path: `"cite"` (default `true`) controls whether the injected
block asks the model to mark the notes it used. See the section below.

## Does it work? Two instruments, two questions

### The canary: does an injected note change the answer?

Plant a fact that cannot be guessed, ask for it, and see whether it comes back. This is the
only measurement here with an unambiguous reading.

```bash
python canary_test.py --vague               # the production shape: thin follow-up + transcript
python canary_test.py --vague --diagnose    # no model calls: where the note lands
python canary_test.py --vague --no-context  # the behaviour before 2026-09-16
```

It clones the store into a temp directory, plants canary notes through the CLI, and runs
three arms per canary:

| arm | what it is | must read |
| --- | --- | --- |
| A | prompt + the block the hook would inject, wrapped as production wraps it | the canary |
| B | prompt alone — **the null** | zero |
| C | prompt + the block for a *different* canary | zero |

Arm B reading non-zero means the canary is guessable and the run is void; arm C reading
non-zero means the model filled the slot with something plausible. Either one invalidates
the run, and the script says so instead of reporting a number.

**Measured 2026-09-16, five canaries, two models (`openai/gpt-4o-mini` and
`deepseek/deepseek-chat`):**

| prompt shape | hook injected | arm A | arm B | arm C | cited |
| --- | --- | --- | --- | --- | --- |
| names the topic | 5/5 | 5/5 | 0/5 | 0/5 | 5/5 |
| thin follow-up, no transcript | **0/5** | — | — | — | — |
| thin follow-up, with transcript | 5/5 | 5/5 | 0/5 | 0/5 | 4/5 |

So the pipe works: given a note that answers the question, the block puts it in the answer,
and the value could not have come from anywhere else. The cite instruction works too — 5/5
here against **0 of 42** in production, which is the first evidence that production's zero
was about the *notes* being useless, not about the model ignoring instructions.

### Why the thin follow-up injected nothing

The hook searched on the prompt alone. A production prompt is often three words — "działaj",
"rób licznik", "co musimy zrobić żeby działało?" — and three words retrieve notes that share
those three words and answer nothing. Measured: **0 of 5**.

Every `UserPromptSubmit` payload has carried `transcript_path` all along and the hook never
looked at it. It does now, and the conversation is a **fallback, never a replacement**:

1. search on the prompt and gate as before;
2. only if that gates nothing in, search again on the prompt's terms plus the terms of the
   last few messages.

That order is deliberate. Re-ranking every prompt on conversation terms would let a note
that merely echoes the last few minutes outrank the note that answers the question, so a
prompt that already works keeps its own query. The fallback triggers on the *gate*, not on
prompt length, because a long prompt can gate nothing in too.

Cost: one search normally, two only in the case that used to fail silently.

### The counter: a weaker question, answered honestly

`measure_usage.py` counts whether the notes the hook injected show up in the answer:

```bash
python measure_usage.py --day 2026-09-16 --verbose --reconstruct
```

It joins three sources — the hook log (`ids=`, `sess=`), the store's index (document
frequency) and the transcripts — and reports two arms.

**Arm 1 — declared use.** The injected block asks for a marker:

```
[agtmem] 2 trafienia w pamięci projektu (pełna treść: `agtmem show <id>`):
  Jeśli z którejś korzystasz, dopisz na końcu odpowiedzi `[agtmem:<id>]`.
```

The counter counts those markers. This is the only signal that means what it says: the
model states which note it relied on. It sits second in the block, not last, because the
block is truncated line by line and an instruction that vanishes whenever the notes are
long would make the counter read zero and look like model indifference. Turn it off with
`"cite": false` in the sidecar — the block then returns to exactly what it was.

**Arm 2 — lexical trace, with two nulls.** For each injection, take the terms of the
injected excerpt that the model *could only have learned from the note* — distinctive in
the store (present in ≤ `--df-max` notes), absent from the prompt, absent from everything
said earlier in the conversation — and check whether they appear in the answer. Then run
the same test twice more, against the same answer text:

* **near control** — notes the *same query* gated in and did **not** inject. Same topic,
  same vocabulary profile; the only difference is that they never reached the model. This
  is the arm that matters, because it is the only one that can separate "the note arrived"
  from "this store always sounds like this".
* **far control** — random notes from the store. It cannot isolate anything, and is
  printed precisely to show why: this store is one topic, so even an unrelated note
  overlaps the answer.

Both controls are counted **per injection**, the same unit as the measurement. Comparing a
per-injection rate against a per-note rate flatters the measurement purely by giving it
more chances to hit — which is what the first version of this counter did.

**Measured on 2026-09-16, the first day the hook worked** (17 injections whose notes could
be identified, 42 injected notes):

| arm | measured | control | reading |
| --- | --- | --- | --- |
| 1 — declared `[agtmem:<id>]` | 0 / 42 notes | binary | production only; 5/5 on the canary, so the zero is about the notes |
| 2 — exact match | 0 / 6 | 3–6% | no power — the model paraphrases, it does not reuse rare words |
| 2 — six-character stem | 10 / 17 (59%) | near **57%**, far 50% | **no separation — the instrument is seeing coincidence** |

The honest reading of that last row: **a lexical trace cannot show that a note was read.**
The near control reads 57% against a 59% measurement, and a control at more than half the
measurement is not a null, it is a second measurement. The reason is structural rather than
a tuning problem: notes the query gated in are *about the same subject as the answer*, so
they share its vocabulary whether or not they were injected.

So the counter is a **regression alarm**, not a proof of value. When you want to know
whether the integration works, run the canary. The counter exists to notice the day the
canary stops working.

Arm 1's zero is not the same kind of zero as arm 2's. The block demonstrably reaches the
model (it arrives as a `<system-reminder data-role="hook">` block ahead of the prompt), the
instruction is in it, and in production the marker was never once written. The canary then
showed the same instruction being honoured 5 times out of 5 — so the difference is not the
instruction, it is whether there was anything in the block worth citing.

Two limits to state plainly. It only sees injections made after `sess=` was added
(earlier lines are resolved by prompt text and marked `~` as guesses), and injected
context is **not** persisted in the transcript — which is exactly why the hook logs what
it sent. `--reconstruct` replays the hook's own selection to recover `ids=` for lines
written before the hook logged them; it self-checks against the lines that do carry ids
and currently reproduces 3 of 9 exactly, so treat a reconstructed set as an upper bound.
It also has to be able to see the note at all, so `--reconstruct` is only as good as the
store's current index against a prompt from hours ago.

## What does it cost, and what does it save?

The canary answers "does the note reach the answer". It does not answer the question
the integration exists for: an agent should not spend time and tokens rediscovering
what is already written down. That is a claim about cost, and `savings_test.py` is
the counterfactual:

```bash
python savings_test.py --model openai/gpt-4o-mini
```

Same question, same tools, same system prompt, two arms:

* **arm A** — the prompt plus the block the hook would inject
* **arm B** — the prompt alone

Arm B gets `grep`, `read` and `list_dir` over the real repository, so it can go and
look. Every tool is workspace-confined and read-only. Token counts come from the
API's own `usage` counters summed over every turn, because a tool result makes the
*next* prompt bigger and that growth is half the cost being measured.

The workspace is `~/verbigem`: **27 GB, 33 186 files**, and for the questions whose
answer is history the fact sits inside a 46–73 KB memory log. The saving is not "the
tokens the agent did not read" — without the store the agent does not know *which
file* to read. What it pays instead is a search.

### The price

| | |
| --- | --- |
| block size | median **797 tokens** (2 311 characters) |
| hook latency | **0.35–0.42 s**, including one store search |
| paid on | every prompt where the hook injects |

The character count fell when the excerpt rule changed (2 356 → 2 311) while the token
count rose slightly (748 → 797). The obvious suspect was the new `§` marker, so the
provider's own tokenizer was asked: a 2 876-character block and the same block with an
ASCII marker both report **938 prompt tokens**. The marker is free. The extra tokens are
the section lines themselves, which are short and fragmented — and they are what makes
the answer reachable.

### The return, and the three classes it comes in

Only the first class is a saving, and averaging the three together is how a cost
measurement starts lying:

| class | what it means |
| --- | --- |
| both arms answered | the only like-for-like measurement |
| only A answered | the store is the **only** place the fact exists |
| A also missed | the block was off-topic: cost with no return |

The repetitions are kept separate rather than averaged because **arm B gave different
verdicts on the same question**: it found the answer on question 2 in one run and failed
on it in the next, at temperature 0. The tool loop is path-dependent — a different first
`grep` leads somewhere else. A single run would produce a confident number in either
direction, which is why the third class is diagnosed rather than averaged away.

**The current run, under the current excerpt rule (5 questions, `openai/gpt-4o-mini`):
0 / 5 / 0.** Arm A answered every question; arm B answered none.

| | arm A | arm B |
| --- | --- | --- |
| answered correctly | **5 / 5** | **0 / 5** |
| tokens, median | **1 106** | 2 184 |
| tool calls, median | **0** | 6 |
| wall time, median | **1.7 s** | 10.4 s |
| token difference, median | **+1 196** | |

The token saving is real but not uniform, and the table should not pretend otherwise:
on questions 3 and 4 arm B spent **fewer** tokens than arm A (773 vs 1 106, 709 vs 1 165)
because it gave up quickly. A cheap failure is not a saving. What *is* uniform is the
shape: arm A answered four of five with **zero tool calls**, and the one that needed a
call needed exactly one, while arm B spent 1–9 calls and never got there. The robust
numbers are the **tool calls** (median 0 vs 6) and the **wall time** (median 1.7 s vs
10.4 s) — and, above all, the answer.

An earlier fifteen-run measurement under the **old** excerpt rule split **1 / 10 / 4**.
That class of four was the one worth chasing, and chasing it is what produced the two
sections below. It is kept here because the bucket is not empty by construction: it is
empty in this run, on these five questions, with this model.

### The reading

**The per-prompt answer is a split, not an average.** On a prompt whose answer the
store has and whose retrieval lands, arm A answers in ~1 000–2 400 tokens and 0–1 tool
calls, against 1–9 tool calls and 3–13 s for arm B — which usually comes back saying it
could not find the fact at all. On a prompt where retrieval misses, the block is a
**pure cost**, and worse than a pure cost: arm A spent 4 746 tokens on 3 tool calls and
still answered wrongly, because a plausible-but-adjacent block makes the model search
*and* mislead itself.

So the variable that decides the sign is **retrieval precision**, not the size of the
block. Tightening the block would not help a miss.

### What the third class led to: the query cap was cutting the question's tail

The class that costs without paying is the one worth chasing, so it was diagnosed
rather than averaged. For question 3 the answering note was **not in the result list
at all** — a recall failure, not a ranking one — and the cause was not in the store:

| query | rank of the answering note |
| --- | --- |
| the raw question | **3** (terms = 7) |
| the hook's processed query | **10** (terms = 5) |

`MAX_QUERY_WORDS` truncated the question and dropped its **last** content word,
`klienta` — and in Polish the specific noun tends to come last. The cap exists for a
measured reason (a 367-word prompt once demanded 111 matched terms and was therefore
never injected), so it is a two-sided limit: too low cuts ordinary questions, too high
puts `need` back out of reach for long prompts. It was judged on four sets at once:

| cap | description-style | junk | known misses | long prompts |
| --- | --- | --- | --- | --- |
| 12 | 4/5 | 0/14 | 0/2 | 1/2 |
| 14 | 5/5 | 0/14 | 0/2 | **1/2** |
| **16** | **5/5** | **0/14** | **0/2** | **2/2** |
| 20–32 | 5/5 | 0/14 | 0/2 | 2/2 |

14 also passes the first three sets, which is exactly why the long-prompt arm has to be
measured: without it, 14 looks like the minimal fix and silently reintroduces the bug
the cap was written for. 16 is the smallest cap that satisfies all four, and it sits at
the edge of a plateau, so the choice is not a knife edge.

**The deterministic result: retrieval went 4/5 → 5/5**, verified by
`savings_test.py --why` and by two regression tests.

### What the third class led to, part two: the block had the note but not its answer

Fixing the cap put question 3's answering note back in the block, and the block still
did not answer the question. That is the more interesting failure, because it looks
exactly like a healthy injection: the note's id and title were there.

The diagnosis, measured without a model:

| question | answering note | where it ranks | how deep the answer is |
| --- | --- | --- | --- |
| 1 | `verbigem-release-pipeline-android-to-mini` | 1 / 6 | 645 chars |
| 2 | `play-flavors-play-vs-standalone` | 1 / 6 | 683 chars |
| 3 | `firebase-identity-and-rules-model` | **3 / 6** | **2 046 chars** |
| 4 | `bug-noadsuntil-400-days` | 1 / 6 | 314 chars |
| 5 | `bug-aab-language-split-hides-locales` | 1 / 6 | 277 chars |

Four of five needed under 700 characters, which the old `BODY_CHARS = 700` delivered —
hence 4/5. The fifth needed 2 046, inside a note that ranked **third**, so no budget for
the *top* note could reach it. `firebase-identity-and-rules-model` is five numbered
sections and `BODY_LINES = 7` bought sections 1 and 2; the answer is a sentence under a
heading in section 3. Arm A read that block, did not find the answer, searched the
repository instead and answered `allow write: if false;`.

| excerpt rule | cap needed to reach every answer | block then |
| --- | --- | --- |
| head, 7 lines | never | 2 541 chars |
| head 2 100 chars, flat | 7 000 | 6 115 chars |
| **head 200 + one lead line per later heading** | **2 800** | **2 794 chars** |

So the excerpt is now the head, then every later heading with the line beneath it — a
heading names its section in a few words, which is what tells a reader whether the rest
is worth fetching. It reaches the answer at less than half the price of a flat raise, at
a **lower median block than the rule it replaces** (2 311 vs 2 356 characters), with
junk prompts still at **0/14** and substring sufficiency at **5/5**.

**The trap in that last sentence is worth stating plainly: "the block contains the
string" is necessary, not sufficient.** The run immediately after the change had
`affectedKeys` in the block for question 3 — and arm A still replied *"nie znalazłem
pliku ... ani wywołania"*, refusing both halves because it could not supply one. The
block was fine. The **question** was not: it asked two things, and the second half (the
file name) lives in a note marked `status: superseded`, which is deliberately never
injected. A question whose answer the block cannot contain is not a test of the block,
so the question was split and kept as its answerable half. After that: **5/5 answered by
arm A, 0/5 by arm B.**

Two lessons, both about instrumentation rather than retrieval. A substring check tells
you what the block *can* support and nothing about whether a model will use it. And a
question that fails can be failing for a reason that is in the question — which is why
the failing case is diagnosed with the ranking in hand instead of being counted.

### And the end-to-end effect is still not measurable at this sample size

Across the cost runs the "arm A also missed" class read 2, 1, 2, then **0**. Arm A's
verdict on a question flips between runs *with the same block*, so the difference is
inside the model's behaviour, not in what the store sent. A deterministic retrieval
improvement is visible; its effect on a five-question outcome is not, and one clean run
is not evidence that the bucket is closed.

**Re-ranking was tried and rejected, with evidence.** Weighting each matched term by its
rarity in the store (idf) pushes question 3 from rank 10 to **rank 15**, because the
notes that win also match the question's rare words. The term that identifies the answer
is absent from the question, and no reweighting of query terms can invent it. The
measurement is recorded in the eval set's header so it is not retried blind.

Two limits worth stating. Arm B's 0/5 is partly a weak-model result — a stronger model
searches better, and the *transferable* number is the cost per attempt (1–9 tool calls,
0.7–5.6 k tokens), not the success rate. And the tool output caps
(`GREP_MAX_MATCHES = 40`, `READ_MAX_LINES = 120`, a 25 s grep budget) decide how
expensive arm B is, so they are constants in the file rather than incidental
settings: an uncapped grep would make the store look magnificent.

## Tests

```bash
python test_agtmem_inject.py
```

217 checks, no dependencies beyond the standard library and an `agtmem` that can
be imported. The suite is hermetic: it points `AGTMEM_HOOK_RUNTIME` at a
temporary directory *before* importing the hook, so it never touches the live log
or state. Getting that wrong once meant a verification call silenced a note for a
real prompt.

It is **not part of CI**, and deliberately so: the gate is asserted against real
search results, so the suite needs a store with content. Against an empty store
every relevant prompt would stay silent and the suite would fail for the wrong
reason. Seeding a fixture corpus is the prerequisite — until then this runs where
the store lives.

The suite covers the gate, the block shape, the process contract (stdin JSON in,
hook JSON out, exit 0, nothing on stdout when silent), configuration resolution,
installer idempotence and non-destructiveness, and a section of production
regressions — each of which is a bug that a green suite did not catch, because it
depended on how the CLI invokes the hook rather than on the function under test.
