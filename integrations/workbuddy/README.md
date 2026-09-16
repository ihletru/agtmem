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

Each hit arrives with **the head of the note itself**, not just its title:

```
[agtmem] 1 trafienie w pamięci projektu (pełna treść: `agtmem show <id>`):
- fact/verbigem-release-pipeline-android-to-mini · 2026-09-15 — Wydanie Androida trafia na…
  Kanał „strona" (APK do pobrania) i kanał „Play" (AAB) są niezależne i łatwo je rozjechać.
  Pięć miejsc do podbicia
  1. `android/app/build.gradle.kts` — `versionCode` / `versionName`.
  2. `mini/vite.config.ts` — `ANDROID_VERSION_CODE` / `ANDROID_VERSION_NAME`. …
```

The first version of this hook injected `- fact/<id> — <title>` plus a
110-character snippet. That is a **pointer**: it tells the agent a note exists
and asks it to run `agtmem show <id>`. Over one measured day, **none of the nine
injections was followed by a `show`** — the decision never happened, so every
note was delivered and never read. That is the original failure this project
exists to fix, merely moved one step later.

Notes cannot be injected whole (median ~3 kB, mean ~9.8 kB, max 96 kB), but
their head can, and by the store's own convention the first section is the
essence. `search --json` returns a `path` but **not the body**, so the hook reads
the file itself — one local read per injected note, no second search. Excerpts
are indented, which is what keeps a Markdown bullet inside a note from being
mistaken for a note header.

| | pointer version | content version |
| --- | --- | --- |
| per note | ~30 tokens | ~180 tokens |
| whole block | 105–284 tokens | **~250–520 tokens** |
| one `agtmem search --json` it replaces | 1234 tokens | 1234 tokens |

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
| `agtmem-inject.log` | one line per event: the query, the gate decision, the ids injected |
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

## Tests

```bash
python test_agtmem_inject.py
```

148 checks, no dependencies beyond the standard library and an `agtmem` that can
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
