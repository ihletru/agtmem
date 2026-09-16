#!/usr/bin/env python3
"""Install (or remove) the agtmem hooks in the WorkBuddy/CodeBuddy settings.

Why this exists
---------------
A hook command is a **literal string** in `settings.json`. Writing that string by
hand means one person's absolute paths end up in the config, and the hook stops
working the moment the interpreter moves, the repo is cloned elsewhere, or
somebody else tries to use it.

So: the *installer* resolves the variables once and records them in a sidecar
file next to the hook. `settings.json` then only has to name the interpreter and
the script — both of which the installer knows from `sys.executable` and
`__file__`, not from a constant.

What it touches
---------------
* `<configDir>/settings.json` — a `hooks` block with `UserPromptSubmit` and
  `SessionStart`. Existing hooks from other sources are **preserved**; only
  entries that already point at this script are replaced, so the command is
  idempotent.
* `agtmem_inject.config.json` next to the hook — the resolved `repo` / `store` /
  `runtime`, written only when they cannot be derived at run time.

Nothing here is machine-specific: every path comes from the environment, from
`sys.executable`, or from `__file__`.

Usage
-----
    python install_hooks.py --status              # what is wired up, and where
    python install_hooks.py                       # every candidate that exists
    python install_hooks.py --settings PATH       # an explicit file (repeatable)
    python install_hooks.py --repo PATH           # record the agtmem checkout
    python install_hooks.py --runtime PATH        # where log + state go
    python install_hooks.py --uninstall           # remove our entries again
    python install_hooks.py --dry-run             # show, write nothing
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "agtmem_inject.py")
SIDECAR = os.path.join(HERE, "agtmem_inject.config.json")

EVENTS = ("UserPromptSubmit", "SessionStart")
TIMEOUT = 20


def log(msg: str) -> None:
    print(msg)


# --------------------------------------------------------------------------- #
# Where the settings live — computed, never hardcoded
# --------------------------------------------------------------------------- #

def candidate_settings() -> list[str]:
    """Files worth considering, most-likely first.

    The CLI resolves its *user* scope from a product-name constant, and on this
    machine three files are plausible: `~/.workbuddy/` (where the desktop writes
    `enabledPlugins`), `~/.codebuddy/`, and whatever `WORKBUDDY_CONFIG_DIR`
    points at.

    **Do not trust that variable first.** It is context-dependent: the desktop
    sets it to `~/.workbuddy` when it spawns the CLI, but the agent runtime sets
    it to `~/.workbuddy-ai` in a shell — a different product's home, whose
    `settings.json` holds `sandbox` / `claw` keys rather than hooks. So the
    well-known paths come first and the env-derived one comes last, as a
    candidate rather than as an answer.
    """
    home = os.path.expanduser("~")
    out: list[str] = []
    for name in (".workbuddy", ".codebuddy"):
        path = os.path.join(home, name, "settings.json")
        if path not in out:
            out.append(path)
    for key in ("WORKBUDDY_CONFIG_DIR", "CODEBUDDY_CONFIG_DIR"):
        val = os.environ.get(key)
        if val and val.strip():
            path = os.path.join(val.strip(), "settings.json")
            if path not in out:
                out.append(path)
    return out


def label_for(path: str) -> str:
    """`…/.workbuddy/settings.json` -> `workbuddy`. Keeps the log self-describing.

    Deliberately does not call `abspath`: a bare `settings.json` would then be
    labelled after the current directory, which is an accident, not information.
    """
    parent = os.path.dirname(path)
    name = os.path.basename(parent) if parent else ""
    if not name:
        name = os.path.splitext(os.path.basename(path))[0]
    return name.lstrip(".") or "settings"


def default_targets() -> list[str]:
    existing = [p for p in candidate_settings() if os.path.exists(p)]
    return existing or [candidate_settings()[0]]


def default_runtime() -> str:
    """Where the hook's log and suppression state belong.

    The desktop's own hooks directory is the natural home: it is already the
    place user-level hook material lives, and it is *not* the checkout, so a
    prompt never dirties the working tree.
    """
    env = os.environ.get("AGTMEM_HOOK_RUNTIME")
    if env and env.strip():
        return os.path.abspath(os.path.expanduser(env.strip()))
    return os.path.join(os.path.expanduser("~"), ".workbuddy", "hooks")


# --------------------------------------------------------------------------- #
# Resolving the variables
# --------------------------------------------------------------------------- #

def guess_repo() -> str | None:
    """Ask the interpreter where `agtmem` is importable from."""
    import subprocess
    try:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import os, agtmem; print(os.path.dirname(os.path.dirname("
             "os.path.abspath(agtmem.__file__))))"],
            capture_output=True, timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    path = proc.stdout.decode("utf-8", "replace").strip()
    return path if path and os.path.isdir(path) else None


def resolve(repo_arg: str | None, store_arg: str | None,
            runtime_arg: str | None) -> dict:
    """Work out what the sidecar should say. Absent values stay absent."""
    repo = repo_arg or os.environ.get("AGTMEM_REPO") or guess_repo()
    store = store_arg or os.environ.get("AGTMEM_HOME")
    out: dict[str, str] = {}
    if repo:
        out["repo"] = os.path.abspath(repo)
    if store:
        out["store"] = os.path.abspath(store)
    out["runtime"] = os.path.abspath(runtime_arg or default_runtime())
    return out


# --------------------------------------------------------------------------- #
# Settings read/write
# --------------------------------------------------------------------------- #

def load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise SystemExit(f"{path}: not valid JSON ({exc}) — refusing to touch it")
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: top level is not an object — refusing to touch it")
    return data


def our_command(label: str | None) -> str:
    cmd = f'"{sys.executable}" "{SCRIPT}"'
    if label:
        cmd += f" --src={label}"
    return cmd


def is_ours(group: object) -> bool:
    """True when a hook group points at this script, wherever it lives."""
    if not isinstance(group, dict):
        return False
    for hook in group.get("hooks") or []:
        if isinstance(hook, dict) and SCRIPT.replace("\\", "/") in str(hook.get("command", "")).replace("\\", "/"):
            return True
    return False


def apply_hooks(settings: dict, label: str | None, remove: bool) -> tuple[dict, int]:
    """Add or drop our entries. Returns (settings, number of groups written)."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    written = 0
    for event in EVENTS:
        groups = [g for g in (hooks.get(event) or []) if not is_ours(g)]
        if not remove:
            groups.append({"hooks": [{"type": "command",
                                      "command": our_command(label),
                                      "timeout": TIMEOUT}]})
            written += 1
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings, written


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_status(targets: list[str]) -> int:
    log(f"hook script : {SCRIPT}")
    log(f"interpreter : {sys.executable}")
    side = load(SIDECAR) if os.path.exists(SIDECAR) else {}
    log(f"sidecar     : {SIDECAR}")
    log(f"              repo={side.get('repo', '<agtmem default>')} "
        f"store={side.get('store', '<agtmem default>')}")
    log(f"              runtime={side.get('runtime', default_runtime() + ' (default)')}")
    log("")
    found = 0
    for path in targets:
        if not os.path.exists(path):
            log(f"[--] {path}  (absent)")
            continue
        settings = load(path)
        hooks = settings.get("hooks") or {}
        mine = [ev for ev in EVENTS
                if any(is_ours(g) for g in (hooks.get(ev) or []))]
        others = sorted(set(hooks) - set(EVENTS))
        found += len(mine)
        mark = "[ok]" if mine == list(EVENTS) else ("[~]" if mine else "[--]")
        log(f"{mark} {path}")
        log(f"     ours: {mine or 'none'}"
            f"{'   other events present: ' + ', '.join(others) if others else ''}")
    log("")
    log("registered in %d place(s)." % found)
    return 0 if found else 1


