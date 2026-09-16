#!/usr/bin/env python3
"""Did the notes the hook injected actually reach the answer?

This is the counter the integration was missing. Delivery was always measurable
(`INJECT ... ids=...` in the log); *effect* was not, and an unfalsifiable claim of
value is what let a pointer-only hook look healthy for a day.

**Two arms, because the first one alone proved unusable.**

*Arm 1 — declared use.* Count `[agtmem:<id>]` markers in the answer. Precise,
binary, and the only signal that means what it says: the model states which note it
relied on. It requires the injected block to ask for the marker, so until that is
added this arm reads zero — which is information about the contract, not the notes.

*Arm 2 — lexical trace.* For each injection, take the terms of the injected excerpt
that the model **could only have learned from the note** and ask whether they show up
in the answer. A term qualifies only if it is

1. **distinctive** — present in at most `--df-max` notes in the store;
2. **absent from the prompt** — otherwise the answer is just echoing the question;
3. **absent from everything said earlier in the conversation**;
4. **present in the answer** — matched exactly, or by a six-character stem, because
   Polish inflects heavily and `wstrzykuje` / `wstrzykiwanie` are the same evidence.

**Measured on 2026-09-16, the first day the hook worked** — 17 injections whose notes
could be identified, 40 injected notes:

| arm | result | control |
| --- | --- | --- |
| 1 — declared `[agtmem:<id>]` | **0 of 40 notes**; 0 of 16 in the window where the instruction was live | binary, no null needed |
| 2 — exact match | 0 of 6 injections | 3-6% per note |
| 2 — six-character stem | **12 of 17 injections (71%)** | near 42%, far 33% |

Two lessons, both paid for.

*Exact matching has no power at all.* The model paraphrases; it does not reuse a
note's rare words. Only the stem arm moves.

*The matched control is the only one worth comparing against.* A random note from this
store traces 33% of the time, because the store is a single topic and every note
sounds like every answer. Notes that the same query gated in and did **not** inject
trace 42% — more than half the measurement. So 71% is a separation of roughly 1.7x,
which is suggestive and not established. A counter without a null is how you end up
quoting 71% as if it meant something.

Arm 1 deserves its own sentence: the block asks for the marker, the block demonstrably
reaches the model (verified in the transcript), and the marker has never once been
written. An instruction inside injected context reads as reference material, not as an
order. That is information about the contract, not about the notes.

What neither arm measures: that the note changed the outcome. A trace is necessary
evidence, not sufficient. Read the rate, not the individual hit.

**The control is built in and is not optional.** For every injection the same test
runs against notes that were *not* injected, over the same answer text. A counter
without a null is how you end up quoting 18% as if it meant something.

Usage:

    python measure_usage.py                    # today
    python measure_usage.py --day 2026-09-16
    python measure_usage.py --verbose          # name the traced terms
    python measure_usage.py --reconstruct      # recompute ids for older log lines
    AGTMEM_HOOK_RUNTIME=... python measure_usage.py   # a different runtime dir

Limits worth stating out loud:

* It can only see injections made **after** the hook started logging `sess=`.
  Earlier lines have no session id, so they are resolved by prompt text, which is
  a guess; those are counted separately and marked `~`.
* Likewise, only injections made **after** `ids=` was added can name their notes.
  `--reconstruct` replays the hook's own selection to recover the rest, and prints
  a self-check against the lines that do carry ids (marked `^` in `--verbose`).
  It is measured at 5 of 6 exact, so treat a reconstructed set as an upper bound.
* It reads the transcript as the CLI wrote it. Injected context is **not** persisted
  there (see the README), which is exactly why the hook has to log what it sent.
* Token overlap is a proxy, and a weak one — see the measured numbers above.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import sqlite3
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agtmem_inject as hook            # noqa: E402  (path set above, deliberately)

LOG_LINE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
                      r"\[(?P<src>[^\]]*)\]\s+(?P<rest>.*)$")
INJECT = re.compile(r"^INJECT event=(?P<event>\S+) (?P<chars>\d+)c"
                    r"(?: sess=(?P<sess>\S+))?(?: ids=(?P<ids>\S+))? "
                    r"prompt='(?P<prompt>.*)'$")
SILENT = re.compile(r"^silent event=(?P<event>\S+)"
                    r"(?: sess=(?P<sess>\S+))? prompt='(?P<prompt>.*)'$")
USER_QUERY = re.compile(r"<user_query>(.*?)</user_query>", re.S)
# The block shows the note as `- fact/<id>`, so a citing model may well echo the type
# prefix. Accept it, but capture only the id — the counter compares against bare ids.
MARKER = re.compile(r"\[agtmem:(?:[a-z]+/)?([A-Za-z0-9._-]+)\]")
STEM = 6                # characters of a term that count as the same evidence
PROMPT_KEY = 30         # characters of the prompt used to resolve a legacy line


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

def parse_log(path: str) -> list[dict]:
    """One dict per prompt event, in order. Unparseable lines are skipped."""
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return events
    for line in lines:
        m = LOG_LINE.match(line.rstrip("\n"))
        if not m:
            continue
        rest = m.group("rest")
        hit = INJECT.match(rest)
        kind = "inject"
        if not hit:
            hit = SILENT.match(rest)
            kind = "silent"
        if not hit:
            continue
        ts = time.mktime(time.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S"))
        fields = hit.groupdict()
        events.append({
            "ts": ts,
            "kind": kind,
            "event": fields.get("event") or "?",
            "chars": int(fields.get("chars") or 0),
            "sess": (fields.get("sess") or "").strip(),
            "ids": [i for i in (fields.get("ids") or "").split(",") if i and i != "-"],
            "prompt": fields.get("prompt") or "",
        })
    return events


def blocks_text(content) -> str:
    """Concatenate the text blocks of a message record, ignoring tool payloads."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def load_conversations(root: str) -> dict[str, list[tuple]]:
    """``{session_id: [(ts_ms, role, text), ...]}``, ordered by time."""
    out: dict[str, list[tuple]] = {}
    for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
        fallback = os.path.basename(path)[:-len(".jsonl")]
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            if '"message"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") != "message" or rec.get("role") not in ("user", "assistant"):
                continue
            text = blocks_text(rec.get("content"))
            if not text.strip():
                continue
            out.setdefault(str(rec.get("sessionId") or fallback), []).append(
                (int(rec.get("timestamp") or 0), rec["role"], text))
    for turns in out.values():
        turns.sort(key=lambda t: t[0])
    return out


