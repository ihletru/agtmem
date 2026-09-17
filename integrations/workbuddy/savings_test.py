#!/usr/bin/env python3
"""What does one prompt cost with the store, and what does it cost without it?

agtmem exists so an agent does not spend time and tokens rediscovering what is
already written down. That is a claim about *cost*, and until now this integration
had no cost measurement at all. The canary proves a note reaches the answer; the
counter proves nothing either way. Neither of them says what the note saved.

The saving is not "the tokens the agent did not read". Without the store the agent
does not know *which file* to read — what it pays instead is a **search**, and a
search here is expensive: this workspace is 27 GB and 33 186 files, and the fact is
often buried in a 72 KB memory log. So the measurement is a counterfactual:

    arm A   the prompt plus the block the hook would inject, tools available
    arm B   the prompt alone, tools available

Same question, same tools, same system prompt. Arm A usually answers from context;
arm B has to go and look. The difference in tokens, tool calls and wall time is the
saving, and the token figure is read off the API's own `usage` counters rather than
estimated from character counts — an estimate here would be worthless, because the
two arms differ in exactly the thing being estimated.

Two ways arm B can end, and they are different findings:

* it finds the fact after N tool calls -> the saving is finite, and this measures it
* it never finds it                    -> the saving is unbounded, and the store is
                                          the only place the fact exists

That is what `truth` is for. An arm that fails to answer must be reported as a
failure, not quietly counted as a large win. Each question carries the substrings a
correct answer must contain; `answered` in the output is that check, not a
hand-wave.

Tool output caps are part of the measurement, not incidental: a grep that returns
400 lines would inflate arm B and flatter the store. They are set to what a real
harness does (40 matches, 120 lines per read) and they are constants below, so the
number can be reproduced or argued with.

Read-only, and provably so: every tool resolves its path inside the workspace and
refuses to leave it. Nothing in this script writes to the repository.

Usage:

    python savings_test.py                     # all questions, deepseek-chat
    python savings_test.py --only 3            # one question
    python savings_test.py --model openai/gpt-4o-mini
    python savings_test.py --max-turns 10
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agtmem_inject as hook            # noqa: E402  (path set above, deliberately)

API = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-chat"
DEFAULT_WORKSPACE = os.path.expanduser("~/verbigem")

# The wrapper production uses, so arm A is shaped like a real conversation rather
# than like a string concatenation of my own invention.
WRAPPER = '<system-reminder data-role="hook">\n{block}\n</system-reminder>'

SYSTEM = ("Jesteś agentem pracującym w repozytorium. Odpowiadaj po polsku i zwięźle. "
          "Jeśli brakuje ci faktów, użyj narzędzi, żeby je znaleźć.")

# --------------------------------------------------------------------------- #
# Tool output caps. These decide how expensive arm B is, so they are named and
# deliberately modest: an uncapped grep would make the store look magnificent.
# --------------------------------------------------------------------------- #
GREP_MAX_MATCHES = 40
GREP_MAX_FILE_BYTES = 1_000_000
GREP_BUDGET_SECONDS = 25.0
READ_MAX_LINES = 120
READ_MAX_CHARS = 12_000
LIST_MAX_ENTRIES = 120

# What an agent skips because it is not source: VCS metadata, dependency caches,
# build output. `dist` is deliberately NOT here — `mini/dist/android/version.json`
# is a served artifact and one of the answers.
SKIP_DIRS = {".git", "node_modules", ".next", ".gradle", "build", ".idea",
             "__pycache__", ".venv", ".kotlin", ".cxx", ".turbo", "coverage"}


# --------------------------------------------------------------------------- #
# The questions.
#
# Each is a real question whose answer exists BOTH in the store and somewhere in
# the repository — otherwise arm B could not answer at all and the counterfactual
# would be meaningless. `truth` lists substrings a correct answer must contain;
# `kind` records where the answer lives, because that is what decides the price.
# --------------------------------------------------------------------------- #
QUESTIONS = [
    dict(
        q="Z którego pliku aplikacja Mini Verbigem odczytuje wersję APK do "
          "auto-update i jakie pola ten plik zawiera?",
        truth=["version.json", "apkUrl"],
        kind="obecny stan repo",
    ),
    dict(
        q="Jakie dwa flavor ma projekt Android i jaki applicationId ma wersja "
          "do instalacji poza sklepem?",
        truth=["com.verbigem.app.sideload"],
        kind="obecny stan repo",
    ),
    dict(
        q="Który plik w mini definiuje reguły dostępu do Firestore i jak nazywa się "
          "wywołanie chroniące pola przed zapisem z klienta?",
        truth=["firestore.rules", "affectedKeys"],
        kind="obecny stan repo",
    ),
    dict(
        q="Dlaczego konto z aktywną subskrypcją Paddle mogło nie widzieć reklam? "
          "Co ustawiała gałąź subscription w paddleWebhook?",
        truth=["400"],
        kind="historia — zakopane w logu pamięci",
    ),
    dict(
        q="Dlaczego build z Play pokazywał 4 z 6 języków interfejsu?",
        truth=["split"],
        kind="historia — zakopane w logu pamięci",
    ),
]


# --------------------------------------------------------------------------- #
# Tools — read-only, workspace-confined
# --------------------------------------------------------------------------- #

class Escape(Exception):
    """A tool asked for a path outside the workspace."""


def resolve(path: str, root: str) -> str:
    """Absolute path inside the workspace, or refuse.

    The confinement is the whole safety story of this script: it is handed a real
    repository and a model, and the only reason that is acceptable is that no tool
    here can read or write outside `root`.
    """
    candidate = os.path.abspath(os.path.join(root, path or "."))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise Escape(f"{path!r} is outside the workspace")
    return candidate


def walk_files(root: str, suffix: str = ""):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if suffix and not name.endswith(suffix):
                continue
            yield os.path.join(dirpath, name)


def tool_grep(args: dict, root: str) -> str:
    pattern = str(args.get("pattern") or "")
    suffix = str(args.get("glob") or "")
    if not pattern:
        return "error: pattern is required"
    try:
        rx = re.compile(pattern, re.I)
    except re.error as exc:
        return f"error: bad regex ({exc})"

    started = time.time()
    hits: list[str] = []
    scanned = 0
    truncated = False
    for path in walk_files(root, suffix):
        if time.time() - started > GREP_BUDGET_SECONDS:
            truncated = True
            break
        try:
            if os.path.getsize(path) > GREP_MAX_FILE_BYTES:
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                scanned += 1
                for n, line in enumerate(fh, 1):
                    if rx.search(line):
                        rel = os.path.relpath(path, root).replace("\\", "/")
                        hits.append(f"{rel}:{n}: {line.rstrip()[:200]}")
                        if len(hits) >= GREP_MAX_MATCHES:
                            truncated = True
                            break
        except OSError:
            continue
        if len(hits) >= GREP_MAX_MATCHES:
            break

    note = f" ({GREP_MAX_MATCHES} matches is the cap, more exist)" if truncated else ""
    if not hits:
        return f"no matches for {pattern!r} in {scanned} files{note}"
    return f"{len(hits)} matches in {scanned} files{note}\n" + "\n".join(hits)


def tool_read(args: dict, root: str) -> str:
    path = resolve(str(args.get("path") or ""), root)
    offset = max(1, int(args.get("offset") or 1))
    limit = min(READ_MAX_LINES, max(1, int(args.get("limit") or READ_MAX_LINES)))
    if not os.path.isfile(path):
        return f"error: no such file: {args.get('path')}"
    out, chars, truncated = [], 0, False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if n < offset:
                    continue
                if n >= offset + limit:
                    truncated = True
                    break
                chunk = f"{n:>5}: {line.rstrip()[:400]}"
                chars += len(chunk)
                if chars > READ_MAX_CHARS:
                    truncated = True
                    break
                out.append(chunk)
    except OSError as exc:
        return f"error: {exc}"
    note = " (truncated)" if truncated else ""
    return f"{args.get('path')} lines {offset}+{note}\n" + "\n".join(out)


def tool_list(args: dict, root: str) -> str:
    path = resolve(str(args.get("path") or "."), root)
    if not os.path.isdir(path):
        return f"error: no such directory: {args.get('path')}"
    entries = sorted(os.listdir(path))[:LIST_MAX_ENTRIES]
    return "\n".join(entries) or "(empty)"


def run_tool(name: str, args: dict, root: str) -> str:
    try:
        if name == "grep":
            return tool_grep(args, root)
        if name == "read":
            return tool_read(args, root)
        if name == "list_dir":
            return tool_list(args, root)
        return f"error: unknown tool {name}"
    except Escape as exc:
        return f"error: {exc}"
    except Exception as exc:                                    # noqa: BLE001
        return f"error: {type(exc).__name__} {exc}"


TOOLS = [
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search file contents in the workspace. Returns file:line: text.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Python regular expression"},
            "glob": {"type": "string", "description": "Optional filename suffix, e.g. .kt"},
        }, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "read",
        "description": "Read a file from the workspace, with line numbers.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "First line, 1-based"},
            "limit": {"type": "integer", "description": "How many lines"},
        }, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List a directory in the workspace.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
]


# --------------------------------------------------------------------------- #
# The agent loop
# --------------------------------------------------------------------------- #

RETRY_DELAYS = (5.0, 15.0, 45.0)


def call(model: str, messages: list, timeout: float) -> dict:
    """One completion, retrying the failures that are the provider's and not ours.

    A single 429 killed a whole measurement run on 2026-09-16: every question
    reported `HTTP 429` and the summary said "nothing measurable". A transient
    provider throttle must not be able to look like a finding about the store.
    """
    payload = json.dumps({
        "model": model,
        "max_tokens": 700,
        "temperature": 0,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
    }).encode("utf-8")
    last: Exception | None = None
    for attempt, delay in enumerate((0.0,) + RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        req = urllib.request.Request(
            API, data=payload,
            headers={"Authorization": "Bearer " + os.environ.get("OPENROUTER_API_KEY", ""),
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
    raise last if last else RuntimeError("no attempt was made")


def agent(model: str, question: str, context: str | None, root: str,
          max_turns: int, timeout: float) -> dict:
    """One arm. Returns tokens, tool calls, wall seconds and the final answer.

    Tokens come from the API's `usage`, summed across every turn, because that is
    what is actually billed — a prompt that grows by a tool result costs more on the
    next turn, and that growth is the point of the comparison.
    """
    user = question if context is None else f"{WRAPPER.format(block=context)}\n\n{question}"
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user}]

    prompt_tokens = completion_tokens = total_tokens = 0
    calls = 0
    answer = ""
    first_prompt_tokens = None
    started = time.time()
    error = ""

    for _ in range(max_turns):
        try:
            data = call(model, messages, timeout)
        except urllib.error.HTTPError as exc:
            error = f"HTTP {exc.code} {exc.read()[:200].decode('utf-8', 'replace')}"
            break
        except Exception as exc:                                # noqa: BLE001
            error = f"{type(exc).__name__} {exc}"
            break

        usage = data.get("usage") or {}
        prompt_tokens += int(usage.get("prompt_tokens") or 0)
        completion_tokens += int(usage.get("completion_tokens") or 0)
        total_tokens += int(usage.get("total_tokens") or 0)
        if first_prompt_tokens is None:
            first_prompt_tokens = int(usage.get("prompt_tokens") or 0)

        choices = data.get("choices") or []
        if not choices:
            error = "no choices: " + json.dumps(data)[:200]
            break
        msg = choices[0].get("message") or {}
        wanted = msg.get("tool_calls") or []

        turn = {"role": "assistant", "content": msg.get("content") or ""}
        if wanted:
            turn["tool_calls"] = wanted
        messages.append(turn)

        if not wanted:
            answer = msg.get("content") or ""
            break

        for tc in wanted:
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            try:
                targs = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                targs = {}
            if not isinstance(targs, dict):
                targs = {}
            calls += 1
            messages.append({"role": "tool", "tool_call_id": tc.get("id") or "",
                             "content": run_tool(name, targs, root)})

    return {"tokens": total_tokens, "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens, "calls": calls,
            "seconds": time.time() - started, "answer": answer,
            "first_prompt_tokens": first_prompt_tokens or 0, "error": error}


def answered(answer: str, truth: list) -> bool:
    low = answer.casefold()
    return bool(answer) and all(t.casefold() in low for t in truth)


# --------------------------------------------------------------------------- #

def block_for(question: str) -> str | None:
    """The block the hook would inject, with suppression off.

    Suppression is a 45-minute repeat window; leaving it on would let a note hidden
    by the window look like a note the store does not have.
    """
    hook.load_state = lambda *a, **k: {"notes": {}}
    hook.save_state = lambda *a, **k: None
    return hook.build_context(question, session_id="savings")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    ap.add_argument("--only", default="", help="question number, 1-based")
    ap.add_argument("--max-turns", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set — no model, no measurement",
              file=sys.stderr)
        return 2
    root = os.path.abspath(os.path.expanduser(args.workspace))
    if not os.path.isdir(root):
        print(f"no workspace at {root}", file=sys.stderr)
        return 2

    picked = [(i, q) for i, q in enumerate(QUESTIONS, 1)
              if not args.only or str(i) == args.only]
    if not picked:
        print(f"no question matches --only {args.only}", file=sys.stderr)
        return 2

    print(f"model      : {args.model}")
    print(f"workspace  : {root}")
    print(f"max turns  : {args.max_turns}")
    print(f"caps       : grep {GREP_MAX_MATCHES} matches / {GREP_BUDGET_SECONDS:.0f}s, "
          f"read {READ_MAX_LINES} lines\n")

    rows = []
    for i, item in picked:
        block = block_for(item["q"])
        ids = hook.injected_ids(block) if block else []
        print(f"### {i}. {item['q']}")
        print(f"    rodzaj odpowiedzi: {item['kind']}")
        print(f"    blok: {len(block or '')} znaków, notatki: {ids or 'brak'}")
        if not block:
            print("    UWAGA: hook nic nie wstrzyknął — ramię A nie ma czym się "
                  "różnić od B, pomiar dla tego pytania nic nie znaczy\n")
            continue

        a = agent(args.model, item["q"], block, root, args.max_turns, args.timeout)
        b = agent(args.model, item["q"], None, root, args.max_turns, args.timeout)
        if a["error"] or b["error"]:
            print(f"    BŁĄD A={a['error'][:120]!r} B={b['error'][:120]!r}\n")
            continue

        ok_a, ok_b = answered(a["answer"], item["truth"]), answered(b["answer"], item["truth"])
        block_tokens = max(0, a["first_prompt_tokens"] - b["first_prompt_tokens"])
        rows.append({
            "n": i, "q": item["q"], "kind": item["kind"], "ids": ids,
            "block_chars": len(block), "block_tokens": block_tokens,
            "a": a, "b": b, "ok_a": ok_a, "ok_b": ok_b,
            "d_tokens": b["tokens"] - a["tokens"],
            "d_calls": b["calls"] - a["calls"],
            "d_seconds": b["seconds"] - a["seconds"],
        })

        print(f"    A  tokeny={a['tokens']:>6}  wywołań={a['calls']:>2}  "
              f"{a['seconds']:>6.1f}s  trafione={'tak' if ok_a else 'NIE'}")
        print(f"    B  tokeny={b['tokens']:>6}  wywołań={b['calls']:>2}  "
              f"{b['seconds']:>6.1f}s  trafione={'tak' if ok_b else 'NIE'}")
        print(f"    różnica  tokeny={b['tokens'] - a['tokens']:>+6}  "
              f"wywołań={b['calls'] - a['calls']:>+3}  "
              f"czas={b['seconds'] - a['seconds']:>+6.1f}s")
        # The answers are printed because the number is only as good as the run it
        # came from: a "saving" measured against an arm that answered nonsense is not
        # a saving, and that must be visible without re-running anything.
        for label, run, ok in (("A", a, ok_a), ("B", b, ok_b)):
            body = " ".join((run["answer"] or "").split())[:180]
            print(f"    {label} {'OK ' if ok else 'ZLE'} {body!r}")
        if not ok_b:
            print("    B NIE ODPOWIEDZIAŁ POPRAWNIE — magazyn jest jedynym miejscem, "
                  "gdzie ten fakt istnieje; oszczędność jest nieograniczona, "
                  "nie zmierzona")
        print()

    if not rows:
        print("nothing measurable")
        return 1

    # Averaging every row into one "saving" would be the same mistake as the counter
    # that could not say no: arm B's failure to find an answer is not a saving, it is
    # a failed search, and it drags the mean around. Three classes, one of which is
    # an actual saving.
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else 0      # noqa: E731
    paired = [r for r in rows if r["ok_a"] and r["ok_b"]]
    only_a = [r for r in rows if r["ok_a"] and not r["ok_b"]]
    missed = [r for r in rows if not r["ok_a"]]

    print("=" * 66)
    print(f"pytań zmierzonych            : {len(rows)}")
    print(f"koszt bloku (mediana)        : {med([r['block_tokens'] for r in rows])} tokenów"
          f"  ({med([r['block_chars'] for r in rows])} znaków)")
    print()
    print("Trzy klasy, bo tylko pierwsza jest oszczędnością:")
    print(f"  1. oba ramiona odpowiedziały : {len(paired)}")
    print(f"  2. tylko A — B nie znalazł   : {len(only_a)}"
          "   <- magazyn jest JEDYNYM źródłem")
    print(f"  3. A też nie trafiło         : {len(missed)}"
          "   <- blok obok tematu: koszt bez zysku")
    print()
    if paired:
        print(f"  klasa 1 — oszczędność na pytanie (n={len(paired)}):")
        print(f"    tokeny    A {med([r['a']['tokens'] for r in paired]):>6}"
              f"   B {med([r['b']['tokens'] for r in paired]):>6}"
              f"   różnica {med([r['d_tokens'] for r in paired]):>+6}")
        print(f"    wywołania A {med([r['a']['calls'] for r in paired]):>6}"
              f"   B {med([r['b']['calls'] for r in paired]):>6}"
              f"   różnica {med([r['d_calls'] for r in paired]):>+6}")
        print(f"    sekundy   A {med([r['a']['seconds'] for r in paired]):>6.1f}"
              f"   B {med([r['b']['seconds'] for r in paired]):>6.1f}"
              f"   różnica {med([r['d_seconds'] for r in paired]):>+6.1f}")
    if only_a:
        print(f"  klasa 2 — bez magazynu nie ma odpowiedzi (n={len(only_a)}):")
        for r in only_a:
            print(f"    #{r['n']}  A {r['a']['tokens']:>5} tok / {r['a']['calls']} wyw."
                  f"   vs   B {r['b']['tokens']:>5} tok / {r['b']['calls']} wyw."
                  "   — i nie odpowiedziało")
    if missed:
        print(f"  klasa 3 — blok nie zawierał odpowiedzi (n={len(missed)}):")
        for r in missed:
            print(f"    #{r['n']}  A {r['a']['tokens']:>5} tok / {r['a']['calls']} wyw."
                  f"   vs   B {r['b']['tokens']:>5} tok / {r['b']['calls']} wyw."
                  f"   czoło bloku: {r['ids'][0] if r['ids'] else 'brak'}")

    if args.json:
        print()
        print(json.dumps([{k: v for k, v in r.items() if k not in ("a", "b")} | {
            "a": {k: v for k, v in r["a"].items() if k != "answer"},
            "b": {k: v for k, v in r["b"].items() if k != "answer"},
        } for r in rows], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