def cmd_install(args) -> int:
    targets = args.settings or default_targets()
    sidecar = resolve(args.repo, args.store, args.runtime)

    log(f"interpreter : {sys.executable}")
    log(f"hook script : {SCRIPT}")
    log(f"sidecar     : {SIDECAR}")
    for key in ("repo", "store", "runtime"):
        log(f"  {key:<10}: {sidecar.get(key, '<agtmem default — not recorded>')}")
    log("")

    if args.dry_run:
        for path in targets:
            log(f"would write {path}")
        return 0

    write_json(SIDECAR, sidecar)
    log(f"wrote {SIDECAR}")

    for path in targets:
        label = args.src or label_for(path)
        settings = load(path)
        settings, written = apply_hooks(settings, label, remove=False)
        write_json(path, settings)
        log(f"wrote {path}  ({written} event(s), --src={label})")
    log("")
    log("Settings are read when the CLI process starts. A conversation already")
    log("running keeps its old worker — open a new one (or restart the app) for")
    log("the hook to take effect.")
    return 0


def cmd_uninstall(args) -> int:
    targets = args.settings or candidate_settings()
    for path in targets:
        if not os.path.exists(path):
            continue
        settings = load(path)
        settings, _ = apply_hooks(settings, None, remove=True)
        if args.dry_run:
            log(f"would rewrite {path}")
            continue
        write_json(path, settings)
        log(f"cleaned {path}")
    if os.path.exists(SIDECAR) and not args.dry_run:
        os.remove(SIDECAR)
        log(f"removed {SIDECAR}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", action="append", metavar="PATH",
                    help="settings.json to write (repeatable; default: every candidate that exists)")
    ap.add_argument("--src", metavar="LABEL",
                    help="append --src=LABEL, so the hook log says which file fired it")
    ap.add_argument("--repo", metavar="PATH", help="agtmem checkout to record")
    ap.add_argument("--store", metavar="PATH", help="agtmem store to record")
    ap.add_argument("--runtime", metavar="PATH",
                    help="directory for the hook's log and state (default: ~/.workbuddy/hooks)")
    ap.add_argument("--status", action="store_true", help="show what is wired up")
    ap.add_argument("--uninstall", action="store_true", help="remove our entries")
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    args = ap.parse_args()

    if args.status:
        return cmd_status(args.settings or candidate_settings())
    if args.uninstall:
        return cmd_uninstall(args)
    return cmd_install(args)


if __name__ == "__main__":
    raise SystemExit(main())