def note_paths(index_db: str) -> dict[str, str]:
    """``{note_id: absolute path}`` from the disposable index."""
    try:
        con = sqlite3.connect(f"file:{index_db}?mode=ro", uri=True)
        try:
            return {str(i): str(p) for i, p in
                    con.execute("select id, path from notes") if i and p}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def document_frequency(store: str, paths: dict[str, str]) -> Counter:
    """How many notes each token appears in — the store's own vocabulary profile."""
    df: Counter = Counter()
    for path in paths.values():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for token in set(tokens(text)):
            df[token] += 1
    return df


def tokens(text: str) -> list[str]:
    """Lowercased tokens, normalised exactly the way the hook normalises them.

    `WORD_RE` admits `.`, `-` and `/` *inside* a token, because that is what keeps
    `hooks.json`, `E2eCrypto.kt` and `./gradlew` whole. The cost is that a
    sentence-ending period is glued on: `notatkę.` comes back as one token, which the
    store can never contain, so it would count as a term that never traces no matter
    what the answer says. `content_words` strips it; a counter that forgets to is
    quietly measuring the wrong string.
    """
    out: list[str] = []
    for raw in hook.WORD_RE.findall(text):
        token = raw.lower().strip(".-/")
        if len(token) >= 3 and not token.isdigit():
            out.append(token)
    return out


