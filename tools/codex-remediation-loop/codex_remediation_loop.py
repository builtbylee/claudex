#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import hashlib
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

DEFAULT_MAX_ITERATIONS = 5
DEFAULT_CODEX_MODEL = "gpt-5.4"
DEFAULT_CLAUDE_MODEL = "opus"
DEFAULT_CODEX_TIMEOUT_SECONDS = 180
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 600
DEFAULT_VALIDATION_TIMEOUT_SECONDS = 300
DEFAULT_MAX_PROMPT_CHARS = 30_000
DEFAULT_STATE_ROOT = Path.home() / ".claude" / "state" / "codex-remediation-loop"
DEFAULT_ALLOWED_TOOLS = "Read,Write,Edit,MultiEdit,Glob,Grep"
PLAN_CACHE_VERSION = "2026-03-06"
IGNORE_DIRS = {
    ".git",
    ".idea",
    ".next",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "tmp",
    "vendor",
    "venv",
}
IGNORE_SUFFIXES = {".pyc", ".pyo", ".so", ".dll", ".dylib", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip"}


@dataclass(frozen=True)
class RunPaths:
    root: Path
    meta: Path
    final_summary_json: Path
    final_summary_md: Path
    approved_plan: Path
    approved_plan_meta: Path

    def plan_iteration_dir(self, iteration: int) -> Path:
        return self.root / "plan-phase" / f"iteration-{iteration:02d}"

    def implementation_iteration_dir(self, iteration: int) -> Path:
        return self.root / "implementation-phase" / f"iteration-{iteration:02d}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def relative_schema_dir() -> Path:
    return Path(__file__).resolve().parent / "schemas"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plan_cache_key(plan_body: str, codex_model: str) -> str:
    return sha256_text(f"{PLAN_CACHE_VERSION}\n{codex_model}\n{plan_body}")


def plan_cache_path(key: str) -> Path:
    return DEFAULT_STATE_ROOT / "plan-cache" / f"{key}.json"


def load_plan_cache(*, plan_body: str, codex_model: str) -> dict[str, Any] | None:
    path = plan_cache_path(plan_cache_key(plan_body, codex_model))
    if not path.exists():
        return None
    loaded = read_json(path)
    approved_plan_body = loaded.get("approved_plan_body")
    approved_plan_sha256 = loaded.get("approved_plan_sha256")
    if not isinstance(approved_plan_body, str) or not isinstance(approved_plan_sha256, str):
        return None
    if sha256_text(approved_plan_body) != approved_plan_sha256:
        return None
    return loaded


def write_plan_cache(*, source_plan_body: str, approved_plan_body: str, codex_model: str) -> None:
    payload = {
        "cache_version": PLAN_CACHE_VERSION,
        "cached_at": utc_now(),
        "codex_model": codex_model,
        "source_plan_sha256": sha256_text(source_plan_body),
        "approved_plan_body": approved_plan_body,
        "approved_plan_sha256": sha256_text(approved_plan_body),
    }
    cache_keys = {plan_cache_key(source_plan_body, codex_model), plan_cache_key(approved_plan_body, codex_model)}
    for key in cache_keys:
        write_json(plan_cache_path(key), payload)


def compact_plan_review(review: dict[str, Any] | None) -> dict[str, Any] | None:
    if review is None:
        return None
    return {
        "iteration": review.get("iteration"),
        "overall_status": review.get("overall_status"),
        "summary": review.get("summary"),
        "must_fix_count": plan_must_fix_count(review),
        "findings": [
            {
                "id": item.get("id"),
                "severity": item.get("severity"),
                "category": item.get("category"),
                "title": item.get("title"),
                "acceptance_criteria": item.get("acceptance_criteria", []),
            }
            for item in review.get("findings", [])[:8]
        ],
        "next_actions": review.get("next_actions", [])[:8],
    }


def compact_verification(verification: dict[str, Any] | None) -> dict[str, Any] | None:
    if verification is None:
        return None
    return {
        "iteration": verification.get("iteration"),
        "overall_status": verification.get("overall_status"),
        "ready_to_approve": verification.get("ready_to_approve"),
        "validation_status": verification.get("validation_status"),
        "summary": verification.get("summary"),
        "unresolved": [
            {
                "id": item.get("id"),
                "severity": item.get("severity"),
                "category": item.get("category"),
                "reason": item.get("reason"),
                "missing_acceptance_criteria": item.get("missing_acceptance_criteria", []),
            }
            for item in verification.get("unresolved", [])[:8]
        ],
        "regressions": [
            {
                "id": item.get("id"),
                "severity": item.get("severity"),
                "category": item.get("category"),
                "reason": item.get("reason"),
            }
            for item in verification.get("regressions", [])[:8]
        ],
        "next_actions": verification.get("next_actions", [])[:8],
    }


def workspace_config_path(workspace: Path) -> Path | None:
    candidates = [
        workspace / ".claude-codex-loop.json",
        workspace / ".claude" / "codex-remediation-loop.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_workspace_config(workspace: Path) -> dict[str, Any]:
    config_path = workspace_config_path(workspace)
    if config_path is None:
        return {}
    loaded = json.loads(config_path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def plan_text(path: Path, max_chars: int = DEFAULT_MAX_PROMPT_CHARS) -> tuple[str, bool]:
    text = path.read_text(encoding="utf-8")
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def run_command(
    cmd: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def run_codex_structured(
    *,
    prompt: str,
    schema_path: Path,
    cwd: Path,
    model: str,
    timeout_seconds: int,
    sandbox: str,
) -> tuple[dict[str, Any] | None, str | None]:
    output_path = cwd / ".codex-last-message.json"
    cmd = [
        "codex",
        "exec",
        "-s",
        sandbox,
        "--skip-git-repo-check",
        "--ephemeral",
        "--color",
        "never",
        "-C",
        str(cwd),
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
        "-m",
        model,
        "-",
    ]
    try:
        completed = run_command(cmd, cwd=cwd, input_text=prompt, timeout=timeout_seconds)
    except FileNotFoundError:
        output_path.unlink(missing_ok=True)
        return None, "Codex CLI is not installed or not on PATH."
    except subprocess.TimeoutExpired:
        output_path.unlink(missing_ok=True)
        return None, "Codex CLI timed out."
    raw_output = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
    output_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        return None, f"Codex failed: {stderr}"
    if not raw_output:
        return None, "Codex returned no structured output."
    try:
        parsed = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        return None, f"Codex returned invalid JSON: {exc}"
    return parsed if isinstance(parsed, dict) else None, None


def run_claude_implementer(
    *,
    prompt: str,
    workspace: Path,
    model: str,
    timeout_seconds: int,
    allowed_tools: str,
) -> tuple[str, str | None]:
    cmd = [
        "claude",
        "-p",
        "--model",
        model,
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        allowed_tools,
        "--setting-sources",
        "project,local",
        "--no-session-persistence",
    ]
    try:
        completed = run_command(cmd, cwd=workspace, input_text=prompt, timeout=timeout_seconds)
    except FileNotFoundError:
        return "", "Claude CLI is not installed or not on PATH."
    except subprocess.TimeoutExpired:
        return "", "Claude CLI timed out."
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        return completed.stdout.strip(), f"Claude implementer failed: {stderr}"
    return completed.stdout.strip(), None


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in IGNORE_SUFFIXES:
        return False
    if path.stat().st_size > 2_000_000:
        return False
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\x00" not in chunk


def workspace_manifest(workspace: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [name for name in dirs if name not in IGNORE_DIRS]
        base = Path(root)
        for file_name in files:
            path = base / file_name
            rel = path.relative_to(workspace).as_posix()
            lowered = path.name.lower()
            if lowered.startswith(".env") or path.suffix.lower() == ".local":
                continue
            if any(token in lowered for token in ("secret", "credential")):
                continue
            if not is_text_file(path):
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest[rel] = digest
    return manifest


def changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changed = set(before) ^ set(after)
    for path, digest in after.items():
        if before.get(path) != digest:
            changed.add(path)
    return sorted(changed)


def git_repo(workspace: Path) -> bool:
    completed = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(workspace),
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def git_diff(workspace: Path, changed: list[str]) -> str:
    if not changed or not git_repo(workspace):
        return ""
    cmd = ["git", "diff", "--no-ext-diff", "--"] + changed
    completed = subprocess.run(cmd, cwd=str(workspace), text=True, capture_output=True, check=False)
    return completed.stdout[:20_000]


def untracked_changed_files(workspace: Path, changed: list[str]) -> list[str]:
    if not changed or not git_repo(workspace):
        return []
    cmd = ["git", "ls-files", "--others", "--exclude-standard", "--"] + changed
    completed = subprocess.run(cmd, cwd=str(workspace), text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        return []
    return sorted(line.strip() for line in completed.stdout.splitlines() if line.strip())


def snapshot_reason(workspace: Path, changed: list[str], diff_text: str) -> str | None:
    if not changed:
        return None
    if not git_repo(workspace):
        return "workspace is not a git repository"
    untracked = untracked_changed_files(workspace, changed)
    if untracked:
        preview = ", ".join(untracked[:6])
        suffix = "..." if len(untracked) > 6 else ""
        return f"changed files include untracked paths: {preview}{suffix}"
    if not diff_text.strip():
        return "git diff is empty or unavailable for the changed files"
    return None


def text_diff(before: str, after: str, *, from_name: str, to_name: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=from_name,
            tofile=to_name,
        )
    )


def file_snapshots(workspace: Path, changed: list[str], *, max_files: int = 12, max_chars: int = 5_000) -> list[dict[str, str]]:
    snapshots: list[dict[str, str]] = []
    for rel in changed[:max_files]:
        path = workspace / rel
        if not path.exists() or not path.is_file() or not is_text_file(path):
            continue
        text = path.read_text(encoding="utf-8")
        snapshots.append(
            {
                "path": rel,
                "content": text[:max_chars] + ("\n[truncated]" if len(text) > max_chars else ""),
            }
        )
    return snapshots


def detect_validation_commands(workspace: Path, config: dict[str, Any]) -> list[dict[str, str]]:
    configured = config.get("validation_commands")
    if isinstance(configured, list) and all(isinstance(item, str) for item in configured):
        return [{"kind": "custom", "command": item} for item in configured]

    commands: list[dict[str, str]] = []
    package_json = workspace / "package.json"
    if package_json.exists():
        package = json.loads(package_json.read_text(encoding="utf-8"))
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        if isinstance(scripts, dict):
            runner = "pnpm" if (workspace / "pnpm-lock.yaml").exists() else "npm"
            for name in ("lint", "typecheck", "test", "build"):
                if isinstance(scripts.get(name), str):
                    commands.append({"kind": name, "command": f"{runner} run {name}"})

    if (workspace / "Cargo.toml").exists():
        commands.append({"kind": "test", "command": "cargo test --quiet"})
    if (workspace / "go.mod").exists():
        commands.append({"kind": "test", "command": "go test ./..."})
    python_markers = ["pyproject.toml", "pytest.ini", "tox.ini", "setup.py"]
    if any((workspace / marker).exists() for marker in python_markers) or (workspace / "tests").exists():
        commands.append({"kind": "test", "command": "python3 -m pytest -q"})
    return commands


def run_validation_command(workspace: Path, entry: dict[str, str], timeout_seconds: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            entry["command"],
            cwd=str(workspace),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "kind": entry["kind"],
            "command": entry["command"],
            "passed": completed.returncode == 0,
            "exit_code": completed.returncode,
            "stdout_tail": completed.stdout[-4_000:],
            "stderr_tail": completed.stderr[-4_000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "kind": entry["kind"],
            "command": entry["command"],
            "passed": False,
            "exit_code": None,
            "stdout_tail": (exc.stdout or "")[-4_000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-4_000:] if isinstance(exc.stderr, str) else "",
            "error": f"Timed out after {timeout_seconds}s",
        }


def run_validation_commands(workspace: Path, commands: list[dict[str, str]], timeout_seconds: int) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    if not commands:
        return {"status": "skipped", "commands": results}
    indexed_commands = list(enumerate(commands))
    parallel_commands = [(index, entry) for index, entry in indexed_commands if entry.get("kind") != "build"]
    sequential_commands = [(index, entry) for index, entry in indexed_commands if entry.get("kind") == "build"]
    result_map: dict[int, dict[str, Any]] = {}

    if parallel_commands:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(parallel_commands))) as executor:
            future_map = {
                executor.submit(run_validation_command, workspace, entry, timeout_seconds): index
                for index, entry in parallel_commands
            }
            for future in concurrent.futures.as_completed(future_map):
                result = future.result()
                result_map[future_map[future]] = result

    for index, entry in sequential_commands:
        result = run_validation_command(workspace, entry, timeout_seconds)
        result_map[index] = result

    overall_passed = True
    for index, _entry in indexed_commands:
        result = result_map[index]
        overall_passed = overall_passed and bool(result["passed"])
        results.append(result)
    return {"status": "passed" if overall_passed else "failed", "commands": results}


def plan_review_prompt(
    *,
    sandbox_plan_path: Path,
    current_plan_body: str,
    truncated: bool,
    latest_plan_review: dict[str, Any] | None,
    iteration: int,
) -> str:
    truncation = "The plan body was truncated for prompt size. Edit the actual file in the workspace, not the truncated excerpt.\n\n" if truncated else ""
    return (
        "You are refining a Markdown implementation plan.\n"
        "You may edit only the plan file in the current workspace. Do not create or modify any other files.\n"
        "Update the plan directly until it is approval-ready. Then return only structured output matching the schema.\n"
        "Use overall_status values as follows:\n"
        "- approved: the edited plan is ready to freeze and implement\n"
        "- continue: you improved the plan but meaningful issues still remain\n"
        "- blocked: the plan cannot be brought to approval without missing requirements or contradictory constraints\n"
        "Assign one category to every finding: structural_mismatch, missing_logic, wrong_location, or behavioral_divergence.\n"
        "Reuse previous finding IDs when the same issue persists.\n"
        "If you fix a finding in the plan, remove it from the output instead of renaming it.\n\n"
        f"Iteration: {iteration}\n"
        f"Editable plan file: {sandbox_plan_path.name}\n\n"
        f"{truncation}"
        "Current plan excerpt:\n"
        f"{current_plan_body}\n\n"
        "Previous Codex plan review summary:\n"
        f"{json.dumps(compact_plan_review(latest_plan_review), indent=2) if latest_plan_review else 'null'}"
    )


def implementation_prompt(
    *,
    plan_path: Path,
    approved_plan_body: str,
    latest_verification: dict[str, Any] | None,
    iteration: int,
) -> str:
    verification_summary = compact_verification(latest_verification)
    verification_context = (
        "This is the first implementation pass. Focus on implementing the frozen approved plan completely.\n"
        if latest_verification is None
        else "Resolve all remaining must-fix items before touching should-fix or advisory items.\n"
    )
    return (
        "You are executing a bounded implementation pass against a frozen approved plan.\n"
        "The approved plan is the source of truth. Implement it in the workspace.\n"
        f"Do not edit the plan file at {plan_path}. The controller will revert it if you change it.\n"
        "Do not run tests, linters, build commands, or shell commands. The controller will do that.\n"
        "Read relevant files before editing. Prefer the smallest coherent set of code changes that satisfies the approved plan and all open must-fix items.\n"
        "Use the finding categories to fix the right class of problem:\n"
        "- structural_mismatch: the change does not match the approved plan structure\n"
        "- missing_logic: behavior or logic is incomplete\n"
        "- wrong_location: the change landed in the wrong file or layer\n"
        "- behavioral_divergence: the code exists but does not satisfy the intended behavior\n"
        "If the approved plan is contradictory or impossible from the repo context, say that plainly in your short response.\n"
        "Respond with a short plain-text summary only.\n\n"
        f"Iteration: {iteration}\n"
        f"Frozen plan file: {plan_path}\n\n"
        f"{verification_context}\n"
        "Frozen approved plan:\n"
        f"{approved_plan_body}\n\n"
        "Latest verification summary:\n"
        f"{json.dumps(verification_summary, indent=2) if verification_summary else 'null'}"
    )


def verification_prompt(
    *,
    plan_path: Path,
    approved_plan_body: str,
    latest_verification: dict[str, Any] | None,
    validation: dict[str, Any],
    changed: list[str],
    diff_text: str,
    snapshots: list[dict[str, str]],
    snapshot_context: str,
    iteration: int,
    plan_mutation_detected: bool,
) -> str:
    return (
        "Verify whether the implementation work satisfies the frozen approved plan.\n"
        "Return only structured output matching the schema.\n"
        "Use original unresolved IDs exactly when they persist. You may add regression IDs like R1 for new regressions.\n"
        "Assign one category to every unresolved item and regression: structural_mismatch, missing_logic, wrong_location, or behavioral_divergence.\n"
        "Be strict: unresolved means unresolved. blocked means further progress requires missing context or contradictory requirements.\n\n"
        f"Iteration: {iteration}\n"
        f"Frozen plan path: {plan_path}\n"
        f"Frozen plan mutated during implementation pass: {str(plan_mutation_detected).lower()}\n\n"
        "Frozen approved plan:\n"
        f"{approved_plan_body}\n\n"
        "Previous implementation verification summary:\n"
        f"{json.dumps(compact_verification(latest_verification), indent=2) if latest_verification else 'null'}\n\n"
        "Validation results:\n"
        f"{json.dumps(validation, indent=2)}\n\n"
        "Changed files:\n"
        f"{json.dumps(changed, indent=2)}\n\n"
        "Git diff:\n"
        f"{diff_text or '[no git diff available]'}\n\n"
        "File snapshot context:\n"
        f"{snapshot_context}\n"
        f"{json.dumps(snapshots, indent=2) if snapshots else '[]'}"
    )


def plan_must_fix_count(review: dict[str, Any]) -> int:
    return sum(1 for item in review.get("findings", []) if item.get("severity") == "must_fix")


def unresolved_must_fix_count(verification: dict[str, Any]) -> int:
    unresolved = verification.get("unresolved", [])
    regressions = verification.get("regressions", [])
    count = sum(1 for item in unresolved if item.get("severity") == "must_fix")
    count += sum(1 for item in regressions if item.get("severity") == "must_fix")
    return count


def plan_signature(review: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(str(item.get("id")) for item in review.get("findings", []) if item.get("severity") == "must_fix"))


def verification_signature(verification: dict[str, Any]) -> tuple[str, ...]:
    unresolved_ids = [str(item.get("id")) for item in verification.get("unresolved", []) if item.get("severity") == "must_fix"]
    regression_ids = [str(item.get("id")) for item in verification.get("regressions", []) if item.get("severity") == "must_fix"]
    return tuple(sorted(unresolved_ids + regression_ids))


def stagnation_rounds(
    records: list[dict[str, Any]],
    *,
    count_fn: Callable[[dict[str, Any]], int],
    signature_fn: Callable[[dict[str, Any]], tuple[str, ...]],
) -> int:
    if len(records) < 2:
        return 0
    rounds = 0
    for index in range(len(records) - 1, 0, -1):
        newer = records[index]
        older = records[index - 1]
        if count_fn(newer) >= count_fn(older) and signature_fn(newer) == signature_fn(older):
            rounds += 1
        else:
            break
    return rounds


def plan_controller_decision(
    *,
    reviews: list[dict[str, Any]],
    iteration: int,
    max_iterations: int,
) -> dict[str, Any]:
    latest = reviews[-1]
    if latest.get("overall_status") == "approved" and plan_must_fix_count(latest) == 0:
        return {"action": "approve_plan", "reason": "Codex approved the plan and no must-fix findings remain."}
    if iteration >= max_iterations:
        return {"action": "stop_plan_max_iterations", "reason": f"Reached max plan iterations ({max_iterations})."}
    if latest.get("overall_status") == "blocked":
        return {"action": "stop_plan_blocked", "reason": latest.get("summary", "Plan approval is blocked.")}
    if stagnation_rounds(reviews, count_fn=plan_must_fix_count, signature_fn=plan_signature) >= 2:
        return {"action": "stop_plan_stagnating", "reason": "Plan must-fix findings did not improve for two consecutive plan-review rounds."}
    return {"action": "continue", "reason": "Plan still has unresolved issues but further refinement is possible."}


def implementation_controller_decision(
    *,
    verifications: list[dict[str, Any]],
    iteration: int,
    max_iterations: int,
) -> dict[str, Any]:
    latest = verifications[-1]
    validation_status = latest.get("validation_status")
    has_regression = bool(latest.get("regressions"))
    ready_to_approve = bool(latest.get("ready_to_approve"))

    if unresolved_must_fix_count(latest) == 0 and not has_regression and ready_to_approve and validation_status in {"passed", "skipped"}:
        return {"action": "stop_resolved", "reason": "All must-fix findings resolved and no regressions remain."}
    if iteration >= max_iterations:
        return {"action": "stop_max_iterations", "reason": f"Reached max implementation iterations ({max_iterations})."}
    if latest.get("overall_status") == "blocked":
        return {"action": "stop_blocked", "reason": latest.get("summary", "Implementation verification blocked progress.")}
    if stagnation_rounds(verifications, count_fn=unresolved_must_fix_count, signature_fn=verification_signature) >= 2:
        return {"action": "stop_stagnating", "reason": "Implementation must-fix findings did not improve for two consecutive verification rounds."}
    return {"action": "continue", "reason": "Unresolved must-fix findings remain and progress is still possible."}


def run_paths(root: Path) -> RunPaths:
    return RunPaths(
        root=root,
        meta=root / "run.json",
        final_summary_json=root / "final-summary.json",
        final_summary_md=root / "final-summary.md",
        approved_plan=root / "approved-plan.md",
        approved_plan_meta=root / "approved-plan.json",
    )


def init_run(
    plan: Path,
    workspace: Path,
    *,
    max_plan_iterations: int,
    max_implementation_iterations: int,
) -> RunPaths:
    root = DEFAULT_STATE_ROOT / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=False)
    paths = run_paths(root)
    write_json(
        paths.meta,
        {
            "id": root.name,
            "created_at": utc_now(),
            "plan_path": str(plan),
            "workspace": str(workspace),
            "max_plan_iterations": max_plan_iterations,
            "max_implementation_iterations": max_implementation_iterations,
        },
    )
    return paths


def stage_plan_workspace(plan: Path, sandbox_root: Path) -> Path:
    sandbox_root.mkdir(parents=True, exist_ok=True)
    sandbox_plan = sandbox_root / plan.name
    sandbox_plan.write_text(plan.read_text(encoding="utf-8"), encoding="utf-8")
    return sandbox_plan


def freeze_approved_plan(paths: RunPaths, plan: Path, approved_body: str, *, plan_iteration: int, codex_model: str) -> dict[str, Any]:
    write_text(paths.approved_plan, approved_body)
    plan.write_text(approved_body, encoding="utf-8")
    metadata = {
        "frozen_at": utc_now(),
        "source_plan_path": str(plan),
        "approved_plan_path": str(paths.approved_plan),
        "approved_plan_sha256": hashlib.sha256(approved_body.encode("utf-8")).hexdigest(),
        "plan_iteration": plan_iteration,
        "codex_model": codex_model,
    }
    write_json(paths.approved_plan_meta, metadata)
    return metadata


def append_plan_mutation_regression(verification: dict[str, Any], plan_path: Path) -> None:
    regressions = verification.setdefault("regressions", [])
    regressions.append(
        {
            "id": "R_PLAN_MUTATION",
            "severity": "must_fix",
            "category": "structural_mismatch",
            "title": "Frozen approved plan was modified",
            "reason": f"Claude changed the frozen approved plan during implementation: {plan_path}",
        }
    )
    summary = verification.get("summary", "")
    extra = "Frozen approved plan was modified during implementation and was restored by the controller."
    verification["summary"] = f"{summary} {extra}".strip()
    actions = verification.setdefault("next_actions", [])
    actions.append("Do not edit the frozen approved plan file during implementation.")


def write_final_summary(paths: RunPaths, summary: dict[str, Any]) -> None:
    write_json(paths.final_summary_json, summary)
    lines = [
        "# Claudex",
        "",
        f"Status: {summary.get('status')}",
        "",
        f"Reason: {summary.get('reason')}",
        "",
        f"Run dir: {summary.get('run_dir')}",
        "",
        f"Plan iterations used: {summary.get('plan_iterations_used', 0)}",
        "",
        f"Implementation iterations used: {summary.get('implementation_iterations_used', 0)}",
        "",
        f"Unresolved must-fix: {summary.get('unresolved_must_fix_count', 0)}",
    ]
    if "plan_cache_hit" in summary:
        lines.extend(["", f"Plan cache hit: {str(bool(summary['plan_cache_hit'])).lower()}"])
    if summary.get("approved_plan_path"):
        lines.extend(["", f"Approved plan: {summary['approved_plan_path']}"])
    write_text(paths.final_summary_md, "\n".join(lines) + "\n")


def run_loop(
    plan: Path,
    workspace: Path,
    max_plan_iterations: int,
    max_implementation_iterations: int,
) -> dict[str, Any]:
    config = load_workspace_config(workspace)
    paths = init_run(
        plan,
        workspace,
        max_plan_iterations=max_plan_iterations,
        max_implementation_iterations=max_implementation_iterations,
    )
    codex_model = str(config.get("codex_model") or DEFAULT_CODEX_MODEL)
    claude_model = str(config.get("claude_model") or DEFAULT_CLAUDE_MODEL)
    allowed_tools = str(config.get("claude_allowed_tools") or DEFAULT_ALLOWED_TOOLS)
    original_plan_body = plan.read_text(encoding="utf-8")
    plan_cache = load_plan_cache(plan_body=original_plan_body, codex_model=codex_model)

    plan_reviews: list[dict[str, Any]] = []
    approved_plan_body: str | None = None
    approved_plan_meta: dict[str, Any] | None = None
    plan_cache_hit = False

    if plan_cache is not None:
        approved_plan_body = str(plan_cache["approved_plan_body"])
        approved_plan_meta = freeze_approved_plan(paths, plan, approved_plan_body, plan_iteration=0, codex_model=codex_model)
        approved_plan_meta["plan_cache_hit"] = True
        write_json(paths.approved_plan_meta, approved_plan_meta)
        write_json(
            paths.root / "plan-phase" / "cache-hit.json",
            {
                "cached_at": plan_cache.get("cached_at"),
                "source_plan_sha256": plan_cache.get("source_plan_sha256"),
                "approved_plan_sha256": plan_cache.get("approved_plan_sha256"),
            },
        )
        plan_cache_hit = True

    for iteration in range(1, max_plan_iterations + 1):
        if approved_plan_body is not None and approved_plan_meta is not None:
            break
        iteration_dir = paths.plan_iteration_dir(iteration)
        sandbox_root = iteration_dir / "sandbox"
        sandbox_plan = stage_plan_workspace(plan, sandbox_root)
        current_plan_body, truncated = plan_text(plan)
        write_text(iteration_dir / "plan-before.md", current_plan_body)

        prompt = plan_review_prompt(
            sandbox_plan_path=sandbox_plan,
            current_plan_body=current_plan_body,
            truncated=truncated,
            latest_plan_review=plan_reviews[-1] if plan_reviews else None,
            iteration=iteration,
        )
        write_text(iteration_dir / "codex-prompt.txt", prompt)
        review_data, review_error = run_codex_structured(
            prompt=prompt,
            schema_path=relative_schema_dir() / "plan-edit.schema.json",
            cwd=sandbox_root,
            model=codex_model,
            timeout_seconds=DEFAULT_CODEX_TIMEOUT_SECONDS,
            sandbox="workspace-write",
        )
        if review_error or review_data is None:
            summary = {
                "status": "failed",
                "reason": review_error or "Codex plan refinement failed.",
                "run_dir": str(paths.root),
                "plan_iterations_used": iteration - 1,
                "implementation_iterations_used": 0,
                "unresolved_must_fix_count": plan_must_fix_count(plan_reviews[-1]) if plan_reviews else 0,
                "plan_cache_hit": plan_cache_hit,
            }
            write_final_summary(paths, summary)
            return summary

        updated_plan_body = sandbox_plan.read_text(encoding="utf-8")
        write_text(iteration_dir / "plan-after.md", updated_plan_body)
        write_text(
            iteration_dir / "plan-diff.patch",
            text_diff(current_plan_body, updated_plan_body, from_name=f"{plan.name} (before)", to_name=f"{plan.name} (after)"),
        )
        plan.write_text(updated_plan_body, encoding="utf-8")
        review_data["iteration"] = iteration
        review_data["changed_plan"] = current_plan_body != updated_plan_body
        write_json(iteration_dir / "plan-review.json", review_data)
        plan_reviews.append(review_data)

        decision = plan_controller_decision(reviews=plan_reviews, iteration=iteration, max_iterations=max_plan_iterations)
        write_json(iteration_dir / "controller-decision.json", decision)
        if decision["action"] == "approve_plan":
            approved_plan_body = updated_plan_body
            approved_plan_meta = freeze_approved_plan(paths, plan, approved_plan_body, plan_iteration=iteration, codex_model=codex_model)
            write_plan_cache(source_plan_body=original_plan_body, approved_plan_body=approved_plan_body, codex_model=codex_model)
            break
        if decision["action"] != "continue":
            summary = {
                "status": decision["action"],
                "reason": decision["reason"],
                "run_dir": str(paths.root),
                "plan_iterations_used": iteration,
                "implementation_iterations_used": 0,
                "unresolved_must_fix_count": plan_must_fix_count(review_data),
                "plan_cache_hit": plan_cache_hit,
            }
            write_final_summary(paths, summary)
            return summary

    if approved_plan_body is None or approved_plan_meta is None:
        summary = {
            "status": "stop_plan_max_iterations",
            "reason": f"Reached max plan iterations ({max_plan_iterations}).",
            "run_dir": str(paths.root),
            "plan_iterations_used": max_plan_iterations,
            "implementation_iterations_used": 0,
            "unresolved_must_fix_count": plan_must_fix_count(plan_reviews[-1]) if plan_reviews else 0,
            "plan_cache_hit": plan_cache_hit,
        }
        write_final_summary(paths, summary)
        return summary

    verification_history: list[dict[str, Any]] = []
    latest_verification: dict[str, Any] | None = None

    for iteration in range(1, max_implementation_iterations + 1):
        iteration_dir = paths.implementation_iteration_dir(iteration)
        iteration_dir.mkdir(parents=True, exist_ok=True)
        before_manifest = workspace_manifest(workspace)
        approved_plan_before = plan.read_text(encoding="utf-8")

        implement_prompt = implementation_prompt(
            plan_path=plan,
            approved_plan_body=approved_plan_body,
            latest_verification=latest_verification,
            iteration=iteration,
        )
        write_text(iteration_dir / "claude-prompt.txt", implement_prompt)
        claude_output, claude_error = run_claude_implementer(
            prompt=implement_prompt,
            workspace=workspace,
            model=claude_model,
            timeout_seconds=DEFAULT_CLAUDE_TIMEOUT_SECONDS,
            allowed_tools=allowed_tools,
        )
        write_text(iteration_dir / "claude-output.txt", claude_output)
        if claude_error:
            summary = {
                "status": "failed",
                "reason": claude_error,
                "run_dir": str(paths.root),
                "plan_iterations_used": approved_plan_meta["plan_iteration"],
                "implementation_iterations_used": iteration - 1,
                "unresolved_must_fix_count": unresolved_must_fix_count(latest_verification) if latest_verification else 0,
                "approved_plan_path": str(paths.approved_plan),
                "plan_cache_hit": plan_cache_hit,
            }
            write_final_summary(paths, summary)
            return summary

        plan_mutation_detected = plan.read_text(encoding="utf-8") != approved_plan_body
        if plan_mutation_detected:
            write_text(iteration_dir / "plan-mutation-before-restore.md", plan.read_text(encoding="utf-8"))
            plan.write_text(approved_plan_body, encoding="utf-8")
            write_text(iteration_dir / "plan-restored.md", approved_plan_body)
        elif approved_plan_before != approved_plan_body:
            plan.write_text(approved_plan_body, encoding="utf-8")

        after_manifest = workspace_manifest(workspace)
        changed = changed_files(before_manifest, after_manifest)
        write_json(iteration_dir / "changed-files.json", {"changed_files": changed})

        validation_commands = detect_validation_commands(workspace, config)
        write_json(iteration_dir / "validation-commands.json", {"commands": validation_commands})
        validation = run_validation_commands(workspace, validation_commands, DEFAULT_VALIDATION_TIMEOUT_SECONDS)
        write_json(iteration_dir / "validation.json", validation)

        diff_text = git_diff(workspace, changed)
        write_text(iteration_dir / "git-diff.patch", diff_text)
        snapshots_reason = snapshot_reason(workspace, changed, diff_text)
        snapshots = file_snapshots(workspace, changed) if snapshots_reason else []
        snapshot_context = snapshots_reason or "omitted because git diff covers the tracked changes"
        write_json(iteration_dir / "file-snapshots.json", {"files": snapshots})

        if not changed and validation["status"] == "failed":
            verification_prompt_text = ""
            verification_data = {
                "overall_status": "blocked",
                "ready_to_approve": False,
                "summary": "Claude made no file changes and validation still fails.",
                "unresolved": [
                    {
                        "id": "PLAN",
                        "severity": "must_fix",
                        "category": "missing_logic",
                        "reason": "The frozen approved plan has not been implemented in the workspace.",
                        "missing_acceptance_criteria": [
                            "Apply code changes required by the approved plan",
                            "Get validation to pass",
                        ],
                    }
                ],
                "regressions": [],
                "next_actions": [
                    "Implement the frozen approved plan itself before attempting another verification.",
                    "Make file edits that address the failing validation.",
                ],
            }
        else:
            verification_prompt_text = verification_prompt(
                plan_path=plan,
                approved_plan_body=approved_plan_body,
                latest_verification=latest_verification,
                validation=validation,
                changed=changed,
                diff_text=diff_text,
                snapshots=snapshots,
                snapshot_context=snapshot_context,
                iteration=iteration,
                plan_mutation_detected=plan_mutation_detected,
            )
            verification_data, verification_error = run_codex_structured(
                prompt=verification_prompt_text,
                schema_path=relative_schema_dir() / "verification.schema.json",
                cwd=workspace,
                model=codex_model,
                timeout_seconds=DEFAULT_CODEX_TIMEOUT_SECONDS,
                sandbox="read-only",
            )
            if verification_error or verification_data is None:
                summary = {
                    "status": "failed",
                    "reason": verification_error or "Codex verification failed.",
                    "run_dir": str(paths.root),
                    "plan_iterations_used": approved_plan_meta["plan_iteration"],
                    "implementation_iterations_used": iteration - 1,
                    "unresolved_must_fix_count": unresolved_must_fix_count(latest_verification) if latest_verification else 0,
                    "approved_plan_path": str(paths.approved_plan),
                    "plan_cache_hit": plan_cache_hit,
                }
                write_final_summary(paths, summary)
                return summary

        if verification_prompt_text:
            write_text(iteration_dir / "verification-prompt.txt", verification_prompt_text)

        verification_data["iteration"] = iteration
        verification_data["validation_status"] = validation["status"]
        if plan_mutation_detected:
            append_plan_mutation_regression(verification_data, plan)
        write_json(iteration_dir / "verification.json", verification_data)
        verification_history.append(verification_data)
        latest_verification = verification_data

        decision = implementation_controller_decision(
            verifications=verification_history,
            iteration=iteration,
            max_iterations=max_implementation_iterations,
        )
        write_json(iteration_dir / "controller-decision.json", decision)
        if decision["action"] != "continue":
            summary = {
                "status": decision["action"],
                "reason": decision["reason"],
                "run_dir": str(paths.root),
                "plan_iterations_used": approved_plan_meta["plan_iteration"],
                "implementation_iterations_used": iteration,
                "unresolved_must_fix_count": unresolved_must_fix_count(verification_data),
                "latest_summary": verification_data.get("summary"),
                "approved_plan_path": str(paths.approved_plan),
                "approved_plan_sha256": approved_plan_meta["approved_plan_sha256"],
                "plan_cache_hit": plan_cache_hit,
            }
            write_final_summary(paths, summary)
            return summary

    summary = {
        "status": "stop_max_iterations",
        "reason": f"Reached max implementation iterations ({max_implementation_iterations}).",
        "run_dir": str(paths.root),
        "plan_iterations_used": approved_plan_meta["plan_iteration"],
        "implementation_iterations_used": max_implementation_iterations,
        "unresolved_must_fix_count": unresolved_must_fix_count(latest_verification) if latest_verification else 0,
        "approved_plan_path": str(paths.approved_plan),
        "approved_plan_sha256": approved_plan_meta["approved_plan_sha256"],
        "plan_cache_hit": plan_cache_hit,
    }
    write_final_summary(paths, summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded Claude/Codex remediation loop")
    subparsers = parser.add_subparsers(dest="command", required=True)

    loop = subparsers.add_parser("loop", help="Run the two-phase plan approval -> implementation verification loop")
    loop.add_argument("--plan", required=True, help="Absolute or workspace-relative path to the plan file")
    loop.add_argument("--cwd", default=".", help="Workspace root")
    loop.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS, help="Default cap applied to both phases")
    loop.add_argument("--max-plan-iterations", type=int, default=None, help="Override plan refinement loop cap")
    loop.add_argument(
        "--max-implementation-iterations",
        type=int,
        default=None,
        help="Override implementation remediation loop cap",
    )

    detect = subparsers.add_parser("detect-validation", help="Print auto-detected validation commands")
    detect.add_argument("--cwd", default=".", help="Workspace root")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "detect-validation":
        workspace = Path(args.cwd).expanduser().resolve()
        config = load_workspace_config(workspace)
        print(json.dumps({"commands": detect_validation_commands(workspace, config)}, indent=2))
        return 0

    workspace = Path(args.cwd).expanduser().resolve()
    plan = Path(args.plan).expanduser()
    if not plan.is_absolute():
        plan = (workspace / plan).resolve()
    max_plan_iterations = args.max_plan_iterations or args.max_iterations
    max_implementation_iterations = args.max_implementation_iterations or args.max_iterations
    summary = run_loop(plan, workspace, max_plan_iterations, max_implementation_iterations)
    print(json.dumps(summary, indent=2))
    return 1 if summary["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
