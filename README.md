# Claude Codex Remediation Loop

[![Test](https://github.com/builtbylee/claude-codex-remediation-loop/actions/workflows/test.yml/badge.svg)](https://github.com/builtbylee/claude-codex-remediation-loop/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-black.svg)](./LICENSE)

Make Claude Code work against an independent verifier instead of its own self-confidence.

| Agent | Best for | Result |
| --- | --- | --- |
| `codex-plan-review` | Getting a second opinion before approval | Codex reviews the plan and returns findings |
| `codex-remediation-loop` | Driving a risky plan to completion | Codex reviews, Claude edits, Codex verifies, controller stops on resolution or failure |

## Why Use It

- independent review instead of self-approval
- bounded automation with a hard stop at 5 iterations
- verification against actual file changes and validation output
- explicit failure modes: blocked, stagnating, max-iterations, review unavailable
- safe defaults: Codex runs `read-only`; Claude implementer gets edit tools only

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/builtbylee/claude-codex-remediation-loop/main/install.sh | bash
```

Requirements:

- `python3`
- `claude` CLI installed and logged in
- `codex` CLI installed and logged in
- macOS or Linux shell environment

## Verify

In Claude Code:

1. run `/agents`
2. confirm these appear:
   - `codex-plan-review`
   - `codex-remediation-loop`
3. if they do not appear immediately, restart Claude Code once and run `/agents` again

![Claude Code agents view](./assets/agents-view.png)

## Remediation Loop Workflow

```mermaid
flowchart LR
    A["Plan file"] --> B["Codex review<br/>structured findings"]
    B --> C["Claude implementation pass"]
    C --> D["Validation<br/>test, lint, build"]
    D --> E["Codex verification<br/>diff + validation output"]
    E --> F{"Resolved?"}
    F -- Yes --> G["Stop: resolved"]
    F -- No --> H{"Blocked, stagnating,<br/>or iteration 5?"}
    H -- Yes --> I["Stop: explicit reason"]
    H -- No --> C
```

## Use

One-shot plan review:

```text
Use the codex-plan-review subagent to review /absolute/path/to/PLAN.md
```

Full remediation loop:

```text
Use the codex-remediation-loop subagent to run the remediation loop for /absolute/path/to/PLAN.md
```

Direct CLI:

```bash
python3 ~/.claude/tools/codex-remediation-loop/codex_remediation_loop.py loop \
  --plan /absolute/path/to/PLAN.md \
  --cwd /absolute/path/to/workspace \
  --max-iterations 5
```

## What Gets Installed

- `~/.claude/agents/codex-plan-review.md`
- `~/.claude/agents/codex-remediation-loop.md`
- `~/.claude/hooks/codex_plan_review.py`
- `~/.claude/tools/codex-remediation-loop/`
- automatic plan-review hook merged into `~/.claude/settings.json`

## Workspace Overrides

If validation auto-detection is wrong for a repo, add either:

- `.claude-codex-loop.json`
- `.claude/codex-remediation-loop.json`

Example:

```json
{
  "validation_commands": [
    "pnpm lint",
    "pnpm test",
    "pnpm build"
  ],
  "codex_model": "gpt-5.4",
  "claude_model": "opus"
}
```

## Safety Model

- Codex runs in `read-only`
- Claude implementer does not get shell access
- plan review reads the real plan file, not a Claude summary
- failures are explicit; the system does not pretend a review happened
- review results are cached by content hash

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/builtbylee/claude-codex-remediation-loop/main/uninstall.sh | bash
```

Or locally:

```bash
./uninstall.sh
```

## Notes

- the installer is shell-based; Windows users would need WSL or a manual install path
- the remediation loop depends on both local CLIs working: `claude` and `codex`
