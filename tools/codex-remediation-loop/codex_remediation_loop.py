#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_MAX_ITERATIONS = 5
DEFAULT_CODEX_MODEL = "gpt-5.4"
DEFAULT_CLAUDE_MODEL = "opus"
DEFAULT_CODEX_TIMEOUT_SECONDS = 180
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 600
DEFAULT_VALIDATION_TIMEOUT_SECONDS = 300
DEFAULT_MAX_PROMPT_CHARS = 30_000
DEFAULT_STATE_ROOT = Path.home() / ".claude" / "state" / "codex-remediation-loop"
DEFAULT_ALLOWED_TOOLS = "Read,Write,Edit,MultiEdit,Glob,Grep"
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
    review: Path
    final_summary_json: Path
    final_summary_md: Path

    def iteration_dir(self, iteration: int) -> Path:
        return self.root / f"iteration-{iteration:02d}"


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
) -> tuple[dict[str, Any] | None, str | None]:
    with tempfile.NamedTemporaryFile("r", delete=False) as handle:
        output_path = Path(handle.name)
    cmd = [
        "codex",
        "exec",
        "-s",
        "read-only",
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


def run_validation_commands(workspace: Path, commands: list[dict[str, str]], timeout_seconds: int) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    if not commands:
        return {"status": "skipped", "commands": results}
    overall_passed = True
    for entry in commands:
        completed = subprocess.run(
            entry["command"],
            cwd=str(workspace),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        passed = completed.returncode == 0
        overall_passed = overall_passed and passed
        results.append(
            {
                "kind": entry["kind"],
                "command": entry["command"],
                "passed": passed,
                "exit_code": completed.returncode,
                "stdout_tail": completed.stdout[-4_000:],
                "stderr_tail": completed.stderr[-4_000:],
            }
        )
    return {"status": "passed" if overall_passed else "failed", "commands": results}


def initial_review_prompt(plan_path: Path, plan_body: str, truncated: bool) -> str:
    truncation = "The plan body was truncated for prompt size.\n\n" if truncated else ""
    return (
        "Review the implementation plan below. Return only structured output that matches the schema.\n"
        "Assign stable finding IDs in the form F1, F2, F3.\n"
        "Every finding must include severity and concrete acceptance criteria.\n"
        "Use severities: must_fix, should_fix, advisory.\n"
        "Verdict meanings:\n"
        "- APPROVE: no material issues\n"
        "- CONCERNS: fixable but incomplete or risky\n"
        "- BLOCK: unsafe to approve as written\n\n"
        f"Plan file: {plan_path}\n\n"
        f"{truncation}"
        "Plan content:\n"
        f"{plan_body}"
    )


def implementation_prompt(
    *,
    plan_path: Path,
    plan_body: str,
    review: dict[str, Any],
    latest_verification: dict[str, Any] | None,
    iteration: int,
) -> str:
    target_findings = review.get("findings", []) if latest_verification is None else latest_verification.get("unresolved", [])
    regressions = [] if latest_verification is None else latest_verification.get("regressions", [])
    guidance = [] if latest_verification is None else latest_verification.get("next_actions", [])
    findings_note = (
        "Codex reported no open findings on the plan itself. That does NOT mean the work is done. "
        "You still need to implement the plan in the workspace and make the validation pass.\n"
        if not target_findings and latest_verification is None
        else ""
    )
    return (
        "You are executing a bounded remediation pass against Codex findings.\n"
        "Your job is to implement the plan in the workspace and also satisfy any unresolved Codex findings.\n"
        "Codex findings are additional constraints. The plan itself remains the primary implementation task.\n"
        "Work in one pass across all must_fix findings before touching should_fix items.\n"
        "Read relevant files before editing. Prefer the smallest change that satisfies acceptance criteria.\n"
        "Do not run tests, linters, or shell commands. The controller will do that.\n"
        "Do not write status reports, plans, or extra documentation unless required by the findings.\n"
        "If a finding is contradictory or impossible to satisfy from repo context, stop and explain that clearly.\n"
        "When you are done, respond with a short plain-text summary only.\n\n"
        f"Iteration: {iteration}\n"
        f"Plan file: {plan_path}\n"
        f"Original verdict: {review.get('verdict')}\n\n"
        f"{findings_note}\n"
        "Original plan content:\n"
        f"{plan_body}\n\n"
        "Target findings to resolve now:\n"
        f"{json.dumps(target_findings, indent=2)}\n\n"
        "Current regressions:\n"
        f"{json.dumps(regressions, indent=2)}\n\n"
        "Codex next actions:\n"
        f"{json.dumps(guidance, indent=2)}"
    )


def verification_prompt(
    *,
    plan_path: Path,
    plan_body: str,
    review: dict[str, Any],
    latest_verification: dict[str, Any] | None,
    validation: dict[str, Any],
    changed: list[str],
    diff_text: str,
    snapshots: list[dict[str, str]],
    iteration: int,
) -> str:
    return (
        "Verify whether the implementation work resolves the original findings.\n"
        "Return only structured output matching the schema.\n"
        "Use the original finding IDs exactly. Do not invent new IDs for original findings.\n"
        "You may add regression IDs like R1 for new regressions.\n"
        "Be strict: unresolved means unresolved. insufficient_evidence means the claim cannot be verified from the provided data.\n\n"
        f"Iteration: {iteration}\n"
        f"Plan file: {plan_path}\n\n"
        "Original plan content:\n"
        f"{plan_body}\n\n"
        "Original review:\n"
        f"{json.dumps(review, indent=2)}\n\n"
        "Previous verification:\n"
        f"{json.dumps(latest_verification, indent=2) if latest_verification else 'null'}\n\n"
        "Validation results:\n"
        f"{json.dumps(validation, indent=2)}\n\n"
        "Changed files:\n"
        f"{json.dumps(changed, indent=2)}\n\n"
        "Git diff:\n"
        f"{diff_text or '[no git diff available]'}\n\n"
        "Current file snapshots:\n"
        f"{json.dumps(snapshots, indent=2)}"
    )


def unresolved_must_fix_count(verification: dict[str, Any]) -> int:
    unresolved = verification.get("unresolved", [])
    regressions = verification.get("regressions", [])
    count = sum(1 for item in unresolved if item.get("severity") == "must_fix")
    count += sum(1 for item in regressions if item.get("severity") == "must_fix")
    return count


def controller_decision(
    *,
    verifications: list[dict[str, Any]],
    iteration: int,
    max_iterations: int,
) -> dict[str, Any]:
    latest = verifications[-1]
    current_count = unresolved_must_fix_count(latest)
    previous_count = unresolved_must_fix_count(verifications[-2]) if len(verifications) > 1 else None
    stagnating = previous_count is not None and current_count >= previous_count
    stagnation_rounds = 1
    if stagnating:
        for idx in range(len(verifications) - 1, 0, -1):
            newer = unresolved_must_fix_count(verifications[idx])
            older = unresolved_must_fix_count(verifications[idx - 1])
            if newer >= older:
                stagnation_rounds += 1
            else:
                break
    else:
        stagnation_rounds = 0

    validation_status = latest.get("validation_status")
    has_regression = bool(latest.get("regressions"))
    ready_to_approve = bool(latest.get("ready_to_approve"))

    if current_count == 0 and not has_regression and ready_to_approve and validation_status in {"passed", "skipped"}:
        return {"action": "stop_resolved", "reason": "All must-fix findings resolved and no regressions remain."}
    if iteration >= max_iterations:
        return {"action": "stop_max_iterations", "reason": f"Reached max iterations ({max_iterations})."}
    if latest.get("overall_status") == "blocked":
        return {"action": "stop_blocked", "reason": latest.get("summary", "Verification blocked progress.")}
    if stagnation_rounds >= 2:
        return {"action": "stop_stagnating", "reason": "Must-fix count did not decrease for two consecutive rounds."}
    return {"action": "continue", "reason": "Unresolved must-fix findings remain and progress is still possible."}


def run_paths(root: Path) -> RunPaths:
    return RunPaths(
        root=root,
        meta=root / "run.json",
        review=root / "review.json",
        final_summary_json=root / "final-summary.json",
        final_summary_md=root / "final-summary.md",
    )


def init_run(plan: Path, workspace: Path, *, max_iterations: int) -> RunPaths:
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
            "max_iterations": max_iterations,
        },
    )
    return paths