# --------------------------------------------------------------------------- #
# Resolving an injection to the answer it produced
# --------------------------------------------------------------------------- #

def find_turn(convo: list[tuple], event: dict) -> int | None:
    """Index of the user turn this injection belongs to, or None.

    The transcript record is written when the prompt is submitted and the hook runs
    a moment later, so the nearest user turn is normally a few seconds *behind* the
    logged second — measured -1.2 s to -19.1 s. The window is deliberately wide and
    the closest candidate wins; the prompt text only breaks ties. With a session id
    this is exact. Without one — lines written before `sess=` was added — it is a
    guess, and callers mark those results.
    """
    key = event["prompt"][:PROMPT_KEY]
    best, best_score = None, None
    for i, (ts_ms, role, text) in enumerate(convo):
        if role != "user":
            continue
        delta = ts_ms / 1000.0 - event["ts"]
        if not -120 <= delta <= 300:
            continue
        quoted = USER_QUERY.search(text)
        body = (quoted.group(1) if quoted else text).strip()
        prefix_ok = bool(key) and body.startswith(key)
        score = (0 if prefix_ok else 1, abs(delta))
        if best_score is None or score < best_score:
            best, best_score = i, score
    return best


def answer_window(convo: list[tuple], turn: int) -> tuple[str, str]:
    """``(everything before the turn, everything the assistant said in that turn)``."""
    before = "\n".join(t for _, _, t in convo[:turn])
    after: list[str] = []
    for _, role, text in convo[turn + 1:]:
        if role == "user":
            break
        after.append(text)
    return before, "\n".join(after)


def full_prompt(convo: list[tuple], turn: int) -> str:
    """The prompt as the user wrote it, from the transcript.

    The log truncates it at 60 characters, which is fine for a human reading the log
    and useless for reconstructing a query: fewer content words means a different
    search. Reconstruction has to start from the transcript.
    """
    text = convo[turn][2]
    quoted = USER_QUERY.search(text)
    return (quoted.group(1) if quoted else text).strip()


def replay(prompt: str) -> tuple[list[str], list[str]]:
    """``(what the hook injected, everything that passed its gate)``.

    **Validated against the lines that do carry ids: 5 of 6 matched exactly, and the
    sixth was a superset by one note** — the difference is the repeat-suppression
    filter, whose state is pruned after REPEAT_WINDOW and is therefore unrecoverable.
    Treat a reconstructed set as an upper bound, never as the exact set.

    The second half is the useful part for the counter. `gate - injected` is every
    note that passed the same gate on the same query and was then dropped — by the
    `KEEP` cut or by suppression. Those notes share the prompt's topic and the
    store's vocabulary profile; the only thing separating them from the injected set
    is that they never reached the model. That is a matched control, and it is the
    only control that can tell "the note reached the answer" from "this store always
    sounds like this".
    """
    terms = hook.query_words(hook.content_words(prompt))
    if len(terms) < 2:
        return [], []
    rows = hook.search(" ".join(terms[:hook.MAX_QUERY_WORDS]))
    need = hook.required_terms(terms)
    gated = [r.get("id") for r in rows
             if isinstance(r, dict) and int(r.get("terms") or 0) >= need]
    return gated[:hook.KEEP], gated


def reconstruct_ids(prompt: str) -> list[str] | None:
    """Just the injected half of `replay()` — an upper bound on what was injected."""
    injected, _ = replay(prompt)
    return injected or None


def stem_hit(term: str, haystack: str) -> bool:
    """Exact match, or a six-character stem — Polish inflects, evidence does not.

    `wstrzykuje` in a note and `wstrzykiwanie` in the answer are the same fact
    restated. Six characters is a crude stem and will occasionally match unrelated
    words; the control arm is what keeps that honest.

    `haystack` must already be lowercased. Terms are lowercased by `tokens()` while
    the answer is prose, so comparing against raw text silently misses every term the
    answer happened to capitalise — including, in Polish, most sentence-initial words.
    """
    if term in haystack:
        return True
    return len(term) >= STEM + 1 and term[:STEM] in haystack


