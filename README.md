# Claude Codex Remediation Loop

Safe Claude Code + Codex workflows for plan review and bounded autonomous remediation.

This package gives you two Claude Code agents:

- `codex-plan-review`: one-shot Codex second opinion on a Markdown plan
- `codex-remediation-loop`: bounded `review -> implement -> verify` loop with a hard stop at 5 iterations

## Best Packaging Model

Not plugin-only.

Plugin metadata helps discovery, but it does not install the user-level Claude assets that make this work:

- `~/.claude/agents`
- `~/.claude/hooks`
- `~/.claude/tools`
- `~/.claude/settings.json`

So the best distribution is:

1. public GitHub repo
2. one-command installer
3. one-command uninstaller
4. optional plugin manifest for discovery

That is what this repo ships.

## Requirements

- `python3`
- `claude` CLI installed and logged in
- `codex` CLI installed and logged in

## Install

Quick install:

```bash
curl -fsSL https://raw.githubusercontent.com/builtbylee/claude-codex-remediation-loop/main/install.sh | bash
```

Local install from a clone:

```bash
./install.sh
```

What it installs:

- `~/.claude/agents/codex-plan-review.md`
- `~/.claude/agents/codex-remediation-loop.md`
- `~/.claude/hooks/codex_plan_review.py`
- `~/.claude/tools/codex-remediation-loop/`
- automatic plan-review hook merged into `~/.claude/settings.json`

## Verify

In Claude Code:

1. run `/agents`
2. confirm these appear:
   - `codex-plan-review`
   - `codex-remediation-loop`

## Use

One-shot review:

```text
Use the codex-plan-review subagent to review /absolute/path/to/PLAN.md
```

Bounded remediation loop:

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

## Behavior

### `codex-plan-review`

- reads the real plan file from disk
- runs `codex exec` in `read-only`
- never uses dangerous sandbox bypass flags
- injects the second opinion back into Claude Code via structured hook output

### `codex-remediation-loop`

- Codex reviews the plan
- Claude implements against the findings
- validation commands run automatically
- Codex verifies the actual diff and validation output
- loop stops on:
  - resolved
  - blocked
  - stagnation
  - iteration 5

## Workspace Overrides

If auto-detected validation commands are wrong for a repo, add one of:

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

## Security Properties

- Codex runs in `read-only`
- Claude implementer has edit tools only; no shell access
- failures are explicit
- no silent “second opinion happened” behavior
- plan review cache is content-hash keyed

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/builtbylee/claude-codex-remediation-loop/main/uninstall.sh | bash
```

Or locally:

```bash
./uninstall.sh
```

## Repo Layout

- `agents/`: Claude Code agents installed into `~/.claude/agents`
- `hooks/`: safe automatic plan-review hook
- `tools/codex-remediation-loop/`: bounded controller + JSON schemas
- `skills/`: optional skill metadata and docs
- `.claude-plugin/`: optional plugin manifest
- `scripts/`: installer and uninstaller
- `tests/`: unit tests
