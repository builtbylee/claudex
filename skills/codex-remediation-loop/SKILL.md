---
name: codex-remediation-loop
description: Use this skill when you want a fully automated Claude/Codex remediation loop for an implementation plan. It is for risky Markdown plans where Codex should review, Claude should implement, Codex should verify, and the loop should continue until must-fix findings are resolved or the bounded iteration limit is reached.
---

# Codex Remediation Loop

Use this when you want the full bounded `review -> implement -> verify` workflow.

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

1. Codex reviews the plan and emits structured findings
2. Claude edits the repo against those findings
3. the controller runs validation commands
4. Codex verifies the real diff and validation output
5. the loop stops on resolved, blocked, stagnating, or iteration 5

## Control Rules

- Codex default model: `gpt-5.4`
- Claude implementation model: `opus`
- Codex is `read-only`
- Claude implementer gets edit tools only, not shell access
- verification is driven by JSON schemas and stable finding IDs
