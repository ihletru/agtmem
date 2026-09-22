#!/usr/bin/env python3
"""Put the agtmem store into the agent's context, via WorkBuddy hooks.

This is an **integration**, not part of the store. `agtmem` deliberately has no
hooks and does not sit in the agent loop (see `docs/ARCHITECTURE.md`); it is a
store. Reading it at the right moment is the *consumer's* job, and this script is
one consumer's answer to that.

Two events, one script:

* ``UserPromptSubmit`` — search the store with the prompt's content words and
  inject the best hits. This is the one that makes the store *read*.
* ``SessionStart`` — inject a one-line reminder that the store exists, once per
  session id. Cheap insurance against the failure this project exists to fix: a
  store that is written automatically and read never.

WorkBuddy spawns the CodeBuddy CLI, which runs these. The payload arrives as
JSON on stdin; the answer goes to stdout as hook JSON. The CLI appends
``hookSpecificOutput.additionalContext`` to the conversation context before the
model sees the prompt.

**Nothing machine-specific is hardcoded.** Paths are resolved at run time, in
this order:

1. environment — ``AGTMEM_PYTHON``, ``AGTMEM_REPO``, ``AGTMEM_HOME``,
   ``AGTMEM_HOOK_RUNTIME``;
2. ``agtmem_inject.config.json`` next to this script — written by
   ``install_hooks.py`` so a fresh clone needs no editing;
3. a sane default — the interpreter running the hook, agtmem's own defaults for
   repo and store, and the desktop's hooks directory for the runtime files.

The sidecar exists because a hook command must be a literal string in
``settings.json``, and that string must not encode one person's home directory.
Install resolves the variables once; the hook reads them on every run.

**The log and the suppression state do not live next to this script.** They are
runtime artifacts; this file is meant to sit in a checkout under version control,
and a hook that writes beside itself dirties the working tree on every prompt.
See ``resolve_runtime()``.

Design rules, in order of importance:

1. **Never disturb a prompt.** Every failure path is silent and exits 0. A hook
   that raises, hangs or prints noise is worse than no hook.
2. **Silence beats noise.** Injecting an irrelevant note is worse than injecting
   nothing: the model treats injected text as authoritative. The gate is tuned
   for precision, not recall — a miss costs nothing, because the agent can still
   run `agtmem search` itself.
3. **Content, not pointers.** A note arrives with its head and the titles of its
   later sections, not just its title. The first version injected
   `- fact/<id> — <title>` plus a 110-character snippet, which is a *pointer*: it
   leaves the agent to decide whether to run `agtmem show <id>`. Measured over a
   full day, **none of the nine injections was followed by a `show`** — the
   decision never happened, so the note was delivered and never read, which is the
   exact failure this hook exists to fix. Content removes the decision. The second
   version carried the head only, which failed the same way one level down: the
   block held the note's id and title and not its answer, and the agent improvised
   instead of reading. See `note_excerpt`.
4. **Bounded cost.** <= 3 notes, <= 3000 chars of context, one search, 8 s cap.
   Measured median **797 tokens / 2311 characters** per injecting prompt, which is
   still less than the single `agtmem search --json` (1234 tokens) it replaces.

The gate was tuned on a labelled prompt set (see `test_agtmem_inject.py`).
`score` does NOT discriminate at all (irrelevant prompts score 0.0325-0.0328,
relevant ones 0.0307-0.0328), so it is ignored. The usable signal is `terms` —
how many distinct query terms the note contains — after the query has been
reduced to content words. Requiring 3 matched content words gave 0 false
positives across 10 junk prompts while keeping 5 of 7 relevant ones.

**What the gate cannot do**, measured rather than assumed (2026-09-16): a
meta-question about the session itself ("did you push that to the repo?") has no
answer in the store, and the gate cannot know that. The store returns its best
lexical matches, and two of the four "matched terms" came from a pronoun and
from substrings inside code identifiers (``hook`` inside ``paddleWebhook``,
``doc`` inside ``documentId``). No threshold fixes "the store has no answer" —
see the README for the signal that would (document frequency). What *is* fixed:
the pronoun is now a stopword, and `query_words()` drops tokens too short to be
evidence, which on the labelled set removed the noise without costing a single
relevant prompt.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "agtmem_inject.config.json")

SEARCH_TIMEOUT = 8.0
CANDIDATES = 6          # ask for more than we keep, so the gate can discard
KEEP = 3                # notes actually injected
MIN_TERMS_FLOOR = 3     # absolute floor on matched content words
TERM_RATIO = 0.3        # ... and a share of the prompt's content words
MIN_QUERY_CHARS = 4     # query tokens below this are not evidence (see query_words)
# Long prompts get truncated, not rejected. The value is measured across four sets
# at once — description-style questions, junk prompts, known misses, long prompts —
# and 16 is the smallest cap that satisfies all four. See MAX_QUERY_WORDS in the
# module docstring's sibling note on `query_words`, and `--why` in savings_test.py.
MAX_QUERY_WORDS = 16
MAX_BLOCK_CHARS = 3000  # worst case; the measured median block is 797 tokens
TRANSCRIPT_TAIL_BYTES = 200_000  # read the tail; a long transcript is tens of MB
CONTEXT_TURNS = 6       # how many recent messages count as "the conversation"
HEAD_CHARS = 200        # of one note, how much of its opening prose to carry
LEAD_CHARS = 100        # ... then one lead line under each later heading
HEADING_CHARS = 90      # a section title's own cap
BODY_CHARS = 1200       # the whole excerpt taken from one note, head included
MIN_TAIL_CHARS = 60     # below this much room, stop rather than emit a stub line
TITLE_CHARS = 84
SNIPPET_CHARS = 110     # fallback when the note file cannot be read
REPEAT_WINDOW = 45 * 60        # per conversation: note injected this recently
SESSION_WINDOW = 12 * 60 * 60  # session reminders: once per session, for 12 h
LOG_MAX_BYTES = 256 * 1024
GUESS_TIMEOUT = 4.0            # locating the package by import, lazily, once
LEGACY_BUCKET = "*"            # suppression entries from the pre-session schema

# Which settings file invoked us. The CLI resolves its user scope from a product
# name constant, and both `~/.workbuddy/settings.json` and `~/.codebuddy/settings.json`
# are plausible; the same script is registered in both, with a distinct label, so the
# log tells us which one actually fires. Overridden by `--src=<label>` on argv.
SRC = "workbuddy"

# Function words only. Deliberately conservative: a word that carries meaning in
# a prompt about this project must not be dropped.
#
# The pronoun family (`jego`, `jej`, `nim`, ...) is here because of a measured
# production failure, not for tidiness: `jego` appears in nearly every Polish
# note, so counting it as a matched content word handed irrelevant notes one
# free term each. One such note cleared the gate on `jego` plus two substring
# hits and was injected for a question about something else entirely.
STOPWORDS = frozenset("""
a aby albo ale alez and ani are as at az bardzo bedzie bez bo bowiem by byl byla
bylo byly bym bys ci co czemu czy czyli dla dlaczego do dokladnie gdy gdzie
gdyby go i ich if im in is it its ja jak jaka jakie jako jednak jest jesli jego
jej jeszcze juz kiedy ktora ktore ktory lub lecz ma mam masz mamy macie maja
mi mnie moim moja moje moj moze moze mu musi my na nad nam nas nasz nawet nie
nich niego niej nim niz no not o od oraz po pod ponad powinien przed przez przy
przeciez prawda sa sie sobie swoj swoich swoje tak takie takze tam te tego tej
ten teraz tez to trzeba tu tych tylko tym u w was we wiec wlasnie wy z za ze
zeby zez
the this that these those with from into over under then than them they
ok okay dzieki dziekuje thanks thank hello hey please yes
""".split())

ACK_RE = re.compile(r"^(ok|okay|tak|nie|dzieki|dziekuje|dobrze|jasne|pewnie|no|hmm|"
                    r"kontynuuj|dalej|go on|continue|yes|no)[.!?]*$", re.I)
WORD_RE = re.compile(r"[0-9A-Za-z\u00c0-\u024f_+#./-]{3,}")
HIGHLIGHT_RE = re.compile(r"\[([^\[\]]{0,60})\]")
NOTE_HEAD_RE = re.compile(r"^- [a-z]+/(\S+)")   # a bullet in the emitted block
FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.S)
SENTENCE_END_RE = re.compile(r"[.!?\u2026](?=\s|$)")
TABLE_SEP_RE = re.compile(r"^\|?[\s|:-]+\|?$")   # `|---|---|` in a Markdown table
HEADING_RE = re.compile(r"^#{1,4}\s+(.*)$")       # `## Sekcja` in a note body
SECTION_MARK = "  \u00a7 "                        # a later section's title, in the block
# A user turn in the transcript carries the injected context around the actual ask.
# Only the ask is evidence about what the conversation is working on.
USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S)


# --------------------------------------------------------------------------- #
# Where the runtime files go
# --------------------------------------------------------------------------- #

def _sidecar() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_runtime() -> str:
    """Directory for the log and the suppression state.

    Deliberately *not* the script's own directory. This file belongs in a
    checkout under version control, and a hook that appends a log line beside
    itself makes `git status` dirty on every prompt — which is exactly how a
    runtime artifact ends up committed by accident.

    Environment beats sidecar beats default. The installer records the real path,
    so the default is only reached on a hand-wired install.
    """
    for value in (os.environ.get("AGTMEM_HOOK_RUNTIME"), _sidecar().get("runtime")):
        if isinstance(value, str) and value.strip():
            return os.path.expanduser(value.strip())
    return os.path.join(os.path.expanduser("~"), ".workbuddy", "hooks")


RUNTIME = resolve_runtime()
STATE = os.path.join(RUNTIME, "agtmem-inject-state.json")
LOG = os.path.join(RUNTIME, "agtmem-inject.log")


def decode_payload(raw: bytes) -> str:
    """Decode the hook payload from bytes, as UTF-8, on purpose.

    **Do not use `sys.stdin.read()`.** Its codec is whatever the spawning process
    set, and in production that is not UTF-8: `zrób tą analizę` arrived as
    `zrÃ³b tÄ… analizÄ™`, and `WORD_RE` then chopped the mangled tokens further
    (`ó` is U+00F3 but `³` is U+00B3, below the regex's Latin range). The search
    query was built from that garbage, so the gate saw words no note contains.

    The CLI sends UTF-8 JSON. Decode it as UTF-8 and the environment stops
    mattering. Fall back only if the bytes are genuinely not UTF-8.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


def log(msg: str) -> None:
    """Diagnostic trace. Always on, one line per event, to a FILE — never stdout.

    A file is the only place this can go: stdout is the hook's return channel and
    anything else printed there is appended to the conversation context.
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{SRC}] {msg}\n"
    try:
        os.makedirs(RUNTIME, exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX_BYTES:
            with open(LOG, encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-LOG_MAX_BYTES // 2:]
            with open(LOG, "w", encoding="utf-8") as fh:
                fh.write(tail)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def tidy(text: str, limit: int) -> str:
    """Strip agtmem's `[term]` highlight markers and truncate on a word boundary."""
    out = HIGHLIGHT_RE.sub(r"\1", str(text or ""))
    out = " ".join(out.replace("\u2026", " ").split()).strip(" -")
    if len(out) <= limit:
        return out
    cut = out[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or out[:limit]) + "\u2026"


def plural(n: int) -> str:
    """Polish: 1 trafienie, 2-4 trafienia, 5+ trafień (teens take the 5+ form)."""
    if n == 1:
        return "trafienie"
    if 2 <= n <= 4:
        return "trafienia"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "trafienia"
    return "trafień"


def content_words(text: str) -> list[str]:
    words = WORD_RE.findall(text or "")
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        low = w.lower().strip(".-/")
        if len(low) < 3 or low in STOPWORDS or low.isdigit() or low in seen:
            continue
        seen.add(low)
        out.append(w.strip(".-/"))
    return out


# --------------------------------------------------------------------------- #
# Configuration — everything machine-specific is resolved here, never inlined.
# --------------------------------------------------------------------------- #

def resolve_config() -> dict:
    """Resolve python / repo / store: environment beats sidecar beats default.

    ``repo`` and ``store`` may legitimately come back ``None``. That means "let
    agtmem decide", which is the right answer for an installed package and for
    the default store location — so neither is ever written down here.
    """
    cfg = _sidecar()

    def pick(env_key: str, cfg_key: str) -> str | None:
        for value in (os.environ.get(env_key), cfg.get(cfg_key)):
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    # The interpreter executing this hook is by construction the right one, so
    # sys.executable is the default rather than a path recorded anywhere.
    python = pick("AGTMEM_PYTHON", "python") or sys.executable or "python3"
    cite = cfg.get("cite")
    return {
        "python": python,
        "repo": pick("AGTMEM_REPO", "repo"),
        "store": pick("AGTMEM_HOME", "store"),
        # Asking the model to name the notes it used. On by default because without
        # it there is no way to tell a read note from an ignored one — and that
        # question is the entire reason this integration exists. `"cite": false` in
        # the sidecar turns the line off and leaves the block exactly as it was.
        "cite": True if cite is None else bool(cite),
    }


def guess_repo() -> str | None:
    """Where is the `agtmem` package importable from?

    Only consulted when nothing is configured *and* the first attempt failed, so
    the normal path never pays for it.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import os, agtmem; print(os.path.dirname(os.path.dirname("
             "os.path.abspath(agtmem.__file__))))"],
            capture_output=True, timeout=GUESS_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    path = proc.stdout.decode("utf-8", "replace").strip()
    return path if path and os.path.isdir(path) else None


def _cwd_candidates(repo: str | None) -> list[str | None]:
    """Working directories to try, in order. A checkout needs its own cwd; an
    installed package works from anywhere."""
    out: list[str | None] = []
    if repo and os.path.isdir(repo):
        out.append(repo)
    out.append(None)          # inherit the hook's own cwd
    return out


def search(query: str) -> list[dict]:
    cfg = resolve_config()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if cfg["store"]:
        env["AGTMEM_HOME"] = cfg["store"]
    cmd = [cfg["python"], "-m", "agtmem", "search", query,
           "--limit", str(CANDIDATES), "--json"]

    cwds = _cwd_candidates(cfg["repo"])
    if cfg["repo"] is None:
        cwds.append(guess_repo())         # last resort: ask the interpreter
    proc = None
    for cwd in cwds:
        try:
            attempt = subprocess.run(cmd, cwd=cwd, env=env,
                                     capture_output=True, timeout=SEARCH_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f"search failed: {exc!r} cwd={cwd!r}")
            continue
        proc = attempt
        if attempt.returncode == 0:
            break
    if proc is None or proc.returncode != 0:
        log(f"search unavailable rc={getattr(proc, 'returncode', None)} "
            f"err={getattr(proc, 'stderr', b'')[:200]!r}")
        return []

    try:
        rows = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        log(f"search json bad: {exc!r}")
        return []
    return rows if isinstance(rows, list) else []


# --------------------------------------------------------------------------- #
# Suppression state — keyed per conversation, deliberately
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    """Always returns both keys — callers index into them directly.

    ``notes`` is a **per-session** map: ``{session_key: {note_id: timestamp}}``.
    The schema used to be flat, which was a real bug — a note injected while
    answering one question was silenced when it was the right answer again in a
    *different* conversation, and a weaker note was injected in its place. A flat
    entry found on disk is migrated into `LEGACY_BUCKET` and still honoured, so
    an upgrade does not silently re-inject everything once.
    """
    empty: dict = {"notes": {}, "sessions": {}}
    try:
        with open(STATE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty

    raw_notes = data.get("notes")
    notes: dict[str, dict] = {}
    if isinstance(raw_notes, dict):
        for key, value in raw_notes.items():
            if isinstance(value, dict):
                notes[str(key)] = {str(k): v for k, v in value.items()}
            else:
                # Pre-session schema: {note_id: timestamp}
                notes.setdefault(LEGACY_BUCKET, {})[str(key)] = value
    return {"notes": notes, "sessions": data.get("sessions") or {}}


def session_key(session_id: str) -> str:
    """Which suppression bucket this event belongs to.

    An empty id is not a conversation — it is an ad-hoc call from a shell or a
    test — so it gets its own bucket rather than being allowed to silence a real
    conversation. That is precisely how a verification run once changed what the
    hook did to the next prompt.
    """
    return session_id or LEGACY_BUCKET


def note_timestamps(state: dict, session_id: str) -> dict:
    """Entries that apply to this conversation: its own, plus any legacy ones."""
    notes = state.get("notes") or {}
    merged = dict(notes.get(LEGACY_BUCKET) or {}) if session_key(session_id) != LEGACY_BUCKET else {}
    merged.update(notes.get(session_key(session_id)) or {})
    return merged


def save_state(state: dict, now: float) -> None:
    """Prune stale entries and write atomically.

    Atomic because a hook can be killed mid-write by the CLI's timeout, and a
    half-written JSON file is a silent behaviour change (every note looks
    unsuppressed). `load_state` survives it; better not to create it.
    """
    pruned: dict = {"notes": {}, "sessions": {}}
    for key, bucket in (state.get("notes") or {}).items():
        live = {k: v for k, v in (bucket or {}).items()
                if now - float(v) < REPEAT_WINDOW}
        if live:
            pruned["notes"][key] = live
    pruned["sessions"] = {k: v for k, v in (state.get("sessions") or {}).items()
                          if now - float(v) < SESSION_WINDOW}
    tmp = STATE + ".tmp"
    try:
        os.makedirs(RUNTIME, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(pruned, fh)
        os.replace(tmp, STATE)
    except (OSError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass


def scope_from_cwd(cwd: str) -> str | None:
    """`<anywhere>/verbigem/android` -> `verbigem-android`, for tie-breaking.

    Matches the last `verbigem` segment anywhere in the path, so it does not
    depend on where the repo happens to live.
    """
    if not cwd:
        return None
    parts = [p for p in re.split(r"[\\/]+", cwd) if p]
    for i, part in enumerate(parts):
        if part.lower() == "verbigem" and i + 1 < len(parts):
            return f"verbigem-{parts[i + 1].lower()}"
    return None


def required_terms(words: list[str]) -> int:
    """How many matched terms a note needs before it is worth injecting.

    Scale against the **query we actually send**, not the whole prompt. The
    search only ever sees `words[:MAX_QUERY_WORDS]`, so the prompt's total length
    says nothing about whether a note is on topic — and using it was a real
    production bug: a 367-content-word prompt demanded 111 matched terms, which
    no note can have, so long prompts were **silently never injected**. With the
    query as the denominator the requirement is 3 or 4, and short prompts behave
    exactly as they did before.

    The cap therefore has a two-sided constraint, and `MAX_QUERY_WORDS` sits at the
    measured point where both sides are satisfied. Too low and the truncation cuts
    the *tail* of an ordinary question — in Polish the specific noun often comes
    last, and losing it cost a whole note: one description-style question put its
    answering note at rank 3 on the raw question and rank 10 on the hook's query,
    purely because the cap dropped `klienta` from "…pola przed zapisem z klienta".
    Too high and `need` rises until long prompts go silent again, which is the
    failure the cap was introduced to fix. Measured 2026-09-16 over four sets:

        cap   description-style   junk   known misses   long prompts
        12    4/5                 0/14   0/2            1/2
        14    5/5                 0/14   0/2            1/2   <- fixes the miss, breaks a long prompt
        16    5/5                 0/14   0/2            2/2   <- the smallest cap that satisfies all four
        20+   5/5                 0/14   0/2            2/2   <- flat, so 16 is the edge of a plateau

    A cap of 14 also passes the first three sets, which is why the long-prompt arm
    has to be measured: without it, 14 looks like the minimal fix and reintroduces
    the original bug for long prompts.
    """
    return max(MIN_TERMS_FLOOR, math.ceil(TERM_RATIO * len(words[:MAX_QUERY_WORDS])))


def query_words(words: list[str]) -> list[str]:
    """Drop tokens too short to be evidence, then cap the query length.

    Measured, not guessed. The store is indexed with trigram matching, so a short
    token matches *inside* longer identifiers: `doc` matched `documentId`, `hook`
    matched `paddleWebhook`. Those two hits, plus one on the pronoun `jego`, were
    enough for an irrelevant note to clear the gate and be injected for a
    meta-question the store has no answer to.

    On the labelled set, requiring four characters keeps **5/5 relevant prompts
    and 0/14 junk prompts** unchanged, while the block for that meta-question
    collapses from three notes to one — the single note that genuinely contains
    `repo` and `opis`. At five characters a relevant prompt is lost, so four is
    the boundary rather than a preference.

    This is a mitigation, not a fix: the real remedy is for the store to say
    which terms matched, so a consumer can require a whole-word hit. See README.
    """
    return [w for w in words if len(w) >= MIN_QUERY_CHARS]


def injected_ids(block: str) -> list[str]:
    """The note ids that are actually in the emitted block.

    ``kept=N`` counts candidates that passed the gate; the character budget can then
    drop some of them, so it is not the same set. Reading the ids back out of the block
    is the only way the log can say what the model really received — and without that,
    "was the right note delivered?" is unanswerable the next morning, because the
    suppression state that would have named them has been pruned by REPEAT_WINDOW.

    The block's own format is the contract here (``- <type>/<id> [· <updated>]``), so
    this stays a pure read of what was written rather than a second source of truth.
    """
    out: list[str] = []
    for line in block.splitlines():
        match = NOTE_HEAD_RE.match(line)
        if match:
            out.append(match.group(1))
    return out


def _cut(line: str, room: int) -> str:
    """Truncate a line to `room` characters, preferring a sentence boundary."""
    head = line[:room]
    ends = [m.end() for m in SENTENCE_END_RE.finditer(head)]
    return (head[:ends[-1]] if ends else head.rstrip()) + " \u2026"


def note_excerpt(path: str | None, limit: int = BODY_CHARS) -> str:
    """The head of a note, then every later heading with the line beneath it.

    Delivering the content is the point of this revision. `search --json` returns
    a `path` and a 110-character `snippet` but **not the body**, so the hook reads
    the file itself — one local read per injected note, no second search.

    The whole note cannot go in (median ~2.5 kB, max 96 kB), so something has to
    be chosen, and *what* to choose was measured rather than assumed. The first
    rule took the head — seven lines of prose — on the reasoning that "by the
    store's own convention the first section is the essence". For a five-section
    note that is false, and it failed silently: the block carried
    `fact/firebase-identity-and-rules-model`, its id and its title, and none of its
    answer, because `affectedKeys().hasAny([...])` sits at character 2046 of a
    3289-character body, under the heading "Lista pól, których klient nie może
    zapisać w `users/{uid}`". The agent read the block, did not find the answer,
    searched the repository instead and answered `allow write: if false;`.

    So the excerpt is the head, then for every heading below it the heading's own
    title and the first line under it. A heading names its section in a few words,
    and that is what tells a reader whether the rest is worth fetching. Measured
    against the same five questions, without a model — a block that does not
    contain the string a correct answer needs cannot be answered from the block,
    so substring sufficiency is checked directly:

        rule                        cap needed for 5/5   block then
        head, 7 lines (before)      never                 2541
        head 2100 chars, flat       7000                  6115
        head 200 + section leads    2800                  2794

    The structural rule reaches the answer at less than half the price of a flat
    raise, and at a *lower* median block than the rule it replaces — 2311 characters
    against 2356, measured over the five questions of `savings_test.py` — while
    answering 5/5 instead of 4/5, with junk prompts still at 0/14. That is why it
    is not a budget increase: on the ~27% of prompts where the notes answer nothing,
    a bigger flat budget is pure cost, and this one is not.

    What this does **not** establish is that a model will use what it is given. The
    run immediately after this change had `affectedKeys` in the block and still came
    back with "nie znalazłem" — the substring check says the block *can* support an
    answer and nothing more. See the README.

    Lines are indented so a Markdown bullet inside a note can never be mistaken
    for a note header by `injected_ids()`. Headings keep their words and lose
    their `#`. `limit` counts the note's own characters — the indent and the
    section marker are the block's, and `MAX_BLOCK_CHARS` is what bounds it.

    Returns "" when the file is unreadable, which makes the caller fall back to the
    snippet — a hook must degrade, never fail.
    """
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(64 * 1024)
    except OSError:
        return ""

    head_budget = min(HEAD_CHARS, limit)
    out: list[str] = []
    used = 0
    head_open = True       # still spending the head budget
    lead_next = False      # the next prose line is a section's lead
    truncated = False

    for raw in FRONTMATTER_RE.sub("", text, count=1).splitlines():
        line = raw.strip()
        if not line or TABLE_SEP_RE.match(line):    # `|---|---|` carries no content
            continue

        if head_open and head_budget - used < MIN_TAIL_CHARS:
            head_open = False                       # the head is full; sections follow
            truncated = True

        heading = HEADING_RE.match(line)
        if heading:
            if head_open:
                line = line.lstrip("#").strip()     # in the head a heading is prose
            else:
                title = tidy(heading.group(1), HEADING_CHARS)
                if not title or used + len(SECTION_MARK) + len(title) >= limit:
                    truncated = True
                    break
                out.append(SECTION_MARK + title)
                used += len(title) + len(SECTION_MARK)
                lead_next = True
                continue

        if head_open:
            room = head_budget - used
            if len(line) > room:
                out.append("  " + _cut(line, room))
                used += room
                head_open = False
            else:
                out.append("  " + line)
                used += len(line)
            continue

        if not lead_next:
            continue                                # prose between sections
        room = limit - used
        if room < MIN_TAIL_CHARS:
            truncated = True
            break
        lead = _cut(line, min(LEAD_CHARS, room)) if len(line) > min(LEAD_CHARS, room) else line
        out.append("    " + lead)
        used += len(lead)
        lead_next = False

    if truncated and out:
        out.append("  \u2026")                      # the note continues; say so
    return "\n".join(out)


def recent_context(path: str, turns: int = CONTEXT_TURNS) -> str:
    """The text of the last few messages, newest first.

    A production prompt is often three words — "działaj", "rób licznik", "co musimy
    zrobić żeby działało?" — and three words cannot say what the conversation is
    about. Measured on 2026-09-16: the search then returns notes that share those
    three words and answer nothing, while the note that actually answers the question
    sits unretrieved. The transcript says what we are working on, and every
    UserPromptSubmit payload carries its path.

    Newest first, deliberately: the caller appends these terms after the prompt's and
    then caps the query, so the freshest words must come first or the cap eats them.

    Read the tail, never the whole file. An 18 MB transcript is normal here and a
    hook has milliseconds.
    """
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            raw = fh.read()
    except OSError:
        return ""

    lines = raw.split(b"\n")
    if size > TRANSCRIPT_TAIL_BYTES:
        lines = lines[1:]        # the first line is a truncated record, not JSON
    msgs: list[str] = []
    for line in lines:
        if b'"message"' not in line:
            continue
        try:
            rec = json.loads(decode_payload(line))
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "message":
            continue
        if rec.get("role") not in ("user", "assistant"):
            continue
        content = rec.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and isinstance(b.get("text"), str))
        else:
            continue
        quoted = USER_QUERY_RE.search(text)
        if quoted:               # a user turn carries injected context around the ask
            text = quoted.group(1)
        if text.strip():
            msgs.append(text)
    return "\n".join(reversed(msgs[-turns:]))


def build_query(prompt: str, transcript: str = "") -> dict:
    """The terms to search on, the gate they imply, and what came back.

    Prompt terms first. The conversation is a **fallback, never a replacement**, and
    that is deliberate: a short prompt is often not a query at all — "a ile dokładnie?"
    is one usable term, and "co musimy zrobić żeby działało?" is three, which the gate
    turns into "all three or nothing". Measured with the canary harness: such prompts
    injected 0 of 5 times, and 5 of 5 once the transcript supplied the topic. But a
    prompt that already works must keep its own query, because re-ranking on
    conversation terms would let a note that merely echoes the last few minutes
    outrank the note that answers the question. So the transcript is consulted only
    when the prompt alone gated nothing in — the trigger is the gate, not the prompt's
    length, since a long prompt can gate nothing in too.

    Lives here rather than inside `build_context` because the canary harness needs the
    same query, and a second copy of this rule is a second place for it to drift.

    Returns `{terms, need, query, rows, kept, added}`.
    """
    query_terms = query_words(content_words(prompt))

    def attempt(terms: list[str]) -> tuple[str, int, list, list]:
        """One search, gated. `need` comes from the terms actually sent — the rule
        that fixed the long-prompt bug, where a 367-word prompt demanded 111 matched
        terms and was therefore silently never injected."""
        if len(terms) < 2:
            return "", 0, [], []
        query = " ".join(terms[:MAX_QUERY_WORDS])
        need = required_terms(terms)
        rows = search(query)
        kept = [r for r in rows
                if isinstance(r, dict) and int(r.get("terms") or 0) >= need]
        return query, need, rows, kept

    query, need, rows, kept = attempt(query_terms)
    effective = query_terms

    added = 0
    if not kept and transcript:
        seen = {w.lower() for w in query_terms}
        extra = [w for w in query_words(content_words(recent_context(transcript)))
                 if w.lower() not in seen]
        if extra:
            wide_terms = query_terms + extra
            wide_query, wide_need, wide_rows, wide_kept = attempt(wide_terms)
            if wide_kept:
                query, need, rows, kept = wide_query, wide_need, wide_rows, wide_kept
                effective = wide_terms
                # What actually went into the query, not how many words the
                # transcript offered: the cap drops most of them, and a log that
                # says 46 when 11 were sent is a log that lies.
                added = max(0, len(wide_terms[:MAX_QUERY_WORDS]) - len(query_terms))

    return {"terms": effective[:MAX_QUERY_WORDS], "need": need, "query": query,
            "rows": rows, "kept": kept, "added": added}


# --------------------------------------------------------------------------- #
# The nudge — the only channel that can ask the agent to use the store at all
# --------------------------------------------------------------------------- #

# The measured problem this exists for: **zero** calls to `mcp__agtmem__*` across
# 84 transcripts and 11 594 tool calls. Reading was never the bottleneck — the
# block arrives by itself on two prompts in three. Two things were missing:
#
#   1. A reason to *search*. The gate fails silently, so a question the store
#      cannot answer looks exactly like a question the store has nothing to say
#      about. That silence is the wasted moment: it is precisely when the agent
#      should learn that searching is an option.
#   2. A reason to *write*. Nothing ever asked. Knowledge reached the store only
#      through the two-hourly distillation of transcripts — delayed and lossy by
#      construction, and done by a different agent that has to re-derive what the
#      working agent already knew.
#
# Once per conversation, deliberately. A reminder on every prompt stops being read,
# and the block already carries notes on most prompts.
NUDGE_STATE = os.path.join(RUNTIME, "agtmem-nudge-state.json")
NUDGE_MIN_WORDS = MIN_TERMS_FLOOR     # a one-word prompt is not a question


def nudge_seen(session_id: str) -> bool:
    """Has this conversation already been nudged?

    Its own file, its own concern. Kept out of the suppression state on purpose:
    that file decides *which notes* get injected and has been measured; a nudge
    counter has no business being able to move those numbers.
    """
    try:
        with open(NUDGE_STATE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return time.time() - float(data.get(session_key(session_id), 0) or 0) < SESSION_WINDOW


def mark_nudge(session_id: str) -> None:
    """Record the nudge, pruning on the same window the session reminders use."""
    now = time.time()
    try:
        with open(NUDGE_STATE, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data = {k: v for k, v in data.items() if now - float(v or 0) < SESSION_WINDOW}
    data[session_key(session_id)] = now
    tmp = NUDGE_STATE + ".tmp"
    try:
        os.makedirs(RUNTIME, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, NUDGE_STATE)
    except (OSError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass


def build_nudge(cwd: str = "") -> str:
    """One line, once per conversation: how to search, and how to write.

    Short on purpose. It sits near the top of the block, so every character here
    is a character a note does not get — and the first version, placed last, was
    cut by the line-boundary truncation on the very first real block it met
    (2 596 chars of notes) and would never have been seen at all.
    """
    scope = scope_from_cwd(cwd)
    where = f" Ten katalog to scope `{scope}`." if scope else ""
    return ("[agtmem] Gdy pytanie dotyczy *dlaczego* — decyzji, przyczyny błędu, "
            "ustaleń z poprzednich sesji — poszukaj: `agtmem search \"<temat>\"`. "
            "Gdy ustalicie coś trwałego, zapisz od razu: `agtmem add --id <slug> "
            "--type decision --title \"...\" --body \"...\"`."
            f"{where}")


def build_context(prompt: str, cwd: str = "", session_id: str = "",
                  transcript: str = "") -> str | None:
    """UserPromptSubmit: return the notes to inject, or None to stay silent."""
    if not prompt or prompt.lstrip().startswith("/"):
        return None
    if ACK_RE.match(prompt.strip()):
        return None

    found = build_query(prompt, transcript)
    query, need, rows, kept = (found["query"], found["need"], found["rows"], found["kept"])
    log(f"query={query!r} cw={len(found['terms'])}/{len(content_words(prompt))} "
        f"ctx={found['added']} need={need} rows={len(rows)} kept={len(kept)}")
    if not rows or not kept:
        # The gate found nothing. Until now this was silent, which made "the store
        # has nothing to say" indistinguishable from "the store was never
        # consulted" — and it is precisely the moment the agent should be told that
        # searching is an option. Once per conversation; see NUDGE_STATE above.
        words = content_words(prompt)
        if len(words) < NUDGE_MIN_WORDS or nudge_seen(session_id):
            log(f"silent (gate empty) cw={len(words)} nudged={nudge_seen(session_id)}")
            return None
        mark_nudge(session_id)
        log(f"NUDGE gate empty cw={len(words)} sess={short_session(session_id)}")
        return build_nudge(cwd)

    # Scope breaks ties; it does not override relevance. The reorder this replaces
    # put every note from the current directory ahead of every other note with no
    # regard for `terms` — which contradicts `scope_from_cwd`'s own docstring, and
    # measured over the five questions it cost a question its answer: working in
    # `verbigem/mini` pushed an answering note with **terms = 10** behind three
    # `verbigem-mini` notes with terms 5-6, and out of the block entirely.
    # Across five working directories: 24/25 before, 25/25 after. Sorting by
    # `terms` first and using the scope match only among equal terms leaves the
    # store's own order as the secondary key, because this sort is stable.
    scope = scope_from_cwd(cwd)
    kept.sort(key=lambda r: (-int(r.get("terms") or 0),
                             0 if scope and r.get("scope") == scope else 1))
    kept = kept[:KEEP]

    now = time.time()
    state = load_state()
    recent = note_timestamps(state, session_id)
    fresh = [r for r in kept
             if now - float(recent.get(r.get("id"), 0) or 0) >= REPEAT_WINDOW]
    if not fresh:
        log("all candidates already injected recently")
        return None

    lines = [f"[agtmem] {len(fresh)} {plural(len(fresh))} w pamięci projektu "
             f"(pełna treść: `agtmem show <id>`):"]
    # Placed second, not last: the block is truncated line by line, and an
    # instruction that disappears whenever the notes are long is worse than none —
    # it would make the counter read zero and look like "the model ignores notes".
    if resolve_config()["cite"]:
        lines.append("  Jeśli z którejś korzystasz, dopisz na końcu odpowiedzi "
                     "`[agtmem:<id>]`.")
    # Third, right behind the cite line and for the same reason: the block is
    # truncated line by line, so a nudge at the bottom is a nudge that gets cut
    # whenever the notes are long — which is most of the time.
    if not nudge_seen(session_id):
        lines.append(build_nudge(cwd))
        mark_nudge(session_id)
        log(f"NUDGE appended to block sess={short_session(session_id)}")
    bodies = 0
    for r in fresh:
        head = f"- {r.get('type')}/{r.get('id')}"
        if r.get("updated"):
            head += f" \u00b7 {r['updated']}"
        title = tidy(r.get("title"), TITLE_CHARS)
        lines.append(f"{head} \u2014 {title}" if title else head)
        body = note_excerpt(r.get("path"))
        if body:
            bodies += 1
            lines.append(body)
        else:
            snip = tidy(r.get("snippet"), SNIPPET_CHARS)
            if snip:
                lines.append(f"  \u2026 {snip}")

    # A silent downgrade to snippets would look exactly like a healthy injection,
    # only ~6x smaller — which is how the pointer version went unnoticed for a day.
    if bodies < len(fresh):
        log(f"excerpt unavailable for {len(fresh) - bodies}/{len(fresh)} notes "
            f"\u2014 snippet fallback")

    # Truncate on a line boundary so the block never ends mid-sentence.
    block_lines: list[str] = []
    for line in lines:
        if len("\n".join(block_lines + [line])) > MAX_BLOCK_CHARS:
            break
        block_lines.append(line)
    if len(block_lines) < len(lines):
        block_lines.append("\u2026 (ucięte)")

    bucket = state["notes"].setdefault(session_key(session_id), {})
    for r in fresh:
        bucket[r.get("id")] = now
    save_state(state, now)
    return "\n".join(block_lines)


def build_session_context(cwd: str = "", session_id: str = "",
                          source: str = "startup",
                          transcript: str = "") -> str | None:
    """SessionStart: the store reminder once per session — or the checkpoint after a
    compaction.

    The two are deliberately different. The reminder is suppressed for 12 h, because
    repeating it every session is noise and the block already carries notes on every
    prompt. The checkpoint is **not** suppressed: it exists precisely because the
    window just collapsed, so the one moment it must not stay silent is right after
    that. Before this, `SessionStart(compact)` emitted only the generic reminder —
    which the 12 h window had usually already silenced.
    """
    if not session_id:
        return None

    if source == "compact":
        note = checkpoint_note(session_id)
        if note:
            log(f"checkpoint re-injected sess={short_session(session_id)} chars={len(note)}")
            return ("[agtmem] Kontekst właśnie został sklejony. Stan pracy z chwili przed "
                    f"kompakcją:\n\n{note}")
        # The note is absent in production, because `PreCompact` does not fire here
        # (see `build_checkpoint`). Compaction rewrites the *window*, not the
        # transcript, so the working set is still on disk and is recovered from
        # there. Same renderer, one dependency fewer — and the log says which of
        # the two paths ran, so "the note worked" never has to be assumed.
        ws = working_set(transcript)
        if ws["files"] or ws["asks"]:
            body = render_working_set(ws, "odtworzone z transkryptu")
            log(f"checkpoint derived sess={short_session(session_id)} "
                f"files={len(ws['files'])} asks={len(ws['asks'])} chars={len(body)}")
            return ("[agtmem] Kontekst właśnie został sklejony. Stan pracy odtworzony "
                    f"z transkryptu:\n\n{body}")
        # Falling through is deliberate: a missing checkpoint must not cost the
        # reminder as well, or a failed write turns into total silence.
        log(f"checkpoint missing after compact sess={short_session(session_id)}")

    now = time.time()
    state = load_state()
    if now - float(state["sessions"].get(session_id, 0) or 0) < SESSION_WINDOW:
        return None
    state["sessions"][session_id] = now
    save_state(state, now)

    scope = scope_from_cwd(cwd)
    where = f" Ten katalog to scope `{scope}`." if scope else ""
    repo = resolve_config()["repo"]
    how = f" (CLI z `{repo}`)" if repo else ""
    return ("[agtmem] Pamięć projektu: `agtmem search \"<temat>\"`"
            f"{how} albo narzędzia `mcp__agtmem__*`. Sięgnij po nią, gdy pytanie dotyczy "
            "*dlaczego* — decyzji, przyczyny błędu, ustaleń z poprzednich sesji. "
            f"Trafienia dostajesz też automatycznie przy każdym promptcie.{where}")


def short_session(session_id: str) -> str:
    """First 8 characters of the session id, or ``-`` when there is none.

    The log has to be joinable with the transcripts: an injection is evidence of
    nothing until you can find the answer that followed it. Without the session id
    the only remaining key is the truncated prompt text, which is ambiguous the
    moment two conversations open with the same words — and the whole point of the
    log is to make `measure_usage.py` possible.
    """
    return (session_id or "-")[:8]


# --------------------------------------------------------------------------- #
# PreCompact — the working set, captured before the window collapses
# --------------------------------------------------------------------------- #

# Why this exists. The compaction summary is written to *continue a task*, not to
# remember one: it keeps the shape of the work and drops the specifics — which
# files were open, what was in flight. Measured on this machine, emergency
# compaction fires at 84-89% of the window, and until now the only thing that
# knew the working set was the transcript, read after the fact by a poller.
#
# `PreCompact` is the one hook whose stdout does **not** land in the chat: it
# becomes `session.hookContexts.preCompactInstructions` and is consumed by the
# compaction step itself. So this handler does two separate things — writes a
# note (durable, searchable) and prints an instruction (steers the summary).
CHECKPOINT_PREFIX = "checkpoint-"
CHECKPOINT_TYPE = "log"
CHECKPOINT_TIMEOUT = 12.0
CHECKPOINT_FILES_MAX = 14
CHECKPOINT_ASKS_MAX = 5
CHECKPOINT_ASK_CHARS = 180
CHECKPOINT_NOTE_CHARS = 2000
# Deliberately much larger than TRANSCRIPT_TAIL_BYTES, and measured rather than
# guessed. 200 kB is tuned for `build_query`, which wants the *freshest* words. A
# working set wants the *session*: in a 1.3 MB agentic transcript the last 200 kB
# held **five assistant records and not one user record** — a long turn emits
# hundreds of tool records between two asks, so the asks sit far behind the tail.
# The first version of this function therefore wrote checkpoints with a file list
# and no asks at all, and looked like it had worked.
CHECKPOINT_TAIL_BYTES = 4_000_000
# Not every `role: user` record is an ask. The app appends scaffolding turns —
# `<system-reminder data-role="error-recovery">…</system-reminder>` and the like —
# and the first run of this function stored one of them as a question Milosz had
# asked. Strip the block, then require something to be left.
SYSTEM_BLOCK_RE = re.compile(r"<system-reminder\b.*?</system-reminder>", re.S)
# Only tools that change a file. `Read` is deliberately absent: a session reads
# dozens of files it never touches, and a working set that lists them says
# nothing about what was being worked on.
WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookWrite"})


def _run_cli(args: list[str], timeout: float) -> tuple[int, str]:
    """Run the agtmem CLI exactly the way `search` does — same interpreter, same store.

    Kept separate from `search` rather than folded into it: `search` has its own
    gate-tuned timeout and argument set, and rewriting a path that is already
    measured is how a working thing stops working.
    """
    cfg = resolve_config()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if cfg["store"]:
        env["AGTMEM_HOME"] = cfg["store"]
    cmd = [cfg["python"], "-m", "agtmem", *args]
    cwds = _cwd_candidates(cfg["repo"])
    if cfg["repo"] is None:
        cwds.append(guess_repo())
    for cwd in cwds:
        try:
            proc = subprocess.run(cmd, cwd=cwd, env=env,
                                  capture_output=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f"cli failed: {exc!r} cwd={cwd!r}")
            continue
        if proc.returncode == 0:
            return 0, proc.stdout.decode("utf-8", "replace")
        log(f"cli rc={proc.returncode} argv={args[:2]} cwd={cwd!r} "
            f"err={proc.stderr.decode('utf-8', 'replace')[:160]!r}")
    return 1, ""


def checkpoint_id(session_id: str) -> str | None:
    """One checkpoint per conversation, refreshed in place on each compaction.

    Derived from the session id rather than stored, so `SessionStart` can find it
    again after a compaction even if the hook's own state file was pruned or lost.
    """
    short = re.sub(r"[^0-9a-zA-Z]", "", session_id or "")[:8].lower()
    return f"{CHECKPOINT_PREFIX}{short}" if short else None


def working_set(path: str) -> dict:
    """Files written and asks made, from the transcript tail. Never the whole file.

    Same reading discipline as `recent_context`: an 18 MB transcript is normal
    here and a hook has milliseconds.
    """
    out: dict = {"files": [], "asks": []}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - CHECKPOINT_TAIL_BYTES))
            raw = fh.read()
    except OSError:
        return out

    lines = raw.split(b"\n")
    if size > CHECKPOINT_TAIL_BYTES:
        lines = lines[1:]        # the first line is a truncated record, not JSON
    files: list[str] = []
    asks: list[str] = []
    for line in lines:
        # Cheap pre-filter before the JSON parse. At 4 MB this is thousands of
        # records and almost all of them are tool traffic this function never
        # looks at; parsing them all costs the compaction window for nothing.
        if b"function_call" not in line and b"user" not in line:
            continue
        try:
            rec = json.loads(decode_payload(line))
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        kind = rec.get("type")
        if kind == "function_call":
            if rec.get("name") not in WRITE_TOOLS:
                continue
            argv = rec.get("arguments")
            if isinstance(argv, str):
                try:
                    argv = json.loads(argv)
                except ValueError:
                    continue
            fp = argv.get("file_path") if isinstance(argv, dict) else None
            if isinstance(fp, str) and fp.strip() and fp not in files:
                files.append(fp.strip())
        elif kind == "message" and rec.get("role") == "user":
            content = rec.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(b.get("text", "") for b in content
                                 if isinstance(b, dict) and isinstance(b.get("text"), str))
            else:
                continue
            quoted = USER_QUERY_RE.search(text)   # a user turn carries injected context
            if quoted:
                text = quoted.group(1)
            text = " ".join(SYSTEM_BLOCK_RE.sub(" ", text).split())
            # No `<user_query>` and the leftovers are still markup: not an ask.
            if not quoted and text.lstrip().startswith("<"):
                continue
            if text and text not in asks:
                asks.append(text)

    # The file runs oldest-first; a working set is read freshest-first.
    out["files"] = list(reversed(files))[:CHECKPOINT_FILES_MAX]
    out["asks"] = [tidy(a, CHECKPOINT_ASK_CHARS) for a in reversed(asks)][:CHECKPOINT_ASKS_MAX]
    return out


def render_working_set(ws: dict, trigger: str, stamp: str | None = None) -> str:
    """The checkpoint body, from a working set — shared by both producers.

    Two callers render this text now: `build_checkpoint` (PreCompact, which also
    persists it as a note) and the post-compaction fallback in
    `build_session_context`. They must not drift apart. The fallback exists
    precisely because the note is often absent, and a fallback that rendered a
    different body would be a second mechanism to debug rather than a safety net.
    """
    stamp = stamp or time.strftime("%Y-%m-%d %H:%M")
    lines = [f"Stan pracy przed kompakcją ({trigger}), {stamp}."]
    if ws["files"]:
        lines += ["", "## Pliki dotknięte w tej sesji"]
        lines += [f"- `{f}`" for f in ws["files"]]
    if ws["asks"]:
        lines += ["", "## O co pytał Milosz (od najnowszego)"]
        lines += [f"- {a}" for a in ws["asks"]]
    return "\n".join(lines)[:CHECKPOINT_NOTE_CHARS]


def build_checkpoint(payload: dict) -> str | None:
    """PreCompact: persist the working set, and tell the compaction to keep it.

    Returns the text that becomes `preCompactInstructions`, or None to stay silent.

    ⚠️ **Measured 2026-09-22: this event does not fire in production here.** The
    CLI bundle implements it (`executePreCompactHooks`, `preCompactInstructions`)
    and the registration is present, yet across 2026-09-19 → 09-22 the hook's own
    log holds 376 `UserPromptSubmit` lines and **zero** `PreCompact` lines, and
    the desktop log for an actual compaction (2026-09-21 07:12:12) spawns no
    process at 20 s — only the `SessionStart` one at 30 s, 42 s later, which then
    reports `checkpoint missing after compact`. So the durable note is a bonus
    when it happens and never a dependency: the working set is recovered from the
    transcript instead. See `build_session_context`.
    """
    session_id = str(payload.get("session_id") or "")
    transcript = str(payload.get("transcript_path") or "")
    cwd = str(payload.get("cwd") or "")
    trigger = str(payload.get("trigger") or "auto")
    nid = checkpoint_id(session_id)
    ws = working_set(transcript)
    if not nid or (not ws["files"] and not ws["asks"]):
        log(f"checkpoint skipped id={bool(nid)} files={len(ws['files'])} "
            f"asks={len(ws['asks'])}")
        return None

    scope = scope_from_cwd(cwd) or "global"
    body = render_working_set(ws, trigger)

    tmp = os.path.join(RUNTIME, "checkpoint.md")
    try:
        os.makedirs(RUNTIME, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:
        log(f"checkpoint body not written: {exc!r}")
        return None

    # Every field is passed on purpose. `--update` replaces the note wholesale, so
    # a field left out is a field silently dropped — the trap already recorded in
    # the store as `update wymaga wszystkich pól`.
    rc, _ = _run_cli(
        ["add", "--id", nid, "--type", CHECKPOINT_TYPE, "--scope", scope,
         "--title", f"Kontrolny punkt sesji {nid[len(CHECKPOINT_PREFIX):]}",
         "--body-file", tmp, "--tags", "checkpoint", "sesja", "kompakcja",
         "--origin", "tool", "--detail", f"PreCompact trigger={trigger}", "--update"],
        CHECKPOINT_TIMEOUT)
    try:
        os.remove(tmp)
    except OSError:
        pass
    if rc != 0:
        log(f"checkpoint NOT stored id={nid}")
        return None
    log(f"checkpoint stored id={nid} scope={scope} files={len(ws['files'])} "
        f"asks={len(ws['asks'])} body={len(body)}c")

    names = ", ".join(f"`{os.path.basename(f)}`" for f in ws["files"][:6]) or "brak"
    return ("[agtmem] Przed kompakcją: kontrolny punkt sesji zapisany jako notatka "
            f"`{nid}` (`agtmem show {nid}`). W streszczeniu zachowaj ścieżki plików "
            f"i otwarte pytania — pliki w tej sesji: {names}. "
            "Do pamięci projektu sięgaj przez `agtmem search \"<temat>\"`.")


def checkpoint_note(session_id: str) -> str | None:
    """The checkpoint written just before the compaction that is now finishing."""
    nid = checkpoint_id(session_id)
    if not nid:
        return None
    rc, out = _run_cli(["show", nid], SEARCH_TIMEOUT)
    if rc != 0:
        return None
    text = FRONTMATTER_RE.sub("", out).strip()
    return text[:CHECKPOINT_NOTE_CHARS] or None


# --- ingest right after the session ------------------------------------------
# The distillation pass runs every two hours, which leaves a window where a
# conversation that has just ended is not in the store at all — and the hook
# that would search for it runs at the *start* of the next session, i.e. inside
# that window. `SessionEnd` closes it: the transcript is imported the moment the
# conversation stops. `SessionStart` is only a catch-up, for the runs where the
# app was killed rather than closed and no `SessionEnd` ever fired.
INGEST_STATE = os.path.join(RUNTIME, "agtmem-ingest-state.json")
INGEST_TIMEOUT = 20.0          # the hook's own timeout is set a few seconds above
INGEST_LIMIT = 6               # notes per run; a backfill must not eat the window
INGEST_CATCHUP_AFTER = 900.0   # seconds; how stale the last run may be at startup


def _ingest_last() -> float:
    try:
        with open(INGEST_STATE, encoding="utf-8") as fh:
            return float(json.load(fh).get("last") or 0.0)
    except (OSError, ValueError, TypeError):
        return 0.0


def _mark_ingest(now: float) -> None:
    tmp = INGEST_STATE + ".tmp"
    try:
        os.makedirs(RUNTIME, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"last": now}, fh)
        os.replace(tmp, INGEST_STATE)
    except OSError as exc:
        log(f"ingest stamp not written: {exc!r}")


def ingest_after_session(event: str) -> None:
    """Import the session that just ended, before the two-hour pass reaches it.

    Writes nothing to stdout. `SessionEnd` has no consumer for hook output, and a
    stray line there would land in the transcript rather than in the model's
    context — the same silent-nothing that made the first `PreCompact` attempt
    look like a success.

    The catch-up is throttled on purpose: the one moment a user is waiting on
    this hook is when the window is opening, and the first `--new-only` run has
    no watermark yet, so it pays the full scan.
    """
    now = time.time()
    if event == "SessionStart" and now - _ingest_last() < INGEST_CATCHUP_AFTER:
        log("ingest catch-up not due")
        return
    # Claim the slot before the run, not after: `SessionEnd` and a following
    # `SessionStart` are close enough together to both pass the check above.
    _mark_ingest(now)
    rc, out = _run_cli(
        ["ingest-sessions", "--new-only", "--limit", str(INGEST_LIMIT)],
        INGEST_TIMEOUT)
    if rc != 0:
        log(f"ingest failed event={event}")
        return
    log(f"ingest event={event} {tidy(out, 160)}")


def selfcheck() -> int:
    """`--selfcheck`: print the resolved config and run one real search.

    This is the "first run" surface — it answers "is this hook wired up?" without
    needing the app, and it prints where each value came from.
    """
    cfg = resolve_config()
    print("config file :", CONFIG, "(present)" if os.path.exists(CONFIG) else "(absent)")
    print("runtime dir :", RUNTIME)
    print("state       :", STATE, "(present)" if os.path.exists(STATE) else "(absent)")
    print("log         :", LOG)
    print("python      :", cfg["python"])
    print("running as  :", sys.executable)
    print("repo        :", cfg["repo"] or "<agtmem default>")
    print("store       :", cfg["store"] or "<agtmem default>")
    print("cite        :", cfg["cite"], "(asks the model to mark the notes it used)")
    rows = search("kara za dlugosc coverage")
    print(f"search      : {len(rows)} rows")
    for r in rows[:3]:
        print(f"  - {r.get('type')}/{r.get('id')} terms={r.get('terms')}")
    return 0 if rows else 1


def main() -> int:
    global SRC
    for arg in sys.argv[1:]:
        if arg.startswith("--src="):
            SRC = arg[6:] or SRC
    if "--selfcheck" in sys.argv:
        return selfcheck()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        raw = sys.stdin.buffer.read()
    except (AttributeError, OSError):
        return 0
    try:
        payload = json.loads(decode_payload(raw)) if raw.strip() else {}
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0

    event = str(payload.get("hook_event_name") or "UserPromptSubmit")
    prompt = str(payload.get("prompt") or "")
    cwd = str(payload.get("cwd") or "")
    session_id = str(payload.get("session_id") or "")
    transcript = str(payload.get("transcript_path") or "")

    catchup = False
    try:
        if event == "SessionStart":
            context = build_session_context(cwd, session_id,
                                            str(payload.get("source") or "startup"),
                                            transcript)
            catchup = True
        elif event == "UserPromptSubmit":
            context = build_context(prompt, cwd, session_id, transcript)
        elif event == "PreCompact":
            context = build_checkpoint(payload)
        elif event == "SessionEnd":
            # Nothing to say to anyone here — the payload has no reader. The
            # work is the side effect: the transcript stops being a file only
            # the two-hour pass knows about.
            ingest_after_session(event)
            return 0
        else:
            log(f"ignored event={event}")
            return 0
    except Exception as exc:                      # noqa: BLE001 - must never raise
        log(f"ERROR {event} raised: {exc!r} sess={short_session(session_id)} "
            f"prompt={prompt[:60]!r}")
        return 0

    # PreCompact is the one event that does NOT take the JSON envelope: the CLI
    # reads its stdout straight into `hookContexts.preCompactInstructions`, which
    # the compaction step consumes. Wrapping it would feed the summariser a JSON
    # blob instead of an instruction.
    if event == "PreCompact":
        if context:
            sys.stdout.write(context)
            log(f"PRECOMPACT {len(context)}c sess={short_session(session_id)}")
        else:
            log(f"silent event=PreCompact sess={short_session(session_id)}")
        return 0

    if context:
        out = {"hookSpecificOutput": {"hookEventName": event,
                                      "additionalContext": context}}
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        log(f"INJECT event={event} {len(context)}c sess={short_session(session_id)} "
            f"ids={','.join(injected_ids(context)) or '-'} prompt={prompt[:60]!r}")
    else:
        log(f"silent event={event} sess={short_session(session_id)} "
            f"prompt={prompt[:60]!r}")
    # After the injection, never before: the catch-up scan is the slow part and
    # the injection is what the user is actually waiting on. It runs last for a
    # reason — the notes it imports are for the *next* search, not this one.
    if catchup:
        ingest_after_session("SessionStart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
