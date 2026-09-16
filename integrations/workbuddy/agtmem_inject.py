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
3. **Bounded cost.** <= 3 notes, <= 1200 chars of context, one search, 8 s cap.

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
MAX_QUERY_WORDS = 12    # long prompts get truncated, not rejected
MAX_BLOCK_CHARS = 1200
TITLE_CHARS = 84
SNIPPET_CHARS = 110
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
    return {
        "python": python,
        "repo": pick("AGTMEM_REPO", "repo"),
        "store": pick("AGTMEM_HOME", "store"),
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


def build_context(prompt: str, cwd: str = "", session_id: str = "") -> str | None:
    """UserPromptSubmit: return the notes to inject, or None to stay silent."""
    if not prompt or prompt.lstrip().startswith("/"):
        return None
    if ACK_RE.match(prompt.strip()):
        return None

    words = content_words(prompt)
    query_terms = query_words(words)
    if len(query_terms) < 2:
        log(f"skip: {len(query_terms)} usable content words of {len(words)}")
        return None

    query = " ".join(query_terms[:MAX_QUERY_WORDS])
    need = required_terms(query_terms)
    rows = search(query)
    if not rows:
        log(f"no rows for {query!r}")
        return None

    kept = [r for r in rows if isinstance(r, dict) and int(r.get("terms") or 0) >= need]
    log(f"query={query!r} cw={len(query_terms)}/{len(words)} need={need} "
        f"rows={len(rows)} kept={len(kept)}")
    if not kept:
        return None

    scope = scope_from_cwd(cwd)
    if scope:
        kept.sort(key=lambda r: 0 if r.get("scope") == scope else 1)
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
    for r in fresh:
        head = f"- {r.get('type')}/{r.get('id')}"
        if r.get("updated"):
            head += f" \u00b7 {r['updated']}"
        title = tidy(r.get("title"), TITLE_CHARS)
        lines.append(f"{head} \u2014 {title}" if title else head)
        snip = tidy(r.get("snippet"), SNIPPET_CHARS)
        if snip:
            lines.append(f"  \u2026 {snip}")

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


def build_session_context(cwd: str = "", session_id: str = "") -> str | None:
    """SessionStart: a one-line reminder that the store exists, once per session."""
    if not session_id:
        return None
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

    try:
        if event == "SessionStart":
            context = build_session_context(cwd, session_id)
        elif event == "UserPromptSubmit":
            context = build_context(prompt, cwd, session_id)
        else:
            log(f"ignored event={event}")
            return 0
    except Exception as exc:                      # noqa: BLE001 - must never raise
        log(f"ERROR {event} raised: {exc!r} prompt={prompt[:60]!r}")
        return 0

    if context:
        out = {"hookSpecificOutput": {"hookEventName": event,
                                      "additionalContext": context}}
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        log(f"INJECT event={event} {len(context)}c prompt={prompt[:60]!r}")
    else:
        log(f"silent event={event} prompt={prompt[:60]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