def latest_verification(paths: RunPaths) -> dict[str, Any] | None:
    verifications = sorted(paths.root.glob("iteration-*/verification.json"))
    if not verifications:
        return None
    return read_json(verifications[-1])


def run_loop(plan: Path, workspace: Path, max_iterations: int) -> dict[str, Any]:
    config = load_workspace_config(workspace)
    paths = init_run(plan, workspace, max_iterations=max_iterations)
    codex_model = str(config.get("codex_model") or DEFAULT_CODEX_MODEL)
    claude_model = str(config.get("claude_model") or DEFAULT_CLAUDE_MODEL)
    allowed_tools = str(config.get("claude_allowed_tools") or DEFAULT_ALLOWED_TOOLS)
    plan_body, truncated = plan_text(plan)

    review_prompt = initial_review_prompt(plan, plan_body, truncated)
    write_text(paths.root / "review-prompt.txt", review_prompt)
    review_data, review_error = run_codex_structured(
        prompt=review_prompt,
        schema_path=relative_schema_dir() / "review.schema.json",
        cwd=workspace,
        model=codex_model,
        timeout_seconds=DEFAULT_CODEX_TIMEOUT_SECONDS,
    )
    if review_error or review_data is None:
        summary = {
            "status": "failed",
            "reason": review_error or "Codex review failed.",
            "run_dir": str(paths.root),
        }
        write_json(paths.final_summary_json, summary)
        write_text(paths.final_summary_md, f"# Codex Remediation Loop\n\nStatus: failed\n\nReason: {summary['reason']}\n")
        return summary
    write_json(paths.review, review_data)

    verifications: list[dict[str, Any]] = []
    latest = None
    for iteration in range(1, max_iterations + 1):
        iteration_dir = paths.iteration_dir(iteration)
        iteration_dir.mkdir(parents=True, exist_ok=True)
        before_manifest = workspace_manifest(workspace)
        implement_prompt = implementation_prompt(
            plan_path=plan,
            plan_body=plan_body,
            review=review_data,
            latest_verification=latest,
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
                "iteration": iteration,
            }
            write_json(paths.final_summary_json, summary)
            write_text(paths.final_summary_md, f"# Codex Remediation Loop\n\nStatus: failed\n\nReason: {summary['reason']}\n")
            return summary

        after_manifest = workspace_manifest(workspace)
        changed = changed_files(before_manifest, after_manifest)
        write_json(iteration_dir / "changed-files.json", {"changed_files": changed})

        validation_commands = detect_validation_commands(workspace, config)
        write_json(iteration_dir / "validation-commands.json", {"commands": validation_commands})
        validation = run_validation_commands(workspace, validation_commands, DEFAULT_VALIDATION_TIMEOUT_SECONDS)
        write_json(iteration_dir / "validation.json", validation)

        diff_text = git_diff(workspace, changed)
        write_text(iteration_dir / "git-diff.patch", diff_text)
        snapshots = file_snapshots(workspace, changed)
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
                        "reason": "The plan has not been implemented in the workspace.",
                        "missing_acceptance_criteria": [
                            "Apply code changes required by the plan",
                            "Get validation to pass"
                        ]
                    }
                ],
                "regressions": [],
                "next_actions": [
                    "Implement the plan itself before attempting another verification.",
                    "Make file edits that address the failing validation."
                ]
            }
        else:
            verification_prompt_text = verification_prompt(
                plan_path=plan,
                plan_body=plan_body,
                review=review_data,
                latest_verification=latest,
                validation=validation,
                changed=changed,
                diff_text=diff_text,
                snapshots=snapshots,
                iteration=iteration,
            )
            verification_data, verification_error = run_codex_structured(
                prompt=verification_prompt_text,
                schema_path=relative_schema_dir() / "verification.schema.json",
                cwd=workspace,
                model=codex_model,
                timeout_seconds=DEFAULT_CODEX_TIMEOUT_SECONDS,
            )
            if verification_error or verification_data is None:
                summary = {
                    "status": "failed",
                    "reason": verification_error or "Codex verification failed.",
                    "run_dir": str(paths.root),
                    "iteration": iteration,
                }
                write_json(paths.final_summary_json, summary)
                write_text(paths.final_summary_md, f"# Codex Remediation Loop\n\nStatus: failed\n\nReason: {summary['reason']}\n")
                return summary

        if verification_prompt_text:
            write_text(iteration_dir / "verification-prompt.txt", verification_prompt_text)

        verification_data["iteration"] = iteration
        verification_data["validation_status"] = validation["status"]
        write_json(iteration_dir / "verification.json", verification_data)
        verifications.append(verification_data)
        latest = verification_data

        decision = controller_decision(verifications=verifications, iteration=iteration, max_iterations=max_iterations)
        write_json(iteration_dir / "controller-decision.json", decision)
        if decision["action"] != "continue":
            summary = {
                "status": decision["action"],
                "reason": decision["reason"],
                "run_dir": str(paths.root),
                "iterations_used": iteration,
                "review_verdict": review_data.get("verdict"),
                "unresolved_must_fix_count": unresolved_must_fix_count(verification_data),
                "latest_summary": verification_data.get("summary"),
            }
            write_json(paths.final_summary_json, summary)
            write_text(
                paths.final_summary_md,
                (
                    "# Codex Remediation Loop\n\n"
                    f"Status: {summary['status']}\n\n"
                    f"Reason: {summary['reason']}\n\n"
                    f"Run dir: {summary['run_dir']}\n\n"
                    f"Iterations used: {summary['iterations_used']}\n\n"
                    f"Unresolved must-fix: {summary['unresolved_must_fix_count']}\n\n"
                    f"Latest verification summary: {summary['latest_summary']}\n"
                ),
            )
            return summary

    summary = {
        "status": "stop_max_iterations",
        "reason": f"Reached max iterations ({max_iterations}).",
        "run_dir": str(paths.root),
        "iterations_used": max_iterations,
    }
    write_json(paths.final_summary_json, summary)
    write_text(paths.final_summary_md, f"# Codex Remediation Loop\n\nStatus: stop_max_iterations\n\nRun dir: {summary['run_dir']}\n")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded Claude/Codex remediation loop")
    subparsers = parser.add_subparsers(dest="command", required=True)

    loop = subparsers.add_parser("loop", help="Run the full review -> implement -> verify loop")
    loop.add_argument("--plan", required=True, help="Absolute or workspace-relative path to the plan file")
    loop.add_argument("--cwd", default=".", help="Workspace root")
    loop.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)

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
    summary = run_loop(plan, workspace, args.max_iterations)
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] in {"stop_resolved", "continue"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
