---
description: Run the full Claudex two-phase plan approval and implementation loop
---
Use the `codex-remediation-loop` subagent to run the Claudex workflow.

Rules:
- If `$1` is provided, treat it as the absolute or workspace-relative path to the target plan file.
- If `$1` is missing and there is exactly one plausible plan file in the current workspace (`PLAN.md`, `IMPLEMENTATION_PLAN.md`, `EXECUTION_PLAN.md`, `ARCHITECTURE_PLAN.md`), use it.
- If there is more than one plausible plan file and no explicit argument, stop and ask which file to use. Do not guess.
- Return the controller result succinctly.

Target plan: `$1`
