"""End-to-end test for agtmem.

Covers the properties the design claims, not just the happy path:
  * the acceptance test — delete the index, and a plain search still works
  * supersession keeps the old note out of default results
  * ingest never duplicates a summary
  * the MCP server speaks JSON-RPC on stdout with nothing else mixed in
  * a tool error comes back as isError, not as a dropped connection
  * twelve concurrent writers lose no update
  * a long note does not outrank the short note that answers the question
  * the eval counts scored cases separately from known coverage gaps

Run:  python tests/test_e2e.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

passed, failed = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -- ' + detail) if detail else ''}")


def _env(home: Path) -> dict:
    return {**os.environ, "AGTMEM_HOME": str(home), "PYTHONIOENCODING": "utf-8"}


def run(home: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "agtmem.cli", *args],
        cwd=str(ROOT), env=_env(home), capture_output=True, text=True,
        encoding="utf-8", errors="replace", input=stdin,
    )


def mcp_call(home: Path, messages: list[dict]) -> tuple[list[dict], str]:
    """Send JSON-RPC lines, return (parsed responses, raw stdout)."""
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [PY, "-m", "agtmem.cli", "mcp"],
        cwd=str(ROOT), env=_env(home), input=payload, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    responses = []
    for line in proc.stdout.splitlines():
        if line.strip():
            responses.append(json.loads(line))
    return responses, proc.stdout


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="agtmem-test-"))
    print(f"test store: {home}\n")
    try:
        # ---------------------------------------------------------- basic
        print("[1] init / add / bug / search")
        check("init", run(home, "init").returncode == 0)
        check("add", run(
            home, "add", "--title", "Supersession uses pointers",
            "--type", "decision", "--scope", "t",
            "--tags", "design", "storage",
            "--body", "The old note is not deleted; it is marked superseded.",
        ).returncode == 0)
        check("bug", run(
            home, "bug", "--symptom", "append hung for 15s",
            "--cause", "the lock was not re-entrant",
            "--fix", "per-process depth counter", "--scope", "t",
        ).returncode == 0)

        r = run(home, "search", "supersession")
        check("search finds the note", "supersession-uses-pointers" in r.stdout, r.stdout)

        # `--tags a b` must land in the frontmatter as two tags
        raw = (home / "decisions" / "supersession-uses-pointers.md").read_text("utf-8")
        check("--tags accepts several values", "tags: design storage" in raw, raw[:200])

        # ------------------------------------------------- transliteration
        print("\n[2] slug: diacritics -> ASCII")
        run(home, "add", "--title", "Łąka żółć jaźń", "--scope", "t", "--body", "x")
        check(
            "diacritics are folded, not chopped out",
            (home / "facts" / "laka-zolc-jazn.md").exists(),
            str(sorted(p.name for p in (home / "facts").iterdir())),
        )

        # ---------------------------------------------------- supersession
        print("\n[3] supersession")
        run(home, "add", "--title", "Supersession v2", "--type", "decision",
            "--scope", "t", "--supersedes", "supersession-uses-pointers",
            "--body", "Corrected version.")
        default = run(home, "search", "supersession").stdout
        check("default results show only the new note", "supersession-v2" in default
              and "supersession-uses-pointers" not in default, default)
        allr = run(home, "search", "supersession", "--all").stdout
        check("--all shows both", "supersession-uses-pointers" in allr, allr)
        old = (home / "decisions" / "supersession-uses-pointers.md").read_text("utf-8")
        check("old note is marked superseded", "status: superseded" in old, old[:300])
        check("old note points forward", "superseded_by: supersession-v2" in old)

        # ------------------------------------------- acceptance: rm index
        print("\n[4] ACCEPTANCE TEST: delete the index")
        for leftover in home.glob(".index.sqlite*"):
            leftover.unlink()
        check("index deleted", not (home / ".index.sqlite").exists())
        healed = run(home, "search", "supersession")
        check("a plain search rebuilt it", "supersession-v2" in healed.stdout, healed.stdout)
        check("index exists again", (home / ".index.sqlite").exists())
        still = run(home, "search", "supersession").stdout
        check("supersession still filtered after the rebuild",
              "supersession-uses-pointers" not in still, still)

        # ------------------------------------------------------- ingest
        print("\n[5] session ingest — no duplicates")
        fake_projects = home / "fakeprojects"
        fake_root = fake_projects / "c-Users-someone-myproject"
        fake_root.mkdir(parents=True)
        # Shape mirrors a real transcript record: top-level `content` as a list,
        # with providerData marking it as a compaction summary.
        rec = {
            "type": "message",
            "role": "user",
            "content": [{
                "type": "text",
                "text": "<conversation_history_summary>\nSummary: a synthetic "
                        "compaction summary used by the test suite.\n"
                        "</conversation_history_summary>",
            }],
            "providerData": {"isSummary": True, "compactType": "emergency-auto"},
            "timestamp": 1789000000000,
            "sessionId": "aaaa1111-2222-3333-4444-555566667777",
        }
        (fake_root / "aaaa1111-2222-3333-4444-555566667777.jsonl").write_text(
            json.dumps(rec) + "\n", encoding="utf-8"
        )
        env = _env(home)
        script = (
            "import json;from pathlib import Path;"
            "from agtmem import ingest;"
            f"r=ingest.ingest(projects_root=Path(r'{fake_projects}'));"
            "print(json.dumps(r))"
        )
        first = subprocess.run([PY, "-c", script], cwd=str(ROOT), env=env,
                               capture_output=True, text=True, encoding="utf-8")
        second = subprocess.run([PY, "-c", script], cwd=str(ROOT), env=env,
                                capture_output=True, text=True, encoding="utf-8")
        r1 = json.loads(first.stdout.strip().splitlines()[-1])
        r2 = json.loads(second.stdout.strip().splitlines()[-1])
        check("first run creates 1 note", r1["created"] == 1, str(r1))
        check("second run creates nothing", r2["created"] == 0, str(r2))
        check("second run skips 1", r2["skipped"] == 1, str(r2))

        # --------------------------------------------------------- doctor
        print("\n[6] doctor detects drift, reindex heals it")
        # The ingest above wrote straight to disk without touching the index,
        # which is exactly the drift doctor exists to catch.
        doc = run(home, "doctor").stdout
        check("doctor detects index/disk drift",
              "Notes on disk: 5" in doc and "4 notes" in doc, doc)
        check("doctor names the remedy", "reindex" in doc, doc)
        check("reindex", run(home, "reindex").returncode == 0)
        clean = run(home, "doctor").stdout
        check("doctor is clean after reindex", "OK" in clean, clean)

        # the CLI ingest path refreshes the index itself
        cli_ing = run(home, "ingest-sessions", "--limit", "1").stdout
        check("ingest CLI reports clearly", "already stored" in cli_ing, cli_ing)
        check("ingest CLI leaves no drift", "OK" in run(home, "doctor").stdout)

        # ------------------------------------------------------------ MCP
        print("\n[7] MCP server over stdio")
        responses, raw = mcp_call(home, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18",
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "agtmem_search", "arguments": {"query": "supersession"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "agtmem_read", "arguments": {"id": "supersession-v2"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "agtmem_write",
                        "arguments": {"title": "Note from MCP", "body": "content",
                                      "scope": "mcp", "type": "fact"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "agtmem_bug",
                        "arguments": {"symptom": "s", "cause": "c", "fix": "f"}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": "agtmem_candidates", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
             "params": {"name": "agtmem_find", "arguments": {"symbol": "store_lock"}}},
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": "agtmem_read", "arguments": {"id": "no-such-note"}}},
            {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
             "params": {"name": "agtmem_write", "arguments": {"title": "no body"}}},
            {"jsonrpc": "2.0", "id": 11, "method": "no_such_method"},
        ])
        by_id = {r.get("id"): r for r in responses}

        check("every request got a response", len(responses) == 11,
              f"got {len(responses)}")
        check("the notification was NOT answered", None not in by_id)
        check("initialize returns instructions",
              "instructions" in by_id[1]["result"], str(by_id[1])[:200])
        check("tools/list returns 8 tools",
              len(by_id[2]["result"]["tools"]) == 8,
              str(len(by_id[2]["result"]["tools"])))
        check("agtmem_search works",
              "supersession-v2" in by_id[3]["result"]["content"][0]["text"],
              by_id[3]["result"]["content"][0]["text"][:200])
        check("agtmem_read returns the body",
              "Corrected version" in by_id[4]["result"]["content"][0]["text"])
        check("agtmem_write writes",
              "written" in by_id[5]["result"]["content"][0]["text"])
        check("agtmem_bug writes",
              "bug recorded" in by_id[6]["result"]["content"][0]["text"])
        check("agtmem_candidates works",
              "empty" in by_id[7]["result"]["content"][0]["text"],
              by_id[7]["result"]["content"][0]["text"][:120])
        check("agtmem_find survives with no scan",
              by_id[8]["result"]["isError"] is False)
        check("missing note => isError, connection intact",
              by_id[9]["result"]["isError"] is True,
              str(by_id[9])[:200])
        check("missing parameter => isError",
              by_id[10]["result"]["isError"] is True)
        check("unknown method => error -32601",
              by_id[11].get("error", {}).get("code") == -32601, str(by_id[11]))

        # stdout must carry JSON-RPC and nothing else
        non_json = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                non_json.append(line[:80])
        check("stdout carries only JSON-RPC", not non_json, str(non_json[:3]))

        check("the note written over MCP is a real file",
              (home / "facts" / "note-from-mcp.md").exists(),
              str(sorted(p.name for p in (home / "facts").iterdir())))

        # ---------------------------------------------------- concurrency
        print("\n[8] concurrency: 12 parallel appends")
        run(home, "add", "--title", "Counter", "--scope", "conc", "--body", "start")
        procs = [
            subprocess.Popen(
                [PY, "-m", "agtmem", "append", "counter", f"line-{i}"],
                cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            for i in range(1, 13)
        ]
        codes, errors = [], []
        for i, p in enumerate(procs, 1):
            out, err = p.communicate(timeout=120)
            codes.append(p.returncode)
            if p.returncode != 0:
                errors.append(f"line-{i} rc={p.returncode} stderr={err.strip()[-1500:]}")
        check("all 12 processes exited cleanly", all(c == 0 for c in codes),
              " | ".join(errors) or str(codes))
        body = (home / "facts" / "counter.md").read_text("utf-8")
        missing = [f"line-{i}" for i in range(1, 13) if f"line-{i}\n" not in body
                   and not body.rstrip().endswith(f"line-{i}")]
        check("no append was lost (no lost update)", not missing, str(missing))

        # The lock file persists by design (deleting a file another process may
        # be blocked on is a race); what must hold is that nobody holds it.
        # Probed in a subprocess so AGTMEM_HOME binds to the temp store.
        probe = (
            "from agtmem import store\n"
            "try:\n"
            "    with store.store_lock(timeout=0.5): print('FREE')\n"
            "except store.LockTimeout: print('HELD')\n"
        )
        held = subprocess.run(
            [PY, "-c", probe], cwd=str(ROOT), env=env,
            capture_output=True, text=True, encoding="utf-8",
        )
        check("the lock is released when writers finish",
              "FREE" in held.stdout, held.stdout + held.stderr)
        check("no .tmp- files left behind",
              not list(home.rglob(".tmp-*")),
              str([str(p) for p in home.rglob(".tmp-*")]))

        # ---------------------------------------------------- dependencies
        # The README claims zero runtime dependencies. A claim like that rots the
        # moment someone adds an import, so assert it rather than trusting it.
        print("\n[9] zero runtime dependencies")
        import ast
        foreign = {}
        for source in sorted((ROOT / "agtmem").glob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name not in sys.stdlib_module_names:
                        foreign.setdefault(str(source.name), set()).add(name)
        check("every import in the package is stdlib", not foreign, str(foreign))

        # The declared dependency list must stay empty too.
        declared = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        check("pyproject declares no dependencies",
              'dependencies = []' in declared)

        # ------------------------------------------------- scope derivation
        # A project directory carrying a session timestamp used to yield the
        # clock reading as the scope ("43-59"), which then became a bucket of
        # its own in the store — indistinguishable from a real project.
        print("\n[10] scope derivation")
        from agtmem.ingest import scope_from_dir
        cases = [
            ("c-Users-milo-verbigem-android", "verbigem-android"),
            ("c-Users-milo-projekty-online-ai-hub", "ai-hub"),
            ("home-alice-code-myproject", "code-myproject"),
            ("c-Users-milo-WorkBuddy AI-2026-09-04-11-43-59", "workbuddy-ai"),
            ("c-Users-milo-WorkBuddy AI-2026-09-15-20-31-25", "workbuddy-ai"),
            ("c-Users-milo-my-project-2026-09-03", "my-project"),
            ("", "sessions"),
        ]
        for dirname, expected in cases:
            got = scope_from_dir(dirname)
            check(f"scope of {dirname!r} is {expected!r}", got == expected,
                  f"got {got!r}")

        # ------------------------------------------------ session exclusion
        print("\n[11] raw sessions are excluded from search by default")
        run(home, "add", "--title", "Flux capacitor design note",
            "--type", "fact", "--scope", "t",
            "--body", "The flux capacitor needs 1.21 gigawatts; see the build notes.")
        # The transcript repeats the terms on purpose: a session is ~10x the
        # size of a note, so it wins on length unless it is filtered out.
        run(home, "add", "--title", "Session 2026-01-01 (t): summary",
            "--type", "session", "--scope", "t",
            "--body", "flux capacitor build notes gigawatts " * 40)

        default = run(home, "search", "flux capacitor").stdout
        check("default search returns the distilled note",
              "flux-capacitor-design-note" in default, default)
        check("default search hides the raw transcript",
              "session-2026-01-01" not in default, default)

        with_sessions = run(home, "search", "flux capacitor", "--sessions").stdout
        check("--sessions brings the transcript back",
              "session-2026-01-01" in with_sessions, with_sessions)

        # The transcript must not eat RECALL slots either: excluding it in SQL
        # means the note still surfaces even when the session outranks it.
        check("the note is not crowded out by the transcript",
              run(home, "search", "flux capacitor", "--limit", "1").stdout.strip()
              .startswith("flux-capacitor-design-note"),
              run(home, "search", "flux capacitor", "--limit", "1").stdout)

        mcp_resp, _ = mcp_call(home, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "agtmem_search",
                        "arguments": {"query": "flux capacitor"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "agtmem_search",
                        "arguments": {"query": "flux capacitor", "sessions": True}}},
        ])
        by_id = {r["id"]: r for r in mcp_resp if "id" in r}
        default_text = by_id[2]["result"]["content"][0]["text"]
        sessions_text = by_id[3]["result"]["content"][0]["text"]
        check("MCP search hides sessions by default",
              "session-2026-01-01" not in default_text, default_text[:200])
        check("MCP search exposes sessions=true",
              "session-2026-01-01" in sessions_text, sessions_text[:200])

        # -------------------------------------------------- length penalty
        print("\n[12] a long note does not outrank the short note that answers")
        run(home, "add", "--title", "Warp core alignment procedure",
            "--type", "fact", "--scope", "t",
            "--body", "Warp core alignment: tighten the plasma injector in three passes.")
        # Equal term coverage, but ~7 kB against ~70 B. Counting presence alone
        # scored these identically, and BM25 then handed the win to the long one
        # because term frequency rises with length — which is the bug the length
        # penalty in _coverage() exists to stop.
        run(home, "add", "--title", "Warp core log",
            "--type", "log", "--scope", "t",
            "--body", "warp core alignment plasma injector " * 200)

        query = "warp core alignment plasma injector"
        top = run(home, "search", query).stdout
        check("the short note wins on equal coverage",
              top.strip().startswith("warp-core-alignment-procedure"), top[:300])
        # The penalty must not be a filter: the long note is still reachable.
        check("the long note is still returned, just lower",
              "warp-core-log" in top, top[:300])

        # The opposite failure, and the subtler one: the discount must stay weak
        # enough that better coverage still wins. At COVERAGE_FREE_BYTES = 5000 it
        # did not — a ~4 kB note matching four terms beat a ~6 kB note matching
        # five, purely because the discount overcame better evidence. That is the
        # same mistake as the length bias, only smaller. Deliberately a separate
        # vocabulary, so the notes above cannot decide the ranking instead.
        run(home, "add", "--title", "Dilithium containment spec",
            "--type", "fact", "--scope", "t",
            "--body", "dilithium crystal matrix containment field " * 145)
        run(home, "add", "--title", "Crystal matrix overview",
            "--type", "fact", "--scope", "t",
            "--body", "dilithium crystal matrix containment " * 78)

        top = run(home, "search",
                  "dilithium crystal matrix containment field").stdout
        check("more coverage beats a smaller note",
              top.strip().startswith("dilithium-containment-spec"), top[:300])

        # ------------------------------------------------------------ eval
        print("\n[13] the eval separates retrieval misses from coverage gaps")
        run(home, "add", "--title", "Borg transwarp conduit notes",
            "--type", "fact", "--scope", "t",
            "--body", "Transwarp conduit: the Borg use six hubs, not one.")
        (home / "eval.txt").write_text(
            "# a comment line, which must be ignored\n"
            "How many transwarp hubs do the Borg use? => borg-transwarp-conduit-notes\n"
            "! How do we verify the downloaded APK is intact?\n",
            encoding="utf-8",
        )
        out = run(home, "eval").stdout
        check("only the scored case counts", "Cases: 1 scored" in out, out)
        check("the gap is excluded from the case count",
              "1 known gap(s) excluded" in out, out)
        check("the gap is named as a distillation gap",
              "distillation gap, not a retrieval miss" in out, out)
        mem_row = next((l for l in out.splitlines() if l.startswith("mem")), "")
        check("the scored case was actually found", "1.0" in mem_row, mem_row)

        run(home, "eval", "--add-gap", "Why is the sky blue?")
        appended = (home / "eval.txt").read_text(encoding="utf-8")
        check("--add-gap appends a gap line",
              "! Why is the sky blue?" in appended, appended)

        # Ground truth that search can never return fails for the wrong reason,
        # so it is refused at the moment of writing rather than debugged later.
        run(home, "add", "--title", "Old flux rule", "--type", "fact",
            "--scope", "t", "--body", "superseded shortly")
        run(home, "add", "--title", "New flux rule", "--type", "fact",
            "--scope", "t", "--body", "the current one",
            "--supersedes", "old-flux-rule")
        guard = run(home, "eval", "--add", "Why flux? => old-flux-rule")
        check("--add refuses a superseded target",
              guard.returncode == 2 and "superseded" in guard.stderr, guard.stderr)
        missing = run(home, "eval", "--add", "Why flux? => no-such-note-here")
        check("--add refuses a missing target",
              missing.returncode == 2 and "no such note" in missing.stderr,
              missing.stderr)
        ok = run(home, "eval", "--add", "Why flux? => new-flux-rule")
        check("--add accepts an active target", ok.returncode == 0, ok.stderr)

    finally:
        shutil.rmtree(home, ignore_errors=True)

    print(f"\n{'=' * 46}\n  {passed} passed, {failed} failed\n{'=' * 46}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
