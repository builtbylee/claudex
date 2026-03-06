#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

CLAUDE_ROOT = Path.home() / ".claude"
AGENTS_DIR = CLAUDE_ROOT / "agents"
COMMANDS_DIR = CLAUDE_ROOT / "commands"
HOOKS_DIR = CLAUDE_ROOT / "hooks"
TOOLS_DIR = CLAUDE_ROOT / "tools" / "codex-remediation-loop"
SETTINGS_PATH = CLAUDE_ROOT / "settings.json"
HOOK_COMMAND = f"python3 {HOOKS_DIR / 'codex_plan_review.py'}"


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def remove_hook(settings: dict[str, Any]) -> dict[str, Any]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    post_tool_use = hooks.get("PostToolUse")
    if not isinstance(post_tool_use, list):
        return settings
    filtered: list[dict[str, Any]] = []
    for entry in post_tool_use:
        if not isinstance(entry, dict):
            filtered.append(entry)
            continue
        hook_entries = entry.get("hooks")
        if not isinstance(hook_entries, list):
            filtered.append(entry)
            continue
        remaining = [
            hook
            for hook in hook_entries
            if not (isinstance(hook, dict) and hook.get("type") == "command" and hook.get("command") == HOOK_COMMAND)
        ]
        if remaining:
            entry["hooks"] = remaining
            filtered.append(entry)
    hooks["PostToolUse"] = filtered
    return settings


def main() -> int:
    for path in (
        AGENTS_DIR / "codex-plan-review.md",
        AGENTS_DIR / "codex-remediation-loop.md",
        COMMANDS_DIR / "claudex.md",
        HOOKS_DIR / "codex_plan_review.py",
    ):
        path.unlink(missing_ok=True)
    if TOOLS_DIR.exists():
        shutil.rmtree(TOOLS_DIR)
    settings = remove_hook(read_json(SETTINGS_PATH))
    write_json(SETTINGS_PATH, settings)
    print("Removed Claude/Codex remediation workflow from ~/.claude")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
