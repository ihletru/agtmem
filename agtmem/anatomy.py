"""Codebase map and symbol lookup.

The point is to let an agent ask "where is validateToken?" and get a file:line
and a signature, instead of reading a 400-line file to find one function.

Deliberately regex-based rather than a real parser (no tree-sitter, no WASM
grammars, no dependencies). Less precise than an AST — it will miss dynamically
generated symbols and can be confused by strings that look like code. That is a
conscious trade: zero dependencies and instant startup beat perfect accuracy for
a memory tool, and every result carries a file:line the agent can verify.
"""
from __future__ import annotations

import re
from pathlib import Path

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".mypy_cache", ".pytest_cache", ".ruff_cache", "build", "dist",
    "out", "target", ".gradle", ".idea", ".vs", "bin", "obj", "vendor",
    ".next", ".nuxt", "coverage", ".cache", ".wolf", ".mem",
}

MAX_FILE_BYTES = 400_000

LANG_RULES: dict[str, list[tuple[str, re.Pattern]]] = {
    ".py": [
        ("class", re.compile(r"^\s*class\s+(\w+)")),
        ("def", re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")),
    ],
    ".js": [
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)")),
        ("const", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=")),
    ],
    ".jsx": [],
    ".mjs": [],
    ".cjs": [],
    ".ts": [
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)")),
        ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)")),
        ("type", re.compile(r"^\s*(?:export\s+)?type\s+(\w+)\s*=")),
        ("enum", re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)")),
        ("const", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*[:=]")),
    ],
    ".kt": [
        ("class", re.compile(r"^\s*(?:@\w+\s+)*(?:public|private|internal|open|data|sealed|abstract|final|\s)*class\s+(\w+)")),
        ("interface", re.compile(r"^\s*(?:public|private|internal|\s)*interface\s+(\w+)")),
        ("object", re.compile(r"^\s*(?:public|private|internal|\s)*object\s+(\w+)")),
        ("fun", re.compile(r"^\s*(?:public|private|internal|suspend|inline|override|open|\s)*fun\s+(?:<[^>]+>\s*)?(\w+)")),
    ],
    ".java": [
        ("class", re.compile(r"^\s*(?:public|private|protected|final|abstract|static|\s)*class\s+(\w+)")),
        ("interface", re.compile(r"^\s*(?:public|private|protected|\s)*interface\s+(\w+)")),
        ("method", re.compile(r"^\s*(?:public|private|protected|static|final|synchronized|\s)+[\w<>\[\],\s]+\s+(\w+)\s*\(")),
    ],
    ".go": [
        ("func", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)")),
        ("type", re.compile(r"^\s*type\s+(\w+)\s+(?:struct|interface)")),
    ],
    ".rs": [
        ("fn", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)")),
        ("struct", re.compile(r"^\s*(?:pub\s+)?struct\s+(\w+)")),
        ("enum", re.compile(r"^\s*(?:pub\s+)?enum\s+(\w+)")),
        ("trait", re.compile(r"^\s*(?:pub\s+)?trait\s+(\w+)")),
        ("impl", re.compile(r"^\s*impl(?:<[^>]+>)?\s+(\w+)")),
    ],
    ".cs": [
        ("class", re.compile(r"^\s*(?:public|private|internal|sealed|abstract|static|partial|\s)*class\s+(\w+)")),
        ("interface", re.compile(r"^\s*(?:public|private|internal|\s)*interface\s+(\w+)")),
    ],
}

# inherit shared rules for the JS-ish extensions
for _ext in (".jsx", ".mjs", ".cjs"):
    LANG_RULES[_ext] = LANG_RULES[".js"]

IMPORT_PATTERNS = [
    re.compile(r"^\s*from\s+([\w\.]+)\s+import", re.M),
    re.compile(r"^\s*import\s+([\w\.]+)", re.M),
    re.compile(r"from\s+['\"]([^'\"]+)['\"]"),
    re.compile(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"^\s*use\s+([\w:]+)", re.M),
]


