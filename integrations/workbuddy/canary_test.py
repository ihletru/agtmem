#!/usr/bin/env python3
"""Does an injected note change the answer? The canary, and nothing weaker.

Every other number this integration has produced is a proxy. The counter counts
vocabulary overlap, and vocabulary overlap cannot tell "this note was read" from
"this topic was discussed" — measured on 2026-09-16 the matched control read 57%
against a 59% measurement, which is not a null, it is a second measurement. So the
honest state was: delivery proven, effect unknown.

This asks the one question with an unambiguous answer, by the old method: plant a
fact that cannot be guessed, ask for it, and see whether it comes back.

    store   a throwaway copy of the real store, plus the canary notes
    arm A   prompt + the block the hook would inject, wrapped as production wraps it
    arm B   prompt alone                              <- the null
    arm C   prompt + the block for a DIFFERENT canary <- the wrong-note control

Arm B must read zero. If a model can produce the canary from the prompt alone the
canary is guessable and the run is void — that is a failure of the test, not a
finding about the notes. Arm C must also read zero: it separates "used the note it
was given" from "filled the slot with something plausible".

Two modes, because there are two questions:

* **default** — the prompt names the topic. Tests the pipe: given a note that
  answers the question, does the block get it into the answer? Measured 5/5 with
  the block, 0/5 without, on both models tried.
* **`--vague`** — the prompt is a three-word follow-up ("a ile dokładnie?") and a
  synthetic transcript supplies the topic, the way production works. This is where
  the integration actually failed: three content words retrieve notes that share
  those three words and answer nothing.

A canary is deliberately not a rare word lifted from a note. It is a value with no
other possible source — an odd number, a codename, a build hash — inside a sentence
that reads like every other note in the store, so retrieval sees nothing unusual.
The check is an exact substring match, which is why it cannot be gamed.

Cost is three calls per canary. Tokens are the only thing this spends; the store it
writes to is a copy and the real one is never touched.

Usage:

    python canary_test.py                        # the pipe, 5 canaries
    python canary_test.py --vague                # thin prompt + transcript
    python canary_test.py --diagnose             # no model calls: show the ranking
    python canary_test.py --model openai/gpt-4o-mini
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agtmem_inject as hook            # noqa: E402  (path set above, deliberately)

API = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-chat"
# .../agtmem/integrations/workbuddy/canary_test.py -> the repo root, which is the
# only cwd from which `python -m agtmem` resolves.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The wrapper production uses. The hook returns `additionalContext`; WorkBuddy shows
# it to the model as a hook system-reminder, verified in a session transcript on
# 2026-09-16. Reproducing it here is the difference between testing the mechanism
# and testing a string concatenation of my own invention.
WRAPPER = '<system-reminder data-role="hook">\n{block}\n</system-reminder>'


# --------------------------------------------------------------------------- #
# The canaries.
#
# `ask` is a prompt that names the topic, so the hook retrieves the note. `vague`
# is a three-word follow-up, and `topic` is what an earlier turn said — together
# they reproduce production, where the current prompt alone is not a query.
# The token must be unguessable and must not appear in `topic` or `vague`.
# --------------------------------------------------------------------------- #

CANARIES = [
    dict(
        id="canary-credit-limit",
        title="Limit darmowych kredytów w portfelu Mini Verbigem",
        body="Portfel kredytów w Mini Verbigem ma dzienny limit darmowych jednostek.\n\n"
             "Wartość limitu: 4217 kredytów na dobę, licząc od północy czasu lokalnego.\n"
             "Po przekroczeniu progu tłumaczenie przechodzi na własny klucz użytkownika.\n",
        ask="ile wynosi dzienny limit darmowych kredytów w portfelu Mini Verbigem?",
        vague="a ile dokładnie?",
        topic="przeglądam dzienny limit darmowych kredytów w portfelu Mini Verbigem",
        token="4217",
    ),
    dict(
        id="canary-build-number",
        title="Numer builda ostatniego podpisanego APK Mini Verbigem",
        body="Ostatni podpisany APK Mini Verbigem ma numer builda 0x7F3A2B.\n\n"
             "Numer trafia do `mini/dist/android/version.json` razem z sumą SHA-256.\n"
             "Auto-update porównuje ten numer, nie datę pliku.\n",
        ask="jaki numer builda ma ostatni podpisany APK Mini Verbigem?",
        vague="a jaki dokładnie?",
        topic="sprawdzam numer builda ostatniego podpisanego APK Mini Verbigem",
        token="0x7F3A2B",
    ),
    dict(
        id="canary-review-codename",
        title="Kryptonim modułu powtórek w webappce Verbigem",
        body="Moduł powtórek w webappce Verbigem nosi wewnętrzny kryptonim Wąsik.\n\n"
             "Kryptonim występuje w nazwach testów i w komentarzach, nie w UI.\n"
             "Zmiana kryptonimu wymaga aktualizacji snapshotów testowych.\n",
        ask="jak nazywa się wewnętrzny kryptonim modułu powtórek w webappce Verbigem?",
        vague="a jak to nazwaliśmy?",
        topic="ustalam kryptonim modułu powtórek w webappce Verbigem",
        token="Wąsik",
    ),
    dict(
        id="canary-play-console-ticket",
        title="Numer zgłoszenia do Play Console o Advertising ID",
        body="Zgłoszenie do Play Console w sprawie deklaracji Advertising ID ma numer CZ-8814.\n\n"
             "Dotyczy deklaracji uprawnień przy publikacji aplikacji Verbigem.\n"
             "Odpowiedź recenzenta przyszła po dwóch dniach roboczych.\n",
        ask="jaki numer ma zgłoszenie do Play Console w sprawie Advertising ID?",
        vague="a jaki numer?",
        topic="wypełniam zgłoszenie do Play Console w sprawie Advertising ID",
        token="CZ-8814",
    ),
    dict(
        id="canary-hook-latency",
        title="Zmierzona latencja hooka agtmem",
        body="Hook agtmem wstrzykujący notatki ma zmierzoną latencję 840 milisekund.\n\n"
             "Pomiar obejmuje jedno wywołanie wyszukiwania w magazynie.\n"
             "Powyżej trzech sekund hook jest przerywany przez limit czasu.\n",
        ask="jaka jest zmierzona latencja hooka agtmem przy wstrzykiwaniu notatek?",
        vague="a ile to trwa?",
        topic="mierzę latencję hooka agtmem przy wstrzykiwaniu notatek",
        token="840",
    ),
]


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

def clone_store(real: str, dest: str) -> None:
    """A writable copy of the live store, so the canaries never touch the original.

    The real notes come along on purpose. A store holding only canaries would make
    retrieval trivial and the result would not transfer to production, where a
    canary competes with 289 other notes for three slots.
    """
    shutil.copytree(real, dest, dirs_exist_ok=True)


def plant(store: str, canaries: list) -> None:
    """Write the canary notes through the CLI, so they get real frontmatter.

    The CLI has to run from the repo root: `python -m agtmem` only resolves there.
    Silently ignoring the return code here cost a whole diagnostic run in which every
    canary reported "NOT RETRIEVED" — the notes did not exist, so the run was
    measuring an empty store and saying so in the language of retrieval failure.
    """
    py = sys.executable
    for c in canaries:
        body_file = os.path.join(store, f".{c['id']}.body")
        with open(body_file, "w", encoding="utf-8") as fh:
            fh.write(c["body"])
        proc = subprocess.run(
            [py, "-m", "agtmem.cli", "add", "--id", c["id"], "--type", "fact",
             "--scope", "canary", "--title", c["title"], "--body-file", body_file,
             "--origin", "agent"],
            cwd=REPO, env={**os.environ, "AGTMEM_HOME": store},
            capture_output=True, timeout=120,
        )
        try:
            os.remove(body_file)
        except OSError:
            pass
        if proc.returncode != 0:
            raise SystemExit(f"could not plant {c['id']}: "
                             f"{proc.stderr.decode('utf-8', 'replace')[-400:]}")
    reindex = subprocess.run([py, "-m", "agtmem.cli", "reindex"],
                             cwd=REPO, env={**os.environ, "AGTMEM_HOME": store},
                             capture_output=True, timeout=300)
    if reindex.returncode != 0:
        raise SystemExit("reindex failed: "
                         + reindex.stderr.decode("utf-8", "replace")[-400:])


def write_transcript(directory: str, topic: str) -> str:
    """A two-message transcript in the shape the CLI writes, so the hook can read it.

    It establishes the topic and never mentions the canary value — otherwise the
    test would be measuring whether the model can copy a number out of the context
    it was handed, which is not the question.
    """
    path = os.path.join(directory, "canary-session.jsonl")
    now = int(time.time() * 1000)
    records = [
        {"type": "message", "role": "user", "sessionId": "canary",
         "timestamp": now - 120_000,
         "content": [{"type": "text", "text": f"<user_query>{topic}</user_query>"}]},
        {"type": "message", "role": "assistant", "sessionId": "canary",
         "timestamp": now - 60_000,
         "content": [{"type": "text", "text": "Sprawdzam to w magazynie."}]},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

def ask(model: str, prompt: str, context: str | None, prior: str = "",
        timeout: float = 240.0) -> str:
    """One completion, shaped the way a real conversation is shaped.

    `prior` is the earlier exchange. It matters in `--vague` mode: production hands
    the model the whole conversation, so "a ile dokładnie?" is unambiguous to it, and
    a harness that sends the follow-up alone would be testing whether a model can
    answer a question with no referent — which is a test of my harness, not of the
    injection.
    """
    content = prompt if context is None else f"{WRAPPER.format(block=context)}\n\n{prompt}"
    messages = []
    if prior:
        messages.append({"role": "user", "content": prior})
        messages.append({"role": "assistant", "content": "Sprawdzam to w magazynie."})
    messages.append({"role": "user", "content": content})
    payload = json.dumps({
        "model": model,
        "max_tokens": 700,
        "temperature": 0,
        "messages": messages,
    }).encode("utf-8")
    req = urllib.request.Request(
        API, data=payload,
        headers={"Authorization": "Bearer " + os.environ.get("OPENROUTER_API_KEY", ""),
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        return f"__ERROR__ {exc.code} {exc.read()[:300].decode('utf-8', 'replace')}"
    except Exception as exc:                                    # noqa: BLE001
        return f"__ERROR__ {type(exc).__name__} {exc}"
    choices = data.get("choices") or []
    if not choices:
        return "__ERROR__ no choices: " + json.dumps(data)[:300]
    return choices[0].get("message", {}).get("content") or ""


def says(answer: str, token: str) -> bool:
    """Exact substring, case-folded. No stemming, no fuzzy matching — a canary is
    either reproduced or it is not, and that is the entire point of using one."""
    return token.casefold() in answer.casefold()


# --------------------------------------------------------------------------- #
# The hook, on a real prompt, against the cloned store
# --------------------------------------------------------------------------- #

def block_for(prompt: str, transcript: str = "") -> str | None:
    """Suppression is disabled on purpose: this tests "given the note is in the
    block, does the model use it", and a note hidden by a 45-minute repeat window
    would look like a retrieval failure."""
    hook.load_state = lambda *a, **k: {"notes": {}}
    hook.save_state = lambda *a, **k: None
    return hook.build_context(prompt, session_id="canary", transcript=transcript)


def diagnose(canaries: list, mode: str, work: str, use_context: bool) -> int:
    """Where does the answering note land? No model calls.

    A canary that comes back MISS has two very different reasons needing different
    fixes: the note may never be in the result list (recall), or it may be there but
    ranked below the three injected slots (ranking). This prints which, with numbers.
    Each canary gets its own transcript — sharing one would test the first canary's
    topic five times.
    """
    for c in canaries:
        prompt = c["vague"] if mode == "vague" else c["ask"]
        transcript = (write_transcript(work, c["topic"])
                      if mode == "vague" and use_context else "")
        # The same query builder the hook uses, so the diagnostic cannot drift from
        # the behaviour it is diagnosing.
        found = hook.build_query(prompt, transcript)
        rows = found["rows"]
        print(f"--- {c['id']}  ({mode})")
        print(f"    prompt={prompt!r}")
        print(f"    query={found['query']!r}")
        print(f"    terms={len(found['terms'])} ctx={found['added']} "
              f"need={found['need']} rows={len(rows)} KEEP={hook.KEEP}")
        passed, rank = [], None
        for i, r in enumerate(rows, 1):
            rid = r.get("id") if isinstance(r, dict) else None
            rterms = int(r.get("terms") or 0) if isinstance(r, dict) else 0
            ok = rterms >= found["need"]
            if ok:
                passed.append(rid)
            if rid == c["id"]:
                rank = i
            print(f"      {i:2d}. {'PASS' if ok else 'gate'} terms={rterms:3d}  {rid}")
        print(f"    canary rank={rank}")
        if rank is None:
            print("    -> NOT RETRIEVED: the note is not in the result list at all")
        elif c["id"] not in passed[:hook.KEEP]:
            print(f"    -> RANKED OUT: gate position {passed.index(c['id']) + 1}, "
                  f"only {hook.KEEP} slots")
        else:
            print("    -> IN THE TOP SLOTS")
        print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--keep", action="store_true", help="keep the temp store")
    ap.add_argument("--only", default="", help="run one canary id")
    ap.add_argument("--vague", action="store_true",
                    help="thin follow-up prompt + synthetic transcript, as in production")
    ap.add_argument("--no-context", action="store_true",
                    help="hand the hook no transcript, i.e. the behaviour before "
                         "2026-09-16: the only fair way to state a before/after")
    ap.add_argument("--diagnose", action="store_true",
                    help="no model calls: print where each canary lands in the ranking")
    args = ap.parse_args()
    mode = "vague" if args.vague else "ask"

    if not os.environ.get("OPENROUTER_API_KEY") and not args.diagnose:
        print("OPENROUTER_API_KEY is not set — no model, no canary", file=sys.stderr)
        return 2

    real = os.path.expanduser(os.environ.get("AGTMEM_HOME") or "~/.agtmem")
    if not os.path.isdir(real):
        print(f"no store at {real}", file=sys.stderr)
        return 2

    canaries = [c for c in CANARIES if not args.only or c["id"] == args.only]
    work = tempfile.mkdtemp(prefix="agtmem-canary-")
    store = os.path.join(work, "store")
    clone_store(real, store)
    plant(store, canaries)
    os.environ["AGTMEM_HOME"] = store

    print(f"model          : {args.model}")
    print(f"mode           : {mode}")
    print(f"real store     : {real}")
    print(f"canary store   : {store}")
    print(f"canaries       : {len(canaries)}\n")

    if args.diagnose:
        return diagnose(canaries, mode, work, not args.no_context)

    stats = {"injected": 0, "miss": 0, "a": 0, "b": 0, "c": 0, "error": 0, "cited": 0}
    for c in canaries:
        prompt = c["vague"] if mode == "vague" else c["ask"]
        transcript = (write_transcript(work, c["topic"])
                      if mode == "vague" and not args.no_context else "")
        block = block_for(prompt, transcript)
        injected = hook.injected_ids(block) if block else []
        if c["id"] not in injected:
            stats["miss"] += 1
            print(f"MISS  {c['id']}: the hook did not select it "
                  f"(injected {injected or 'nothing'})")
            continue
        stats["injected"] += 1
        prior = c["topic"] if mode == "vague" else ""
        # The model always sees the conversation; only the hook is blinded.
        # Otherwise --no-context would be measuring the model, not the hook.

        answer_a = ask(args.model, prompt, block, prior)
        # Arm B is the null, arm C the wrong note. Both must stay empty, or the
        # canary was guessable and this run proves nothing.
        answer_b = ask(args.model, prompt, None, prior)
        other = next(x for x in canaries if x["id"] != c["id"])
        answer_c = ask(args.model, prompt, block_for(
            other["vague"] if mode == "vague" else other["ask"],
            write_transcript(work, other["topic"])
            if mode == "vague" and not args.no_context else ""), prior)

        if any(a.startswith("__ERROR__") for a in (answer_a, answer_b, answer_c)):
            stats["error"] += 1
            print(f"ERR   {c['id']}: {answer_a[:140]}")
            continue

        got_a, got_b, got_c = (says(answer_a, c["token"]), says(answer_b, c["token"]),
                               says(answer_c, c["token"]))
        stats["a"] += got_a
        stats["b"] += got_b
        stats["c"] += got_c
        cited = bool(re.search(r"\[agtmem:", answer_a))
        stats["cited"] += cited

        mark = lambda ok: "yes" if ok else "no "                    # noqa: E731
        print(f"A {mark(got_a)}  B {mark(got_b)}  C {mark(got_c)}  "
              f"cite {'yes' if cited else 'no '}  {c['id']}")
        if not got_a:
            print(f"      answer: {answer_a[:220]!r}")

    n = stats["injected"] or 1
    print(f"\ninjected by the hook : {stats['injected']} / {len(canaries)}"
          + (f"  ({stats['miss']} retrieval misses)" if stats["miss"] else ""))
    if stats["error"]:
        print(f"model errors         : {stats['error']}")
    print(f"arm A  block present : {stats['a']} / {n}   <- the canary came back")
    print(f"arm B  no block      : {stats['b']} / {n}   <- the null; must be 0")
    print(f"arm C  wrong note    : {stats['c']} / {n}   <- must be 0")
    print(f"cited the note       : {stats['cited']} / {n}")

    if stats["b"] or stats["c"]:
        print("\nVOID: a control read non-zero, so the canary is guessable from the "
              "prompt alone. The test, not the integration, is what failed.")
    elif stats["injected"] == 0:
        print("\nINCONCLUSIVE: the hook never selected a canary, so nothing about the "
              "model was tested. Fix retrieval, not the block.")
    elif stats["a"] == stats["injected"]:
        print("\nThe injection works: the model used the note and could not have known "
              "the value without it.")
    else:
        print(f"\nThe block is delivered and the model does not use it "
              f"({stats['a']} / {n}). That is a finding about the block, not about "
              f"retrieval — the note was in front of the model.")

    if args.keep:
        print(f"\nkept: {store}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
