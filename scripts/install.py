#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
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


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def ensure_hook(settings: dict[str, Any]) -> dict[str, Any]:
    hooks = settings.setdefault("hooks", {})
    post_tool_use = hooks.setdefault("PostToolUse", [])
    if not isinstance(post_tool_use, list):
        hooks["PostToolUse"] = []
        post_tool_use = hooks["PostToolUse"]

    for entry in post_tool_use:
        if not isinstance(entry, dict):
            continue
        if entry.get("matcher") != "Write|Edit|MultiEdit":
            continue
        hook_entries = entry.get("hooks")
        if not isinstance(hook_entries, list):
            continue
        for hook in hook_entries:
            if isinstance(hook, dict) and hook.get("type") == "command" and hook.get("command") == HOOK_COMMAND:
                hook["timeout"] = 180
                return settings

    post_tool_use.append(
        {
            "matcher": "Write|Edit|MultiEdit",
            "hooks": [
                {
                    "type": "command",
                    "command": HOOK_COMMAND,
                    "timeout": 180,
                }
            ],
        }
    )
    return settings


def main() -> int:
    copy_file(ROOT / "agents" / "codex-plan-review.md", AGENTS_DIR / "codex-plan-review.md")
    copy_file(ROOT / "agents" / "codex-remediation-loop.md", AGENTS_DIR / "codex-remediation-loop.md")
    copy_file(ROOT / "commands" / "claudex.md", COMMANDS_DIR / "claudex.md")
    copy_file(ROOT / "hooks" / "codex_plan_review.py", HOOKS_DIR / "codex_plan_review.py")
    copy_tree(ROOT / "tools" / "codex-remediation-loop", TOOLS_DIR)
    settings = ensure_hook(read_json(SETTINGS_PATH))
    write_json(SETTINGS_PATH, settings)
    print("Installed Claude/Codex remediation workflow into ~/.claude")
    print("Run /agents in Claude Code and look for codex-plan-review and codex-remediation-loop.")
    print("Run /claudex <plan-path> in Claude Code to trigger the full workflow.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
