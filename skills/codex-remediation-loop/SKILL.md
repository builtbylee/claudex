---
name: codex-remediation-loop
description: Use this skill when you want the full two-phase Claude/Codex remediation workflow for a Markdown implementation plan. Codex refines the plan until approval, the approved plan is frozen, Claude implements against it, and Codex verifies the implementation until resolved or the bounded iteration limits are hit.
---

# Claudex Remediation Loop

Use this when you want the full Claudex `plan refine -> freeze -> implement -> verify` workflow.

## Invocation

In Claude Code:

```text
Use the codex-remediation-loop subagent to run the remediation loop for /absolute/path/to/PLAN.md
```

Direct CLI:

```bash
python3 ~/.claude/tools/codex-remediation-loop/codex_remediation_loop.py loop --plan /absolute/path/to/PLAN.md --cwd /absolute/path/to/workspace --max-iterations 5
```

## What It Does

1. Codex reviews and edits the plan directly until it is approved or the plan loop hits its bound
2. the controller freezes the approved plan as the implementation source of truth
3. Claude implements against the frozen plan
4. the controller runs validation commands
5. Codex verifies the real diff and validation output against the frozen plan
6. the implementation loop stops on resolved, blocked, stagnating, or the bounded iteration limit

## Control Rules

- Codex default model: `gpt-5.4`
- Claude implementation model: `opus`
- Codex may edit the plan only; it never edits code
- Claude implementer gets edit tools only, not shell access
- implementation verification is driven by JSON schemas, stable finding IDs, and the frozen approved-plan snapshot
