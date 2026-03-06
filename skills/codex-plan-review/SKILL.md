---
name: codex-plan-review
description: Use this skill when a Claude Code plan file needs a Codex second opinion. It is for implementation plans, migration plans, rollout plans, or any risky Markdown plan where you want a concrete review focused on hidden assumptions, security, testing, rollback, and operational gaps.
---

# Codex Plan Review

Use this when you want a safe Codex second opinion on a Markdown plan file before approval or implementation.

## Invocation

In Claude Code:

```text
Use the codex-plan-review subagent to review /absolute/path/to/PLAN.md
```

## What It Does

1. resolves the plan path
2. runs `python3 ~/.claude/hooks/codex_plan_review.py --manual ...`
3. calls Codex in `read-only`
4. returns Codex's review first, then a short operator summary

## Safety

- no dangerous bypass flags
- explicit failure if Codex is unavailable
- real plan file contents are sent to Codex, not a Claude summary
