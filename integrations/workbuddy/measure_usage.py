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

**Measured on the first day of data (2026-09-16, 6 injected notes with ids): exact
matching found 0 traces against a control rate of 3-6%; the stem variant found 50%
against a control of 20-29%.** So exact matching has no power at all — the model
paraphrases and does not reuse a note's rare words — and the stem variant separates
by roughly 2x at n=6, which is suggestive and not evidence. Read arm 2 as a null
detector: if the control is not clearly below the measurement, believe nothing.

What neither arm measures: that the note changed the outcome. A trace is necessary
evidence, not sufficient. Read the rate, not the individual hit.

**The control is built in and is not optional.** For every injection the same test
runs against notes that were *not* injected, over the same answer text. A counter
without a null is how you end up quoting 18% as if it meant something.

Usage:

    python measure_usage.py                    # today
    python measure_usage.py --day 2026-09-16
    python measure_usage.py --verbose          # name the traced terms
    AGTMEM_HOOK_RUNTIME=... python measure_usage.py   # a different runtime dir

Limits worth stating out loud:

* It can only see injections made **after** the hook started logging `sess=`.
  Earlier lines have no session id, so they are resolved by prompt text, which is
  a guess; those are counted separately and marked `~`.
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
MARKER = re.compile(r"\[agtmem:([A-Za-z0-9._-]+)\]")
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
    return [t.lower() for t in hook.WORD_RE.findall(text) if not t.isdigit()]


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


def stem_hit(term: str, haystack: str) -> bool:
    """Exact match, or a six-character stem — Polish inflects, evidence does not.

    `wstrzykuje` in a note and `wstrzykiwanie` in the answer are the same fact
    restated. Six characters is a crude stem and will occasionally match unrelated
    words; the control arm is what keeps that honest.
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
            df_max: int, verbose: bool, control: bool) -> dict:
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

        stats["resolved"] += 1
        if guessed:
            stats["guessed"] += 1

        # Arm 1 — declared use. Precise and binary: the model either wrote the marker
        # or it did not. Only possible once the block asks for it (see README); until
        # then this is always zero, which is information about the contract, not
        # about the notes.
        declared = set(MARKER.findall(after))
        hit_ids = [i for i in ev["ids"] if i in declared]
        stats["declared_ids"] += len(hit_ids)
        stats["injected_ids"] += len(ev["ids"])
        stats["declared_turns"] += 1 if hit_ids else 0
        stats["stray_markers"] += len(declared - set(ev["ids"]))

        # Arm 2 — lexical trace, with its own null.
        hit_terms: list[str] = []
        for note_id in ev["ids"]:
            path = paths.get(note_id)
            if not path:
                stats["missing_note"] += 1
                continue
            excerpt = hook.note_excerpt(path)
            candidates = trace_terms(excerpt, ev["prompt"], before, df, df_max)
            stats["candidates"] += len(candidates)
            hit_terms += [t for t in candidates if stem_hit(t, after)]

        if hit_terms:
            stats["traced"] += 1
        rows.append({
            "ts": ev["ts"], "prompt": ev["prompt"], "ids": ev["ids"],
            "chars": ev["chars"], "terms": hit_terms, "guessed": guessed,
            "sess": ev["sess"] or "?", "declared": hit_ids,
        })

        if control:
            for _ in range(len(ev["ids"]) or 1):
                fake = rng.choice(all_ids) if all_ids else None
                if not fake or fake in ev["ids"]:
                    continue
                excerpt = hook.note_excerpt(paths[fake])
                candidates = trace_terms(excerpt, ev["prompt"], before, df, df_max)
                stats["control_candidates"] += len(candidates)
                if any(stem_hit(t, after) for t in candidates):
                    stats["control_traced"] += 1
    return {"stats": stats, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--df-max", type=int, default=3,
                    help="a term is distinctive if at most this many notes contain it")
    ap.add_argument("--verbose", action="store_true", help="name the traced terms")
    ap.add_argument("--no-control", action="store_true")
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
                     not args.no_control)
    s, rows = result["stats"], result["rows"]

    print(f"resolved to an answer: {s['resolved']}"
          + (f"  ({s['guessed']} by prompt text, no sess= — treat as a guess)" if s["guessed"] else ""))
    for why in ("unresolved", "no_turn", "no_answer", "missing_note"):
        if s[why]:
            print(f"  could not use {why}: {s[why]}")

    print(f"\ncandidate terms (distinctive, not in prompt, not earlier in the chat): "
          f"{s['candidates']}")
    print(f"arm 1 — declared use  : {s['declared_ids']} of {s['injected_ids']} injected notes "
          f"were marked `[agtmem:<id>]` in the answer")
    print(f"arm 2 — lexical trace : {s['traced']} / {s['resolved']} injections")
    if not args.no_control:
        print(f"        control       : {s['control_traced']} traces from notes NOT injected")
    if s["stray_markers"]:
        print(f"        markers for notes not injected in that turn: {s['stray_markers']} "
              f"(quoted syntax, or leakage from an earlier turn)")

    if rows and args.verbose:
        print("\n-- per injection --")
        for r in sorted(rows, key=lambda r: r["ts"]):
            stamp = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
            mark = "~" if r["guessed"] else " "
            verdict = ",".join(r["terms"]) if r["terms"] else "no trace"
            if r["declared"]:
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
