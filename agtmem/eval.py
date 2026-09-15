"""A small eval harness: does search actually find the right note?

Without a number you cannot tell whether a memory system works, and without a
baseline the number means nothing. So every run reports the same metrics for
`agtmem search` and for a plain grep over the same files.

Format of ~/.agtmem/eval.txt — one case per line, hand-editable:

    # question => expected note id(s)
    why is the index a cache? => design-store-is-contract
    how do I wire the MCP server? => mcp-setup

Metrics:
  R@5  share of cases where at least one expected note landed in the top 5
  P@5  share of the top 5 that were expected notes, averaged over cases
"""
from __future__ import annotations

import re
from pathlib import Path

from . import index, store
from .store import EVAL_PATH

CASE_RE = re.compile(r"^(?P<q>.+?)\s*=>\s*(?P<ids>.+)$")
# English and Polish function words both: the store is deliberately
# multilingual, and the grep baseline must not be handicapped in one of them.
STOP = {
    "the", "and", "for", "with", "that", "this", "what", "which",
    "from", "into", "jak", "gdzie", "czy", "jest", "sie", "się",
    "nie", "oraz",
}


def load_cases(path: Path = EVAL_PATH) -> list[tuple[str, list[str]]]:
    if not path.exists():
        return []
    cases: list[tuple[str, list[str]]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = CASE_RE.match(line)
        if not match:
            continue
        expected = [i.strip() for i in match.group("ids").split(",") if i.strip()]
        if expected:
            cases.append((match.group("q").strip(), expected))
    return cases


def add_case(question: str, note_ids: list[str], path: Path = EVAL_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{question} => {', '.join(note_ids)}\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _keywords(question: str) -> list[str]:
    tokens = re.findall(r"\w+", question.lower(), re.UNICODE)
    return [t for t in tokens if len(t) >= 3 and t not in STOP]


def _grep_top5(question: str, notes: list[store.Note]) -> list[str]:
    """The honest baseline: substring match, ranked by hit count."""
    keys = _keywords(question)
    if not keys:
        return []
    scored: list[tuple[int, str]] = []
    for note in notes:
        haystack = f"{note.title}\n{note.body}\n{' '.join(note.tags)}".lower()
        hits = sum(haystack.count(key) for key in keys)
        if hits:
            scored.append((hits, note.id))
    scored.sort(key=lambda kv: (-kv[0], kv[1]))
    return [note_id for _, note_id in scored[:5]]


def _metrics(retrieved: list[str], expected: list[str]) -> tuple[float, float]:
    top5 = retrieved[:5]
    recall = 1.0 if any(i in top5 for i in expected) else 0.0
    precision = len([i for i in top5 if i in expected]) / 5.0
    return recall, precision


def run(limit: int = 5) -> dict:
    cases = load_cases()
    if not cases:
        return {"cases": 0, "hint": f"no cases yet — add one to {EVAL_PATH}"}

    notes = store.load_all()
    rows = []
    mem_r = mem_p = grep_r = grep_p = 0.0

    for question, expected in cases:
        hits = index.search(question, limit=limit)
        got = [h["id"] for h in hits]
        r, p = _metrics(got, expected)
        mem_r += r
        mem_p += p

        base = _grep_top5(question, notes)
        br, bp = _metrics(base, expected)
        grep_r += br
        grep_p += bp

        rows.append({
            "question": question,
            "expected": expected,
            "got": got[:5],
            "baseline": base,
            "mem_hit": bool(r),
            "grep_hit": bool(br),
        })

    n = len(cases)
    return {
        "cases": n,
        "mem": {"r_at_5": round(mem_r / n, 3), "p_at_5": round(mem_p / n, 3)},
        "grep": {"r_at_5": round(grep_r / n, 3), "p_at_5": round(grep_p / n, 3)},
        "rows": rows,
    }


def render(result: dict) -> str:
    if not result.get("cases"):
        return result.get("hint", "no data")
    lines = [
        f"Cases: {result['cases']}",
        "",
        f"{'':<10}{'R@5':>8}{'P@5':>8}",
        f"{'mem':<10}{result['mem']['r_at_5']:>8}{result['mem']['p_at_5']:>8}",
        f"{'grep':<10}{result['grep']['r_at_5']:>8}{result['grep']['p_at_5']:>8}",
        "",
    ]
    misses = [row for row in result["rows"] if not row["mem_hit"]]
    if misses:
        lines.append("Missed:")
        for row in misses:
            lines.append(f"  - {row['question']}")
            lines.append(f"      expected:   {', '.join(row['expected'])}")
            lines.append(f"      returned:   {', '.join(row['got']) or '(none)'}")
    return "\n".join(lines)