def _module_stem(token: str) -> str:
    token = token.strip().strip("'\"")
    token = token.replace("\\", "/").rstrip("/")
    if not token:
        return ""
    tail = token.split("/")[-1]
    tail = tail.split(".")[0]
    tail = tail.replace(":", "::").split("::")[-1]
    return tail.lower()


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def scan(root: Path, scope: str, max_files: int = 4000) -> dict:
    """Walk `root`, extract symbols and import edges. Returns a summary dict."""
    root = Path(root).resolve()
    files: dict[Path, str] = {}

    for path in root.rglob("*"):
        if len(files) >= max_files:
            break
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in LANG_RULES:
            continue
        text = _read(path)
        if text:
            files[path] = text

    symbols: list[dict] = []
    imports: dict[Path, set[str]] = {}
    own_stem: dict[str, Path] = {}

    for path, text in files.items():
        rel = str(path.relative_to(root)).replace("\\", "/")
        rules = LANG_RULES.get(path.suffix.lower(), [])
        for lineno, line in enumerate(text.splitlines(), 1):
            if len(line) > 400:
                continue
            for kind, pattern in rules:
                match = pattern.match(line)
                if match:
                    symbols.append({
                        "scope": scope,
                        "name": match.group(1),
                        "kind": kind,
                        "file": rel,
                        "line": lineno,
                        "signature": line.strip()[:160],
                    })
                    break
        found: set[str] = set()
        for pattern in IMPORT_PATTERNS:
            for token in pattern.findall(text):
                stem = _module_stem(token)
                if stem:
                    found.add(stem)
        imports[path] = found
        own_stem.setdefault(path.stem.lower(), path)

    # poor man's importance: how many other files import this module
    importance: dict[str, int] = {}
    for stem, path in own_stem.items():
        count = sum(1 for other, deps in imports.items() if other != path and stem in deps)
        if count:
            rel = str(path.relative_to(root)).replace("\\", "/")
            importance[rel] = count

    return {
        "root": str(root),
        "scope": scope,
        "files": len(files),
        "symbols": symbols,
        "importance": importance,
    }


def render_map(result: dict, *, max_files: int = 60, max_symbols_per_file: int = 8) -> str:
    """A compact, readable project map — this is what gets stored as a note."""
    by_file: dict[str, list[dict]] = {}
    for sym in result["symbols"]:
        by_file.setdefault(sym["file"], []).append(sym)

    ranked = sorted(
        by_file,
        key=lambda f: (-result["importance"].get(f, 0), f),
    )[:max_files]

    lines = [
        f"# Code map — {result['scope']}",
        "",
        f"Root: `{result['root']}`  ",
        f"Files: {result['files']}, symbols: {len(result['symbols'])}",
        "",
        "Ordered by importance (how many files import the module).",
        "",
    ]
    for rel in ranked:
        syms = by_file[rel]
        deps = result["importance"].get(rel, 0)
        head = f"## `{rel}`" + (f" — {deps} importers" if deps else "")
        lines.append(head)
        for sym in syms[:max_symbols_per_file]:
            lines.append(f"- `{sym['name']}` ({sym['kind']}) — line {sym['line']}")
        if len(syms) > max_symbols_per_file:
            lines.append(f"- … and {len(syms) - max_symbols_per_file} more")
        lines.append("")
    return "\n".join(lines)


def render_focus(result: dict, focus: str, *, limit: int = 40) -> str:
    """A narrow slice of the map, for `agtmem map --focus`."""
    needle = focus.lower().replace("\\", "/")
    hits = [s for s in result["symbols"] if needle in s["file"].lower()]
    hits.sort(key=lambda s: (s["file"], s["line"]))
    if not hits:
        return f"(no symbols in paths containing {focus!r})"
    lines = [f"# Code map — {result['scope']} / {focus}", ""]
    current = None
    for sym in hits[:limit]:
        if sym["file"] != current:
            current = sym["file"]
            lines.append(f"`{current}`")
        lines.append(f"  - `{sym['name']}` ({sym['kind']}) — line {sym['line']}")
    return "\n".join(lines)
