"""A small eval harness: does search actually find the right note?

Without a number you cannot tell whether a memory system works, and without a
baseline the number means nothing. So every run reports the same metrics for
`agtmem search` and for a plain grep over the same files.

Format of ~/.agtmem/eval.txt — one case per line, hand-editable:

    # question => expected note id(s)
    why is the index a cache? => design-store-is-contract
    how do I wire the MCP server? => mcp-setup

    ! a question the store cannot answer

Ground truth must point at **active, distilled notes**, never at a raw
`session-*` transcript and never at a `superseded` note. A session is input
rather than knowledge, so scoring it measures the wrong layer; a superseded note
is hidden from default search, so a case built on one fails for a reason that
has nothing to do with ranking quality.

A line starting with `!` is a **known coverage gap**: a question that no note
answers. These are kept deliberately — deleting them would hide a real defect in
distillation — but they are scored separately, because no amount of ranking work
can return a note that was never written. Mixing them into R@5 would make a
distillation gap look like a retrieval failure and send you tuning the wrong
component.

Metrics (over scored cases only):
  R@5  share of cases where at least one expected note landed in the top 5
  P@5  share of the top 5 that were expected notes, averaged over cases
"""
from __future__ import annotations

import re
from pathlib import Path

from . import index, store
from .store import EVAL_PATH

CASE_RE = re.compile(r"^(?P<q>.+?)\s*=>\s*(?P<ids>.+)$")
GAP_PREFIX = "!"
# English and Polish function words both: the store is deliberately
# multilingual, and the grep baseline must not be handicapped in one of them.
STOP = {
    "the", "and", "for", "with", "that", "this", "what", "which",
    "from", "into", "jak", "gdzie", "czy", "jest", "sie", "się",
    "nie", "oraz",
}


def load_cases(path: Path = EVAL_PATH) -> list[tuple[str, list[str], bool]]:
    """Parse the eval file into (question, expected_ids, is_gap) triples."""
    if not path.exists():
        return []
    cases: list[tuple[str, list[str], bool]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(GAP_PREFIX):
            question = line[len(GAP_PREFIX):].strip()
            if question:
                cases.append((question, [], True))
            continue
        match = CASE_RE.match(line)
        if not match:
            continue
        expected = [i.strip() for i in match.group("ids").split(",") if i.strip()]
        if expected:
            cases.append((match.group("q").strip(), expected, False))
    return cases


def add_case(
    question: str,
    note_ids: list[str] | None = None,
    path: Path = EVAL_PATH,
    gap: bool = False,
) -> None:
    """Append a case. With `gap=True` the question is recorded as unanswerable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if gap:
        line = f"{GAP_PREFIX} {question}\n"
    else:
        line = f"{question} => {', '.join(note_ids or [])}\n"
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
        return {"cases": 0, "gaps": [], "hint": f"no cases yet — add one to {EVAL_PATH}"}

    notes = store.load_all()
    rows = []
    gaps = []
    mem_r = mem_p = grep_r = grep_p = 0.0
    scored = 0

    for question, expected, is_gap in cases:
        hits = index.search(question, limit=limit)
        got = [h["id"] for h in hits]
        base = _grep_top5(question, notes)

        if is_gap:
            # Scored nowhere: a missing note is not a ranking result.
            gaps.append({"question": question, "got": got[:5], "baseline": base})
            continue

        r, p = _metrics(got, expected)
        mem_r += r
        mem_p += p
        br, bp = _metrics(base, expected)
        grep_r += br
        grep_p += bp
        scored += 1

        rows.append({
            "question": question,
            "expected": expected,
            "got": got[:5],
            "baseline": base,
            "mem_hit": bool(r),
            "grep_hit": bool(br),
        })

    result: dict = {
        "cases": scored,
        "gaps": gaps,
        "rows": rows,
    }
    if scored:
        result["mem"] = {
            "r_at_5": round(mem_r / scored, 3),
            "p_at_5": round(mem_p / scored, 3),
        }
        result["grep"] = {
            "r_at_5": round(grep_r / scored, 3),
            "p_at_5": round(grep_p / scored, 3),
        }
    return result


def render(result: dict) -> str:
    if not result.get("cases") and not result.get("gaps"):
        return result.get("hint", "no data")

    header = f"Cases: {result['cases']} scored"
    if result.get("gaps"):
        header += f", {len(result['gaps'])} known gap(s) excluded"
    lines = [header, ""]

    if result.get("cases"):
        lines += [
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

    if result.get("gaps"):
        lines.append("")
        lines.append(
            "Known coverage gaps — no note answers these, so this is a "
            "distillation gap, not a retrieval miss:"
        )
        for row in result["gaps"]:
            lines.append(f"  - {row['question']}")
            lines.append(f"      top hit:    {', '.join(row['got']) or '(none)'}")
    return "\n".join(lines)