def trace_terms(excerpt: str, prompt: str, before: str, df: Counter,
                df_max: int) -> list[str]:
    """The terms of an excerpt that only the note could have supplied."""
    prompt_tokens = set(tokens(prompt))
    before_tokens = set(tokens(before))
    out = []
    for term in dict.fromkeys(tokens(excerpt)):
        if df.get(term, 0) > df_max:
            continue
        if term in prompt_tokens or term in before_tokens:
            continue
        out.append(term)
    return out


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def measure(events: list[dict], convos: dict, df: Counter, paths: dict,
            df_max: int, verbose: bool, control: bool,
            reconstruct: bool = False) -> dict:
    stats = Counter()
    rows: list[dict] = []
    rng = random.Random(20260916)
    all_ids = [i for i, p in paths.items() if p]

    for ev in events:
        if ev["event"] != "UserPromptSubmit":
            continue
        if ev["kind"] == "silent":
            stats["silent"] += 1
            continue
        stats["inject"] += 1

        convo = None
        guessed = False
        if ev["sess"] and ev["sess"] != "-":
            for sid, turns in convos.items():
                if sid.startswith(ev["sess"]):
                    convo = turns
                    break
        if convo is None:
            guessed = True
            for sid, turns in convos.items():
                if find_turn(turns, ev) is not None:
                    convo = turns
                    break
        if convo is None:
            stats["unresolved"] += 1
            continue
        turn = find_turn(convo, ev)
        if turn is None:
            stats["no_turn"] += 1
            continue
        before, after = answer_window(convo, turn)
        if not after.strip():
            stats["no_answer"] += 1
            continue
        after_low = after.lower()      # terms are lowercased; the answer is prose

        ids, was_reconstructed = ev["ids"], False
        replayed: list[str] = []
        near: list[str] = []
        if control or reconstruct:
            # One replay serves both the reconstruction and the matched control; two
            # calls would double the subprocess cost for the same answer.
            replayed, near = replay(full_prompt(convo, turn))
        if not ids and reconstruct:
            ids = replayed
            was_reconstructed = True
            stats["reconstructed"] += 1
        if ev["ids"] and reconstruct:
            # Self-check: the reconstruction is only trustworthy while it keeps
            # reproducing the lines that do carry ids.
            stats["check_n"] += 1
            stats["check_ok"] += set(replayed) == set(ev["ids"])
            stats["check_superset"] += set(replayed) > set(ev["ids"])

        stats["resolved"] += 1
        if guessed:
            stats["guessed"] += 1
        # The denominator that matters: an injection whose notes cannot be named is
        # not evidence of absence, it is no evidence at all. Counting those rows as
        # "no trace" is how a denominator of 15 hides a sample of 4.
        if ids:
            stats["testable"] += 1

        # Arm 1 — declared use. Precise and binary: the model either wrote the marker
        # or it did not. Only possible once the block asks for it (see README); until
        # then this is always zero, which is information about the contract, not
        # about the notes.
        declared = set(MARKER.findall(after))
        hit_ids = [i for i in ids if i in declared]
        stats["declared_ids"] += len(hit_ids)
        stats["injected_ids"] += len(ids)
        stats["declared_turns"] += 1 if hit_ids else 0
        stats["stray_markers"] += len(declared - set(ids))

        # Arm 2 — lexical trace, with its own null.
        hit_terms: list[str] = []
        for note_id in ids:
            path = paths.get(note_id)
            if not path:
                stats["missing_note"] += 1
                continue
            excerpt = hook.note_excerpt(path)
            candidates = trace_terms(excerpt, ev["prompt"], before, df, df_max)
            stats["candidates"] += len(candidates)
            hit_terms += [t for t in candidates if stem_hit(t, after_low)]

        if hit_terms:
            stats["traced"] += 1
        rows.append({
            "ts": ev["ts"], "prompt": ev["prompt"], "ids": ids,
            "chars": ev["chars"], "terms": hit_terms, "guessed": guessed,
            "sess": ev["sess"] or "?", "declared": hit_ids,
            "reconstructed": was_reconstructed,
        })

        if control:
            # Matched control — the arm that actually isolates the injection. These
            # notes came back from the same query and passed the same gate, so they
            # share the prompt's topic and the store's vocabulary; the only thing
            # separating them from `ids` is that they were not injected. If this rate
            # is close to the measured one, the trace is reading the store's style,
            # not the injection.
            near_notes = [i for i in near if i not in ids and paths.get(i)]
            near_hit = False
            for note_id in near_notes:
                excerpt = hook.note_excerpt(paths[note_id])
                candidates = trace_terms(excerpt, ev["prompt"], before, df, df_max)
                stats["near_n"] += 1
                if any(stem_hit(t, after_low) for t in candidates):
                    stats["near_traced"] += 1
                    near_hit = True
            if near_notes:
                stats["near_injections"] += 1
                stats["near_inj_traced"] += near_hit

            # Far control — a random note from the store. It cannot isolate anything
            # (the store is one topic, so even an unrelated note sounds like the
            # answer); it measures that baseline, which is why it is printed.
            # Same unit on both sides, or the comparison is meaningless: the measured
            # rate is per *injection* (one traced note is enough to mark the turn),
            # so the control must also be per injection, drawn with the same number of
            # notes. Comparing a per-injection rate against a per-note rate inflates
            # the measurement purely by giving it more chances to hit.
            fakes: set[str] = set()
            for _ in range(len(ids) or 1):
                if not all_ids:
                    break
                fake = rng.choice(all_ids)
                if fake not in ids:
                    fakes.add(fake)
            control_hit = False
            for fake in fakes:
                excerpt = hook.note_excerpt(paths[fake])
                candidates = trace_terms(excerpt, ev["prompt"], before, df, df_max)
                stats["control_candidates"] += len(candidates)
                stats["control_n"] += 1
                if any(stem_hit(t, after_low) for t in candidates):
                    stats["control_traced"] += 1
                    control_hit = True
            if fakes:
                stats["control_injections"] += 1
                stats["control_inj_traced"] += control_hit
    return {"stats": stats, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--df-max", type=int, default=3,
                    help="a term is distinctive if at most this many notes contain it")
    ap.add_argument("--verbose", action="store_true", help="name the traced terms")
    ap.add_argument("--no-control", action="store_true")
    ap.add_argument("--reconstruct", action="store_true",
                    help="recompute ids for log lines written before the hook logged them")
    args = ap.parse_args()

    cfg = hook.resolve_config()
    store = cfg["store"] or os.path.expanduser("~/.agtmem")
    index = os.path.join(store, ".index.sqlite")
    paths = note_paths(index)
    if not paths:
        print(f"no index at {index} — cannot compute document frequency", file=sys.stderr)
        return 2

    df = document_frequency(store, paths)
    events = [e for e in parse_log(hook.LOG)
              if time.strftime("%Y-%m-%d", time.localtime(e["ts"])) == args.day]
    convos = load_conversations(os.path.expanduser("~/.workbuddy-ai/projects"))

    print(f"day            : {args.day}")
    print(f"log            : {hook.LOG}")
    print(f"store          : {store}  ({len(paths)} notes indexed)")
    prompts = [e for e in events if e["event"] == "UserPromptSubmit"]
    print(f"prompt events  : {len(prompts)}   "
          f"(inject {sum(1 for e in prompts if e['kind'] == 'inject')}, "
          f"silent {sum(1 for e in prompts if e['kind'] == 'silent')})")

    result = measure(events, convos, df, paths, args.df_max, args.verbose,
                     not args.no_control, args.reconstruct)
    s, rows = result["stats"], result["rows"]

    print(f"resolved to an answer: {s['resolved']}"
          + (f"  ({s['guessed']} by prompt text, no sess= — treat as a guess)" if s["guessed"] else ""))
    for why in ("unresolved", "no_turn", "no_answer", "missing_note"):
        if s[why]:
            print(f"  could not use {why}: {s[why]}")

    if args.reconstruct:
        print(f"reconstructed ids   : {s['reconstructed']} of {s['resolved']} resolved "
              f"injections (log line predates `ids=`; an upper bound, not the exact set)")
        if s["check_n"]:
            pct = 100.0 * s["check_ok"] / s["check_n"]
            print(f"  self-check on the {s['check_n']} lines that do carry ids: "
                  f"{s['check_ok']} exact ({pct:.0f}%), "
                  f"{s['check_superset']} a superset by one or more notes")
            if s["check_ok"] != s["check_n"]:
                print("  not 100% exact — the replay is not reproducing the hook; "
                      "treat reconstructed ids as an upper bound and read the ratio")

    print(f"\ncandidate terms (distinctive, not in prompt, not earlier in the chat): "
          f"{s['candidates']}")
    print(f"arm 1 — declared use  : {s['declared_ids']} of {s['injected_ids']} injected notes "
          f"were marked `[agtmem:<id>]` in the answer")
    print(f"arm 2 — lexical trace : {s['traced']} / {s['testable']} injections "
          f"whose notes could be identified"
          + (f"  ({s['resolved'] - s['testable']} rows had no ids and could not be replayed)"
             if s["resolved"] > s["testable"] else ""))
    if not args.no_control:
        # A raw control count says nothing; only rates are comparable, and only when
        # both denominators are shown. Per-injection is the comparable one.
        rate = f"{100.0 * s['traced'] / s['testable']:.0f}%" if s["testable"] else "n/a"
        nrate = (f"{100.0 * s['near_inj_traced'] / s['near_injections']:.0f}%"
                 if s["near_injections"] else "n/a")
        frate = (f"{100.0 * s['control_inj_traced'] / s['control_injections']:.0f}%"
                 if s["control_injections"] else "n/a")
        print(f"        near control  : {s['near_inj_traced']} / {s['near_injections']} "
              f"injections using notes the same query gated in and did NOT inject  "
              f"({nrate} vs {rate} measured)   <- the one that matters")
        print(f"        far control   : {s['control_inj_traced']} / "
              f"{s['control_injections']} injections using random notes  ({frate}) "
              f"— the store's baseline, not a null")
    if s["stray_markers"]:
        print(f"        markers for notes not injected in that turn: {s['stray_markers']} "
              f"(quoted syntax, or leakage from an earlier turn)")

    if rows and args.verbose:
        print("\n-- per injection --")
        for r in sorted(rows, key=lambda r: r["ts"]):
            stamp = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
            # Two independent caveats on a single row: the session was guessed from
            # prompt text, and/or the ids were recomputed rather than logged.
            mark = ("~" if r["guessed"] else " ") + ("^" if r["reconstructed"] else " ")
            verdict = ",".join(r["terms"]) if r["terms"] else "no trace"
            if not r["ids"]:
                verdict = "NOT MEASURABLE — no ids and the replay found none"
            elif r["declared"]:
                verdict = "DECLARED " + verdict
            print(f"  {mark}{stamp} {r['sess']} {r['chars']:5d}c "
                  f"{len(r['ids'])} notes  {verdict}")
            print(f"            {r['prompt'][:72]!r}")

    print("\nRead the rate, not the hit. Arm 1 is precise but only exists once the block")
    print("asks for the marker; arm 2 is a proxy — a trace shows the note's vocabulary")
    print("entered the answer, not that the note changed the outcome. If the control is")
    print("not far below the measured rate, the instrument is seeing coincidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
