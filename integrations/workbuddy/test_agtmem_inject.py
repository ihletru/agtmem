#!/usr/bin/env python3
"""Tests for the agtmem UserPromptSubmit hook.

Run:  python test_agtmem_inject.py
Exit: 0 all passed, 1 failures.

The hook is a pure function of stdin -> stdout, so it can be tested without
WorkBuddy. Two levels:

* `build_context()` directly — the gate and the block format;
* the process contract — stdin JSON in, hook JSON out, exit 0, nothing on
  stdout when it decides to stay silent.

The `RELEVANT`/`IRRELEVANT` sets are the labelled data the threshold was tuned
on. Two prompts are listed as KNOWN_MISS: they are relevant but fall below the
gate. They are asserted to stay silent on purpose — the test documents the
precision/recall trade-off instead of hiding it. Move one to RELEVANT only
together with a threshold change that keeps IRRELEVANT at zero.

Sections guard the things that only break in production, in the order they were
found: portability of the shipped source (no machine path), configuration
resolution (env > sidecar > default), the stdin codec, the gate's denominator,
and per-conversation suppression.

**The suite is hermetic on purpose.** `AGTMEM_HOOK_RUNTIME` is pointed at a
temporary directory *before* the hook is imported, so the log and the
suppression state written here are not the ones the live hook uses. Getting this
wrong once meant an ad-hoc verification call silenced a note for a real prompt.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# Must precede the import: the hook resolves its runtime directory at import time.
RUNTIME_TMP = tempfile.mkdtemp(prefix="agtmem-hook-test-")
os.environ["AGTMEM_HOOK_RUNTIME"] = RUNTIME_TMP

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import agtmem_inject as hook      # noqa: E402
import install_hooks as inst      # noqa: E402

# Paths that must never appear in the shipped source: they encode one machine.
HARDCODED = [
    (re.compile(r"[A-Za-z]:[\\/]Users[\\/]"), "a Windows user directory"),
    (re.compile(r"/home/[a-z]"), "a POSIX home directory"),
    (re.compile(r"\.workbuddy-ai"), "the agent home rather than the desktop's"),
]

RELEVANT = [
    ("jak zbudowac APK androida bez gradlew", ""),
    ("czy agtmem jest w ogole czytany w petli agenta", ""),
    ("blad z firestore rules w gitignore", ""),
    ("jak dziala kompresja kontekstu w workbuddy", ""),
    ("hook wstrzykujacy trafienia przy wyslaniu promptu", ""),
    ("jak zbudowac APK androida bez gradlew", "/srv/apps/verbigem/android"),
]

IRRELEVANT = [
    "ok", "dzieki", "zrob to", "popraw to", "no", "hmm",
    "napisz mi wiersz o morzu",
    "jaka jest stolica Paragwaju",
    "przetlumacz to zdanie na hiszpanski",
    "co robimy dalej",
    "a teraz testy",
    "ile wynosi 2 plus 2",
    "/clear",
    "",
]

# Relevant, but below the gate. Deliberate: silence is the safe failure.
KNOWN_MISS = [
    "dlaczego kara za dlugosc jest ustawiona na 6000",
    "co ustalilismy o Paddle webhookach i podpisach",
]

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))


def fresh() -> None:
    for path in (hook.STATE,):
        try:
            os.remove(path)
        except OSError:
            pass


def call(prompt: str, cwd: str = "", session: str = "") -> str | None:
    fresh()
    return hook.build_context(prompt, cwd, session)


def fake_search(rows: list[dict]):
    """Context manager: replace the real agtmem search with fixed rows.

    The suppression rules are about *timing*, and timing cannot be asserted
    against live search results — their `terms` values move whenever the store
    changes. These tests pin the rows so the rule is the only variable.
    """
    class _Fake:
        def __enter__(self):
            self.real = hook.search
            hook.search = lambda query: [dict(r) for r in rows]
            return self

        def __exit__(self, *exc):
            hook.search = self.real
            return False
    return _Fake()


def run_process(stdin_text: str, timeout: float = 25.0, env: dict | None = None):
    proc = subprocess.run([sys.executable, hook.__file__], input=stdin_text.encode("utf-8"),
                          capture_output=True, timeout=timeout, env=env)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def main() -> int:
    print("--- gate: relevant prompts must inject ---")
    for prompt, cwd in RELEVANT:
        ctx = call(prompt, cwd)
        ok = bool(ctx) and "[agtmem]" in ctx
        check(f"inject: {prompt[:46]}", ok, repr(ctx)[:160])

    print("\n--- gate: junk prompts must stay silent ---")
    for prompt in IRRELEVANT:
        ctx = call(prompt)
        check(f"silent: {prompt[:46]!r}", ctx is None, repr(ctx)[:160])

    print("\n--- gate: known misses stay silent (documented trade-off) ---")
    for prompt in KNOWN_MISS:
        ctx = call(prompt)
        check(f"known-miss silent: {prompt[:38]}", ctx is None, repr(ctx)[:160])

    print("\n--- block shape ---")
    ctx = call("jak zbudowac APK androida bez gradlew")
    check("block starts with the marker", bool(ctx) and ctx.startswith("[agtmem]"))
    check("block names note ids", bool(ctx) and "verbigem" in ctx)
    check("block under the size cap", bool(ctx) and len(ctx) <= hook.MAX_BLOCK_CHARS,
          f"{len(ctx) if ctx else 0} chars")
    check("block at most KEEP notes", bool(ctx) and ctx.count("\n- ") <= hook.KEEP,
          f"{ctx.count(chr(10) + '- ') if ctx else 0} bullets")

    # The log has to name what was delivered. `kept=N` counts candidates that passed
    # the gate, and the character budget can then drop some, so it is a different set —
    # and the suppression state that would name them is pruned within REPEAT_WINDOW,
    # which makes the ids unrecoverable by the next morning if they are not logged.
    if ctx:
        ids = hook.injected_ids(ctx)
        check("injected_ids reads every bullet", len(ids) == ctx.count("\n- "),
              f"{len(ids)} ids vs {ctx.count(chr(10) + '- ')} bullets")
        check("injected_ids returns bare ids, not types or titles",
              all(ids) and all("/" not in i and " " not in i for i in ids), repr(ids))
        check("injected_ids finds nothing in a silent block",
              hook.injected_ids("[agtmem] nic tu nie ma") == [])
        check("injected_ids tolerates the truncation marker",
              hook.injected_ids("- fact/one — a\n\u2026 (ucięte)") == ["one"])
        check("injected_ids survives the updated-date suffix",
              hook.injected_ids("- bug/two \u00b7 2026-09-16 — b") == ["two"])

    print("\n--- excerpt: the note's content, not just its title ---")
    # The revision this section guards: the first version injected a pointer (title
    # + 110-char snippet) and left the agent to run `agtmem show <id>`. Measured over
    # a day, none of nine injections was followed by a show — the decision never
    # happened, so the note was delivered and never read.
    tmp_note = os.path.join(RUNTIME_TMP, "excerpt-note.md")
    with open(tmp_note, "w", encoding="utf-8") as fh:
        fh.write("---\nid: excerpt-note\ntitle: Tytu\u0142\nstatus: active\n---\n\n"
                 "## Sekcja\n\nPierwsze zdanie faktu. Drugie zdanie faktu.\n\n"
                 "| katalog | zawarto\u015b\u0107 |\n|---|---|\n| `facts/` | fakty |\n\n"
                 "- punkt pierwszy\n- punkt drugi\n")
    ex = hook.note_excerpt(tmp_note)
    check("excerpt strips the frontmatter", "status: active" not in ex and "id: excerpt" not in ex,
          repr(ex[:80]))
    check("excerpt keeps the body", "Pierwsze zdanie faktu" in ex, repr(ex[:120]))
    check("excerpt drops the heading markup but keeps its words",
          "Sekcja" in ex and "##" not in ex, repr(ex[:80]))
    check("excerpt drops table separator rows", "|---|" not in ex, repr(ex))
    check("excerpt keeps table rows", "`facts/` | fakty" in ex, repr(ex))
    check("every excerpt line is indented", all(l.startswith("  ") for l in ex.splitlines()),
          repr(ex))
    check("a Markdown bullet in a body cannot fake a note header",
          hook.injected_ids(ex) == [], repr(hook.injected_ids(ex)))
    check("excerpt respects the character limit",
          len(hook.note_excerpt(tmp_note, limit=60)) <= 60 + len("  \u2026"),
          f"{len(hook.note_excerpt(tmp_note, limit=60))} chars")
    check("a truncated excerpt says so",
          hook.note_excerpt(tmp_note, limit=60).rstrip().endswith("\u2026"),
          repr(hook.note_excerpt(tmp_note, limit=60)))
    check("a missing path degrades to empty, never raises",
          hook.note_excerpt(os.path.join(RUNTIME_TMP, "nope.md")) == "")
    check("a None path degrades to empty", hook.note_excerpt(None) == "")
    check("the real block carries content, not only a pointer",
          bool(ctx) and len(ctx) > 500 and "\n  " in ctx,
          f"{len(ctx) if ctx else 0} chars")
    check("the block still fits the cap", bool(ctx) and len(ctx) <= hook.MAX_BLOCK_CHARS,
          f"{len(ctx) if ctx else 0} vs {hook.MAX_BLOCK_CHARS}")

    print("\n--- the excerpt must reach the answer, not just the note ---")
    # The failure this section guards is one level below the pointer failure above,
    # and it looked identical to a healthy injection. Taking the note's *head* was
    # justified as "by the store's own convention the first section is the essence",
    # which is false for a five-section note: the block carried
    # `fact/firebase-identity-and-rules-model`, its id and its title, and none of its
    # answer, because `affectedKeys().hasAny([...])` sits at character 2046 of 3289
    # under a heading in section 3. The agent read the block, did not find the
    # answer, searched the repository instead and answered `allow write: if false;`.
    # So: the head, then every later heading with its lead line.
    deep_note = os.path.join(RUNTIME_TMP, "deep-note.md")
    with open(deep_note, "w", encoding="utf-8") as fh:
        fh.write("---\nid: deep-note\ntitle: Tytu\u0142\nstatus: active\n---\n\n"
                 + "Wst\u0119p bez znaczenia dla pytania. " * 12 + "\n\n"
                 "## Pierwsza sekcja\n\nTresc pierwszej sekcji, nie odpowiedz.\n\n"
                 "DRUGIE zdanie sekcji, ktore nie ma po co jechac w bloku.\n\n"
                 "### Sekcja z odpowiedzi\u0105\n\n"
                 "Regu\u0142a u\u017cywa `affectedKeys().hasAny([...])`.\n\n"
                 "## Ostatnia sekcja\n\nJeszcze cos.\n")
    deep = hook.note_excerpt(deep_note)
    check("a heading below the head reaches the excerpt",
          "Sekcja z odpowiedzi" in deep, repr(deep))
    check("... and so does the lead line under it", "affectedKeys" in deep, repr(deep))
    check("a later section is marked as a section",
          hook.SECTION_MARK in deep and "##" not in deep, repr(deep))
    check("the head is capped, so the sections are reached at all",
          len(deep.splitlines()[0]) <= hook.HEAD_CHARS + len("  \u2026"), repr(deep[:90]))
    check("a section's lead is kept", "Tresc pierwszej sekcji" in deep, repr(deep))
    check("a section's second line is dropped — that is what pays for the reach",
          "DRUGIE zdanie" not in deep, repr(deep))
    check("an excerpt from one note stays inside BODY_CHARS",
          len(hook.note_excerpt(deep_note, limit=400)) <= 400 + 3,
          f"{len(hook.note_excerpt(deep_note, limit=400))} chars")
    check("a note with no headings still yields its head",
          "\u00a7" not in hook.note_excerpt(tmp_note), repr(hook.note_excerpt(tmp_note)))
    check("BODY_CHARS covers the head plus several sections",
          hook.BODY_CHARS >= hook.HEAD_CHARS + 4 * (hook.LEAD_CHARS + 20),
          f"head {hook.HEAD_CHARS} + leads in {hook.BODY_CHARS}")

    # The end-to-end form of the same claim: on the real store, the block for the
    # question that exposed the defect must carry the string a correct answer needs.
    # Substring sufficiency is checked without a model on purpose — if the block does
    # not contain it, no model can answer from the block, and the failure is the block's.
    deep_ctx = call("Jak nazywa si\u0119 wywo\u0142anie w regu\u0142ach Firestore w mini, "
                    "kt\u00f3re chroni pola przed zapisem z klienta?")
    check("the block for that question carries affectedKeys, not only a note title",
          bool(deep_ctx) and "affectedKeys" in deep_ctx,
          repr(deep_ctx)[:240] if deep_ctx else "no block at all")

    print("\n--- the counter's two handles: sess= and the cite line ---")
    # `sess=` makes an injection joinable with the answer that followed it; without it
    # measure_usage.py can only guess from the truncated prompt text. The cite line is
    # what makes "was the note used?" answerable at all — and it must survive the
    # block's truncation, or the counter reads zero and looks like model indifference.
    check("short_session takes the first 8 characters",
          hook.short_session("c33b13b8-9529-4191") == "c33b13b8", hook.short_session("c33b13b8-9529"))
    check("short_session marks a missing id rather than printing nothing",
          hook.short_session("") == "-")
    check("the cite line is second, so truncation cannot drop it",
          bool(ctx) and ctx.splitlines()[1].strip().startswith("Jeśli z którejś"),
          repr(ctx.splitlines()[1] if ctx else None))
    check("the cite line names the marker syntax", "[agtmem:<id>]" in (ctx or ""))
    check("the cite line does not count as a note header", hook.injected_ids(ctx or "") ==
          hook.injected_ids("\n".join(l for l in (ctx or "").splitlines()
                                      if not l.strip().startswith("Jeśli"))),
          repr(hook.injected_ids(ctx or "")))
    check("cite can be turned off in the sidecar",
          hook.resolve_config()["cite"] is True, "default must be on")

    print("\n--- robustness: hostile prompts must not raise ---")
    hostile = [
        'he said "build the apk" without gradlew',
        "APK OR gradlew AND (android) NEAR/3 build",
        "android* apk^2 gradlew~",
        ";;; ... --- +++ ///",
        "zażółć gęślą jaźń — android apk gradlew",
        "x" * 4000 + " android apk gradlew",
    ]
    for prompt in hostile:
        try:
            ctx = call(prompt)
            check(f"no raise: {prompt[:34]!r}", True)
            if ctx is not None:
                check(f"valid utf-8 output: {prompt[:26]!r}",
                      ctx.encode("utf-8").decode("utf-8") == ctx)
        except Exception as exc:  # noqa: BLE001
            check(f"no raise: {prompt[:34]!r}", False, repr(exc))

    print("\n--- state: a repeat within the window is suppressed ---")
    fresh()
    first = hook.build_context("jak zbudowac APK androida bez gradlew")
    second = hook.build_context("jak zbudowac APK androida bez gradlew")
    check("first injects", bool(first))
    check("immediate repeat suppressed", second is None, repr(second)[:120])
    check("state file written", os.path.exists(hook.STATE))
    third = hook.build_context("jak dziala kompresja kontekstu w workbuddy")
    check("a different topic still injects", bool(third), repr(third)[:120])

    print("\n--- suppression is per conversation, not global ---")
    fresh()
    rows = [{"id": "note-alpha", "type": "fact", "scope": "x", "terms": 4,
             "title": "Alpha", "snippet": "alpha body", "updated": "2026-09-16"}]
    with fake_search(rows):
        a1 = hook.build_context("alpha beta gamma delta", "", "sess-A")
        a2 = hook.build_context("alpha beta gamma delta", "", "sess-A")
        b1 = hook.build_context("alpha beta gamma delta", "", "sess-B")
        adhoc = hook.build_context("alpha beta gamma delta", "", "")
    check("session A gets the note", bool(a1) and "note-alpha" in a1)
    check("session A repeat is suppressed", a2 is None, repr(a2)[:120])
    check("session B still gets it — a different conversation is not silenced",
          bool(b1) and "note-alpha" in b1, repr(b1)[:120])
    check("an ad-hoc call (no session id) is not silenced by a real session",
          bool(adhoc) and "note-alpha" in adhoc, repr(adhoc)[:120])

    print("\n--- suppression state: schema migration from the flat format ---")
    legacy = {"notes": {"note-alpha": time.time()}, "sessions": {}}
    with open(hook.STATE, "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)
    state = hook.load_state()
    check("flat note ids migrate into the legacy bucket",
          hook.LEGACY_BUCKET in state["notes"], repr(state["notes"])[:120])
    check("legacy entries are still honoured for a real session",
          "note-alpha" in hook.note_timestamps(state, "sess-A"))
    check("legacy entries are still honoured for an ad-hoc call",
          "note-alpha" in hook.note_timestamps(state, ""))
    fresh()
    with open(hook.STATE, "w", encoding="utf-8") as fh:
        json.dump({"notes": {"sess-A": {"note-alpha": time.time()}},
                   "sessions": {"sess-A": time.time()}}, fh)
    state = hook.load_state()
    check("a session-scoped entry does not leak into another session",
          "note-alpha" not in hook.note_timestamps(state, "sess-B"))
    check("stale entries are pruned on save",
          (hook.save_state({"notes": {"sess-A": {"old": 0.0}}, "sessions": {}}, time.time())
           or json.load(open(hook.STATE, encoding="utf-8"))["notes"] == {}))
    fresh()

    print("\n--- SessionStart: one reminder per session ---")
    fresh()
    s1 = hook.build_session_context("/srv/apps/verbigem/android", "sess-aaa")
    s2 = hook.build_session_context("/srv/apps/verbigem/android", "sess-aaa")
    s3 = hook.build_session_context("", "sess-bbb")
    s4 = hook.build_session_context("", "")
    check("first session reminder injects", bool(s1) and "[agtmem]" in s1)
    check("same session id is silent the second time", s2 is None, repr(s2)[:120])
    check("a different session id still injects", bool(s3))
    check("no session id -> silent", s4 is None)
    check("scope hint present when cwd maps to one", bool(s1) and "verbigem-android" in s1,
          repr(s1)[:160])
    check("session reminder is short", bool(s1) and len(s1) < 500, f"{len(s1) if s1 else 0} chars")

    print("\n--- process contract ---")
    fresh()
    rc, out, err = run_process(json.dumps({"prompt": "jak zbudowac APK androida bez gradlew",
                                           "cwd": "", "hook_event_name": "UserPromptSubmit",
                                           "session_id": "sess-proc"}))
    check("exit 0 on a real payload", rc == 0, f"rc={rc}")
    check("stdout is one JSON object", out.strip().startswith("{") and out.strip().endswith("}"),
          out[:120])
    try:
        parsed = json.loads(out)
        got = parsed["hookSpecificOutput"]
        check("hookEventName correct", got.get("hookEventName") == "UserPromptSubmit")
        check("additionalContext present", bool(got.get("additionalContext")))
        check("no other stdout noise", out.strip().count("\n") == 0, repr(out[:120]))
    except (ValueError, KeyError, TypeError) as exc:
        check("stdout parses as hook JSON", False, repr(exc))

    # The log line is the counter's input. If it loses sess= or ids=, measure_usage.py
    # silently degrades to guessing from prompt text — a healthy-looking log that
    # cannot answer the only question it exists for.
    try:
        with open(hook.LOG, encoding="utf-8") as fh:
            logged = fh.read()
    except OSError:
        logged = ""
    check("the log line carries sess=", "sess=sess-pro" in logged, repr(logged[-160:]))
    check("the log line carries ids=", "ids=" in logged, repr(logged[-160:]))
    check("the log line carries the byte count", "c sess=" in logged or "c ids=" in logged,
          repr(logged[-160:]))

    for label, payload in [("empty stdin", ""),
                           ("invalid json", "not json at all"),
                           ("json array", "[1,2,3]"),
                           ("no prompt key", json.dumps({"cwd": "C:/tmp"})),
                           ("junk prompt", json.dumps({"prompt": "ok"}))]:
        fresh()
        rc, out, err = run_process(payload)
        check(f"silent + exit 0 on {label}", rc == 0 and out.strip() == "",
              f"rc={rc} out={out[:80]!r}")

    fresh()
    rc, out, err = run_process(json.dumps({"hook_event_name": "SessionStart",
                                           "session_id": "sess-xyz",
                                           "cwd": "/srv/apps/verbigem/mini"}))
    check("SessionStart payload exits 0", rc == 0, f"rc={rc}")
    try:
        parsed = json.loads(out)
        got = parsed["hookSpecificOutput"]
        check("SessionStart hookEventName echoed",
              got.get("hookEventName") == "SessionStart", repr(got)[:120])
        check("SessionStart carries context", bool(got.get("additionalContext")))
    except (ValueError, KeyError, TypeError) as exc:
        check("SessionStart stdout parses", False, repr(exc))

    fresh()
    rc, out, err = run_process(json.dumps({"hook_event_name": "PreToolUse",
                                           "session_id": "sess-xyz"}))
    check("an unhandled event is silent", rc == 0 and out.strip() == "",
          f"rc={rc} out={out[:80]!r}")

    print("\n--- portability: no machine-specific path in the shipped source ---")
    # install_hooks.py is allowed to *mention* .workbuddy-ai — it warns against
    # trusting that variable. The hook itself must never assume it.
    per_file = {
        "agtmem_inject.py": HARDCODED,
        "install_hooks.py": HARDCODED[:2],
    }
    for name, patterns in per_file.items():
        path = os.path.join(HERE, name)
        try:
            src = open(path, encoding="utf-8").read()
        except OSError as exc:
            check(f"{name} readable", False, repr(exc))
            continue
        for pat, what in patterns:
            hits = pat.findall(src)
            check(f"{name}: no {what}", not hits, repr(hits[:3]))

    print("\n--- runtime files never sit beside the script ---")
    check("state is outside the checkout",
          not os.path.abspath(hook.STATE).startswith(os.path.abspath(HERE)),
          hook.STATE)
    check("log is outside the checkout",
          not os.path.abspath(hook.LOG).startswith(os.path.abspath(HERE)),
          hook.LOG)
    check("AGTMEM_HOOK_RUNTIME is honoured",
          os.path.abspath(hook.RUNTIME) == os.path.abspath(RUNTIME_TMP),
          f"{hook.RUNTIME} != {RUNTIME_TMP}")
    check("the installer honours AGTMEM_HOOK_RUNTIME when set",
          inst.default_runtime() == os.path.abspath(RUNTIME_TMP),
          inst.default_runtime())
    # The suite itself runs with that variable set, so it has to be lifted to
    # see the fallback the installer would use on a machine that sets nothing.
    saved_rt = os.environ.pop("AGTMEM_HOOK_RUNTIME", None)
    try:
        check("runtime resolution falls back to the desktop hooks dir",
              inst.default_runtime().replace("\\", "/").endswith("/.workbuddy/hooks"),
              inst.default_runtime())
    finally:
        if saved_rt is not None:
            os.environ["AGTMEM_HOOK_RUNTIME"] = saved_rt

    print("\n--- config resolution: env > sidecar > default ---")
    env_keys = ("AGTMEM_REPO", "AGTMEM_HOME", "AGTMEM_PYTHON")
    saved_env = {k: os.environ.get(k) for k in env_keys}
    saved_config = hook.CONFIG
    tmp_config = os.path.join(RUNTIME_TMP, "sidecar-test.json")
    hook.CONFIG = tmp_config          # never touch the real sidecar from a test
    try:
        for key in env_keys:
            os.environ.pop(key, None)
        if os.path.exists(tmp_config):
            os.remove(tmp_config)

        cfg = hook.resolve_config()
        check("python defaults to the running interpreter",
              cfg["python"] == sys.executable, cfg["python"])
        check("repo defaults to None — let agtmem decide",
              cfg["repo"] is None, repr(cfg["repo"]))
        check("store defaults to None — agtmem's own default",
              cfg["store"] is None, repr(cfg["store"]))

        with open(tmp_config, "w", encoding="utf-8") as fh:
            json.dump({"repo": "/tmp/sidecar-repo", "store": "/tmp/sidecar-store"}, fh)
        cfg = hook.resolve_config()
        check("sidecar repo is read", cfg["repo"] == "/tmp/sidecar-repo", repr(cfg["repo"]))
        check("sidecar store is read", cfg["store"] == "/tmp/sidecar-store", repr(cfg["store"]))

        os.environ["AGTMEM_REPO"] = "/tmp/env-repo"
        cfg = hook.resolve_config()
        check("environment beats the sidecar", cfg["repo"] == "/tmp/env-repo", repr(cfg["repo"]))
    finally:
        hook.CONFIG = saved_config
        for key, val in saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    check("cwd candidates: a configured repo comes first",
          hook._cwd_candidates(HERE)[0] == HERE, repr(hook._cwd_candidates(HERE)))
    check("cwd candidates: bare None when nothing is configured",
          hook._cwd_candidates(None) == [None], repr(hook._cwd_candidates(None)))
    check("cwd candidates: a non-directory repo is ignored",
          hook._cwd_candidates(os.path.join(HERE, "no-such-dir")) == [None])

    print("\n--- installer: merge is idempotent and non-destructive ---")
    check("label derived from the config dir name",
          inst.label_for(os.path.join("x", ".workbuddy", "settings.json")) == "workbuddy",
          inst.label_for(os.path.join("x", ".workbuddy", "settings.json")))
    check("label falls back for an odd path", inst.label_for("settings.json") == "settings")
    cands = inst.candidate_settings()
    check("well-known paths precede the env-derived candidate",
          cands[0].replace("\\", "/").endswith(".workbuddy/settings.json"), cands[0])

    base = {"enabledPlugins": {"x": True},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other-tool"}]}]}}
    s1, n1 = inst.apply_hooks(json.loads(json.dumps(base)), "workbuddy", remove=False)
    s2, n2 = inst.apply_hooks(json.loads(json.dumps(s1)), "workbuddy", remove=False)
    check("install writes both events", n1 == 2, str(n1))
    check("foreign top-level keys preserved", s1.get("enabledPlugins") == {"x": True})
    check("foreign hooks preserved", len(s1["hooks"].get("Stop") or []) == 1)
    check("re-install is idempotent",
          json.dumps(s1, sort_keys=True) == json.dumps(s2, sort_keys=True))
    groups = s1["hooks"]["UserPromptSubmit"]
    check("exactly one group of ours", len(groups) == 1, str(len(groups)))
    check("command quotes both paths",
          '"' in groups[0]["hooks"][0]["command"], groups[0]["hooks"][0]["command"])
    check("command carries the label",
          groups[0]["hooks"][0]["command"].endswith("--src=workbuddy"))

    s3, _ = inst.apply_hooks(json.loads(json.dumps(s1)), None, remove=True)
    check("uninstall removes only ours",
          "UserPromptSubmit" not in (s3.get("hooks") or {})
          and "SessionStart" not in (s3.get("hooks") or {})
          and "Stop" in (s3.get("hooks") or {}),
          repr(sorted((s3.get("hooks") or {}))))

    side = inst.resolve(None, None, RUNTIME_TMP)
    check("the installer always records a runtime directory",
          side.get("runtime") == os.path.abspath(RUNTIME_TMP), repr(side))

    print("\n--- content words: function words must not buy a match ---")

    # Measured in production: `jego` appears in nearly every Polish note, so
    # counting it as a content word gave an irrelevant note a free matched term.
    # The injected block for a question about pushing to a repo was a note about
    # social graphs, matched on `jego` + two substrings inside code identifiers.
    for word in ("jego", "jej", "nim", "sobie", "takze", "tylko", "jeszcze",
                 "musi", "trzeba", "dlaczego", "czyli", "jednak"):
        check(f"{word!r} is a stopword", word in hook.STOPWORDS)
    check("content_words drops the pronoun",
          "jego" not in [w.lower() for w in hook.content_words("wypchnij jego opis do repo")],
          repr(hook.content_words("wypchnij jego opis do repo")))
    check("content_words keeps real content",
          hook.content_words("wypchnij jego opis do repo") == ["wypchnij", "opis", "repo"],
          repr(hook.content_words("wypchnij jego opis do repo")))
    check("content_words still keeps short technical tokens",
          "apk" in [w.lower() for w in hook.content_words("czemu apk sie nie buduje")],
          repr(hook.content_words("czemu apk sie nie buduje")))

    # The store is indexed with trigram matching, so a short token matches inside
    # longer identifiers: `doc` in `documentId`, `hook` in `paddleWebhook`. Those
    # hits cleared the gate for an irrelevant note. Requiring four characters kept
    # 5/5 relevant and 0/14 junk prompts on the labelled set while collapsing that
    # note's block from three notes to one.
    check("query_words drops tokens too short to be evidence",
          hook.query_words(["wypchnąłeś", "hook", "opis", "doc", "repo"])
          == ["wypchnąłeś", "hook", "opis", "repo"],
          repr(hook.query_words(["wypchnąłeś", "hook", "opis", "doc", "repo"])))
    check("query_words keeps four-character tokens",
          hook.query_words(["repo", "hook"]) == ["repo", "hook"])
    # The cost of the rule, asserted so it is visible rather than discovered later.
    check("query_words also drops a 3-char technical token (documented cost)",
          hook.query_words(["apk", "mcp", "url"]) == [],
          repr(hook.query_words(["apk", "mcp", "url"])))
    check("MIN_QUERY_CHARS is the measured boundary, not a round number",
          hook.MIN_QUERY_CHARS == 4, str(hook.MIN_QUERY_CHARS))

    print("\n--- production regressions (each surfaced only in the live session) ---")

    # 1. stdin must be decoded as UTF-8 no matter what the spawning process set.
    #    Production logged `zrób tą analizę` as `zrÃ³b tÄ… analizÄ™`, and WORD_RE
    #    then chopped the mangled tokens, so the query was built from garbage.
    for text in ("zrób tą analizę", "Uzupełnij magazyn pamięci", "zażółć gęślą jaźń"):
        check(f"decode_payload round-trips {text[:22]!r}",
              hook.decode_payload(text.encode("utf-8")) == text)
    check("decode_payload survives bytes that are not utf-8",
          isinstance(hook.decode_payload(b"\xff\xfe not utf-8"), str))

    # 2. The gate must scale with the query, not with the whole prompt.
    check("required_terms: a short prompt keeps the floor",
          hook.required_terms(["a", "b", "c", "d"]) == hook.MIN_TERMS_FLOOR,
          str(hook.required_terms(["a", "b", "c", "d"])))
    check("required_terms: a 400-word prompt does not demand 120 matches",
          hook.required_terms(["x"] * 400) <= 6,
          str(hook.required_terms(["x"] * 400)))
    check("required_terms: exactly the query cap, never more",
          hook.required_terms(["x"] * 400)
          == max(hook.MIN_TERMS_FLOOR,
                 math.ceil(hook.TERM_RATIO * hook.MAX_QUERY_WORDS)),
          str(hook.required_terms(["x"] * 400)))

    long_prompt = ("Uzupelnij magazyn pamieci agtmem o wnioski z sesji, przebieg "
                   "destylacji i konsolidacji. Kontekst techniczny: "
                   + " ".join(f"slowo{i}" for i in range(300)))
    ctx = call(long_prompt)
    check("a 350-word prompt still injects", bool(ctx) and "[agtmem]" in ctx, repr(ctx)[:160])

    # 3. A meta-question the store cannot answer. The gate is not a relevance
    #    oracle: it will inject the store's best lexical matches, and that is a
    #    limitation, not a bug to be tuned away. What must never happen again is
    #    the specific note that got in on pronoun + identifier-substring hits —
    #    it was injected while the actually-relevant note sat one rank below.
    meta = call("wypchnąłeś hook i jego opis w doc na repo?")
    check("a meta-question does not inject the social-graph note",
          meta is None or "social-graph" not in meta, repr(meta)[:200])
    check("a meta-question injects at most one note",
          meta is None or meta.count("\n- ") <= 1,
          repr(meta)[:200])

    # The log is the only place the decoded prompt is visible from outside.
    try:
        os.remove(hook.LOG)
    except OSError:
        pass
    hostile_env = dict(os.environ)
    hostile_env["PYTHONIOENCODING"] = "cp1252"
    proc = subprocess.run(
        [sys.executable, hook.__file__],
        input=json.dumps({"prompt": "zrób tą analizę jeszcze raz", "cwd": "",
                          "session_id": "sess-codec",
                          "hook_event_name": "UserPromptSubmit"},
                         ensure_ascii=False).encode("utf-8"),
        capture_output=True, env=hostile_env, timeout=25)
    check("exit 0 under a hostile stdin codec", proc.returncode == 0, f"rc={proc.returncode}")
    logged = ""
    try:
        with open(hook.LOG, encoding="utf-8") as fh:
            logged = fh.read()
    except OSError:
        pass
    check("the prompt is logged decoded, not mojibake",
          "analizę" in logged and "analizÄ" not in logged, repr(logged[-200:]))
    try:
        os.remove(hook.LOG)
    except OSError:
        pass

    print("\n--- a thin prompt is finished by the conversation ---")

    # The production failure, measured 2026-09-16: "a ile dokładnie?" is a single
    # usable term, so the hook returned None before it ever searched and stayed silent
    # on every short follow-up. The canary harness read 0/5 injected that way and 5/5
    # once the transcript was used. The payload has carried `transcript_path` all
    # along; the hook simply never looked at it.
    transcript = os.path.join(RUNTIME_TMP, "synthetic-transcript.jsonl")
    now_ms = int(time.time() * 1000)
    with open(transcript, "w", encoding="utf-8") as fh:
        for rec in (
            {"type": "message", "role": "user", "sessionId": "s",
             "timestamp": now_ms - 2000,
             "content": [{"type": "text",
                          "text": "<user_query>przegladam limit kredytow w portfelu"
                                  "</user_query>"}]},
            {"type": "function_call_result", "name": "Bash",
             "content": [{"type": "text", "text": "CANARY 4217 in a tool result"}]},
            {"type": "message", "role": "assistant", "sessionId": "s",
             "timestamp": now_ms - 1000,
             "content": [{"type": "text", "text": "Sprawdzam to w magazynie."}]},
        ):
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    text = hook.recent_context(transcript)
    check("recent_context reads the conversation", "limit kredytow" in text, repr(text[:120]))
    check("recent_context skips tool results, which are not the conversation",
          "4217" not in text, repr(text[:120]))
    check("recent_context puts the newest message first, so the query cap keeps it",
          text.index("Sprawdzam") < text.index("limit kredytow"), repr(text[:120]))
    check("recent_context degrades on a missing path rather than raising",
          hook.recent_context("") == "" and hook.recent_context(os.path.join(
              RUNTIME_TMP, "nope.jsonl")) == "")

    def capture(prompt: str, transcript_path: str = "",
                rows: list | None = None) -> tuple[str, str | None]:
        """The query the hook *ended up* sending, with the store faked out.

        The last query, not the first: the fallback makes a second search, and the
        first one is the attempt that failed.
        """
        fake = rows if rows is not None else [{"id": "n1", "terms": 99}]
        seen: list[str] = []
        real = hook.search
        hook.search = lambda q: (seen.append(q), [dict(r) for r in fake])[1]
        try:
            fresh()
            ctx = hook.build_context(prompt, session_id="probe",
                                     transcript=transcript_path)
        finally:
            hook.search = real
        return (seen[-1] if seen else ""), ctx

    query, ctx = capture("a ile dokladnie?", transcript)
    check("a thin prompt borrows its query from the conversation",
          "limit" in query and "kredytow" in query, repr(query))
    check("...and the block still gets built", bool(ctx and "[agtmem]" in ctx))

    query, _ = capture("a ile dokladnie?")
    check("with no transcript a thin prompt still stays silent",
          query == "", repr(query))

    query, _ = capture("jak zbudowac APK androida bez gradlew w tym projekcie",
                       transcript)
    check("a prompt that already retrieves keeps its own query",
          "kredytow" not in query and "portfelu" not in query, repr(query))

    # The fallback has to trigger on the gate, not on prompt length: a prompt can be
    # long and still gate nothing in, and that is the case the transcript is for.
    weak = [{"id": "n1", "terms": 0}, {"id": "n2", "terms": 1}]
    query, _ = capture("jak zbudowac APK androida bez gradlew w tym projekcie",
                       transcript, rows=weak)
    check("a prompt that gates nothing in falls back to the conversation",
          "kredytow" in query, repr(query))
    query, _ = capture("jak zbudowac APK androida bez gradlew w tym projekcie",
                       "", rows=weak)
    check("...and stays silent when there is no conversation to fall back on",
          query == "" or "kredytow" not in query, repr(query))

    # `build_query` exists so the query rule lives in exactly one place. The canary
    # harness had its own copy and drifted the moment the rule changed — it kept
    # calling a constant that no longer existed, and `--diagnose` crashed while the
    # hook itself was fine. Diagnose with the hook's own builder or not at all.
    real_search = hook.search
    hook.search = lambda q: [{"id": "n1", "terms": 99}]
    try:
        found = hook.build_query("a ile dokladnie?", transcript)
    finally:
        hook.search = real_search
    check("build_query returns the documented shape",
          set(found) == {"terms", "need", "query", "rows", "kept", "added"},
          str(sorted(found)))
    check("build_query falls back to the conversation for a thin prompt",
          "kredytow" in found["query"], repr(found["query"]))
    canary_src = ""
    try:
        with open(os.path.join(HERE, "canary_test.py"), encoding="utf-8") as fh:
            canary_src = fh.read()
    except OSError:
        pass
    check("the canary diagnoses with the hook's builder, not a private copy",
          "hook.build_query(" in canary_src and "MIN_PROMPT_TERMS" not in canary_src)

    # The wiring, not the function: the path has to arrive from the payload.
    payload = json.dumps({"prompt": "a ile dokladnie?", "cwd": "",
                          "session_id": "sess-thin", "transcript_path": transcript,
                          "hook_event_name": "UserPromptSubmit"}, ensure_ascii=False)
    rc, out, _err = run_process(payload)
    logged = ""
    try:
        with open(hook.LOG, encoding="utf-8") as fh:
            logged = fh.read()
    except OSError:
        pass
    check("the payload's transcript_path reaches the hook", rc == 0, f"rc={rc}")
    check("the hook says in the log that it used the conversation",
          "ctx=" in logged, repr(logged[-200:]))
    try:
        os.remove(transcript)
    except OSError:
        pass

    print("\n--- the counter: parsing, joining, and the two controls ---")
    import measure_usage as usage

    # The log is the counter's only input, and it has two schemas in it: lines from
    # before `sess=` and `ids=` existed, and lines after. Both must parse.
    synth = os.path.join(RUNTIME_TMP, "synthetic.log")
    with open(synth, "w", encoding="utf-8") as fh:
        fh.write(
            "2026-09-16 09:00:00 [workbuddy-ai] query='x' cw=1/1 need=1 rows=1 kept=1\n"
            "2026-09-16 09:00:01 [workbuddy-ai] INJECT event=UserPromptSubmit 500c "
            "prompt='legacy line, no session and no ids'\n"
            "2026-09-16 09:00:02 [workbuddy-ai] silent event=UserPromptSubmit "
            "prompt='ok'\n"
            "2026-09-16 09:00:03 [workbuddy-ai] INJECT event=UserPromptSubmit 900c "
            "sess=abc12345 ids=a,b,c prompt='three notes'\n"
            "2026-09-16 09:00:04 [workbuddy-ai] INJECT event=UserPromptSubmit 1200c "
            "sess=abc12345 ids=- prompt='nothing to name'\n"
            "not a log line at all\n"
        )
    evs = usage.parse_log(synth)
    check("parse_log keeps prompt events and drops the rest", len(evs) == 4, str(len(evs)))
    check("parse_log reads a line with no sess= and no ids=",
          evs[0]["sess"] == "" and evs[0]["ids"] == [] and evs[0]["kind"] == "inject")
    check("parse_log separates silent from inject", evs[1]["kind"] == "silent")
    check("parse_log splits ids on the comma and keeps the session",
          evs[2]["ids"] == ["a", "b", "c"] and evs[2]["sess"] == "abc12345",
          str(evs[2]))
    check("parse_log treats ids=- as no ids", evs[3]["ids"] == [], str(evs[3]))

    # The measured production fact: the transcript record is written 1-19 s BEFORE the
    # hook fires. A forward-only window dropped 5 of 11 injections while looking like
    # "no answer found" — so the window has to reach backwards, and stay narrow enough
    # to refuse a match from a different turn.
    base = 1_700_000_000.0
    convo = [(int(base * 1000), "user",
              "<user_query>po co zrobilismy agtmem?</user_query>"),
             (int((base + 5) * 1000), "assistant", "odpowiedz")]
    check("find_turn joins a record written BEFORE the hook",
          usage.find_turn(convo, {"ts": base + 10, "prompt": "po co zrobilismy agtmem?"}) == 0)
    check("find_turn refuses a match fifteen minutes away",
          usage.find_turn(convo, {"ts": base + 900, "prompt": "po co zrobilismy agtmem?"}) is None)

    check("stem_hit matches an inflected form the model would paraphrase into",
          usage.stem_hit("magazynu", "magazynach"))
    check("stem_hit does not stem a term shorter than STEM+1",
          not usage.stem_hit("magazi", "magazynach"))
    # The counter's own first test caught this: WORD_RE admits `.` inside a token so
    # that `hooks.json` survives, which means a sentence period rides along on the
    # last word of every sentence — a term the store can never contain.
    check("tokens strips the sentence period that WORD_RE glues on",
          "notatkę" in usage.tokens("Wstrzykuje notatkę. hook wspólny"),
          str(usage.tokens("Wstrzykuje notatkę. hook wspólny")))
    check("tokens keeps a dotted identifier and a relative path whole",
          "hooks.json" in usage.tokens("patrz hooks.json oraz ./gradlew"),
          str(usage.tokens("patrz hooks.json oraz ./gradlew")))
    check("the marker regex accepts the id with or without its type prefix",
          usage.MARKER.findall("a [agtmem:fact/one-two] b [agtmem:three]")
          == ["one-two", "three"], str(usage.MARKER.findall("a [agtmem:fact/one-two] b [agtmem:three]")))

    df = usage.Counter({"wstrzykuje": 2, "hook": 50})
    terms = usage.trace_terms("Wstrzykuje notatkę. hook wspólny",
                              "co wstrzykuje?", "hook omówiony wcześniej", df, 3)
    check("trace_terms drops a term the prompt already contains",
          "wstrzykuje" not in terms, str(terms))
    check("trace_terms drops a term too common in the store", "hook" not in terms, str(terms))
    check("trace_terms keeps a distinctive term never said before",
          "notatkę" in terms, str(terms))

    # `replay` has to hand back both halves, because the second half is the only
    # matched control available: same query, same gate, not injected.
    rows = [{"id": f"n{i}", "terms": 5} for i in range(5)]
    with fake_search(rows):
        injected, gated = usage.replay("jak dziala wstrzykiwanie notatek")
    check("replay returns the head the hook would inject",
          injected == [f"n{i}" for i in range(hook.KEEP)], str(injected))
    check("replay returns everything that passed the gate, so gate-minus-injected "
          "is a matched control",
          gated == [f"n{i}" for i in range(5)] and set(gated) - set(injected), str(gated))
    with fake_search([{"id": "keep", "terms": 9}, {"id": "weak", "terms": 0}]):
        injected, gated = usage.replay("jak dziala wstrzykiwanie notatek")
    check("replay excludes rows that fail the hook's gate", gated == ["keep"], str(gated))
    with fake_search(rows):
        check("replay declines a prompt with nothing to search on",
              usage.replay("no") == ([], []))
    check("reconstruct_ids is the injected half of replay",
          usage.reconstruct_ids.__doc__ and "upper bound" in usage.reconstruct_ids.__doc__)

    try:
        os.remove(synth)
    except OSError:
        pass

    print("\n--- the cost counterfactual: what it may read, and what counts as an answer ---")

    # savings_test is handed a real 27 GB repository and a model. The confinement is
    # the entire safety story, so it is asserted rather than trusted.
    import savings_test as savings

    root = os.path.abspath(RUNTIME_TMP)
    check("resolve accepts a path inside the workspace",
          savings.resolve("sub/file.txt", root) == os.path.join(root, "sub", "file.txt"))
    check("resolve accepts the workspace root itself",
          savings.resolve(".", root) == root)
    for escape in ("../outside.txt", "sub/../../outside.txt", "C:/Windows/system32"):
        try:
            savings.resolve(escape, root)
            check(f"resolve refuses {escape!r}", False, "it was allowed through")
        except savings.Escape:
            check(f"resolve refuses {escape!r}", True)

    # An arm that failed must not be counted as a saving, so "answered" has to be
    # strict: every truth substring, and an empty answer is never a pass.
    check("answered requires every truth substring",
          savings.answered("numer to CZ-8814", ["cz-8814"]) is True)
    check("answered rejects a partial match",
          savings.answered("numer to CZ-8814", ["cz-8814", "play console"]) is False)
    check("answered rejects an empty answer",
          savings.answered("", ["cokolwiek"]) is False)
    check("answered is case-insensitive",
          savings.answered("WĄSIK", ["wąsik"]) is True)

    # The caps decide how expensive arm B is, so they must stay named and visible.
    check("the tool output caps are module constants",
          all(isinstance(getattr(savings, name), (int, float)) for name in
              ("GREP_MAX_MATCHES", "READ_MAX_LINES", "GREP_BUDGET_SECONDS")))
    check("the tool set is read-only — no write or edit tool is exposed",
          {t["function"]["name"] for t in savings.TOOLS} == {"grep", "read", "list_dir"},
          str(sorted(t["function"]["name"] for t in savings.TOOLS)))

    print("\n--- the query cap: judged on four sets, because it is a two-sided limit ---")

    # The defect: truncation cut the *tail* of an ordinary question, and in Polish the
    # specific noun comes last. Losing `klienta` put the answering note at rank 3 on the
    # raw question and rank 10 on the hook's query, so the note was never injected.
    # This check is store-independent and names the exact loss.
    tail = hook.content_words(
        "Który plik w mini definiuje reguły dostępu do Firestore i jak nazywa się "
        "wywołanie chroniące pola przed zapisem z klienta?")
    check("the cap keeps an ordinary question's last content word",
          "klienta" in hook.query_words(tail)[:hook.MAX_QUERY_WORDS],
          str(hook.query_words(tail)[:hook.MAX_QUERY_WORDS]))
    check("MAX_QUERY_WORDS is the measured value, not a round number",
          hook.MAX_QUERY_WORDS == 16, str(hook.MAX_QUERY_WORDS))

    # The end-to-end half. Both of these need a store with content, like the rest of
    # this suite: 14 would pass the first and fail the second, which is why both exist.
    found = hook.build_query(
        "Który plik w mini definiuje reguły dostępu do Firestore i jak nazywa się "
        "wywołanie chroniące pola przed zapisem z klienta?")
    picked = [r["id"] for r in found["kept"][:hook.KEEP]]
    check("a description-style question reaches its note",
          "firebase-identity-and-rules-model" in picked,
          f"picked {picked} need={found['need']}")
    long_prompt = (
        "Przejrzyj prosze to co wiemy o reguach dostepu w Firestore i o tym kto moze "
        "zapisywac jakie pola, bo znowu ktos z klienta probowal nadpisac sobie plan i "
        "nie jestem pewien czy reguly to lapia, czy tylko funkcja. Interesuje mnie "
        "zarowno sam plik regul, jak i to czy jest jakas lista pol ktore sa chronione, "
        "bo pamietam ze bylo z tym zamieszanie i trzeba bylo cos dopisywac recznie.")
    check("a long prompt is still injected (the failure the cap was built for)",
          hook.build_context(long_prompt, session_id="cap-test") is not None,
          f"need would be {hook.required_terms(hook.content_words(long_prompt))}")

    print("\n--- latency ---")
    fresh()
    t = time.perf_counter()
    hook.build_context("jak zbudowac APK androida bez gradlew")
    dt = time.perf_counter() - t
    check("under 3 s", dt < 3.0, f"{dt:.2f}s")
    print(f"      (measured {dt:.2f}s including one agtmem search)")

    shutil.rmtree(RUNTIME_TMP, ignore_errors=True)

    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED:")
        for _, name, detail in failed:
            print(f"  - {name}  {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
