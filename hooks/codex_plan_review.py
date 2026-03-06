#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

PROMPT_VERSION = "1"
DEFAULT_TIMEOUT_SECONDS = 90
DEFAULT_MAX_CHARS = 20_000
DEFAULT_FAST_MODEL = "gpt-5.4"
DEFAULT_DEEP_MODEL = "gpt-5.4"
DEFAULT_PLAN_MATCHERS = {
    "plan.md",
    "implementation_plan.md",
    "implementation-plan.md",
    "execution_plan.md",
    "execution-plan.md",
    "workplan.md",
    "work-plan.md",
    "architecture_plan.md",
    "architecture-plan.md",
}
HIGH_RISK_TOKENS = {
    "auth",
    "billing",
    "credential",
    "customer-data",
    "database",
    "delete",
    "deployment",
    "encryption",
    "migration",
    "payment",
    "permission",
    "production",
    "rollback",
    "schema",
    "security",
    "token",
}
PATH_KEYS = {"file_path", "path", "target_file", "new_file_path"}


def hook_response(additional_context: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"continue": True}
    if additional_context:
        response["hookSpecificOutput"] = {"additionalContext": additional_context}
    return response


def load_hook_payload(stdin_text: str) -> dict[str, Any]:
    if not stdin_text.strip():
        return {}
    try:
        loaded = json.loads(stdin_text)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def extract_candidate_paths(value: Any, *, current_key: str | None = None) -> list[Path]:
    paths: list[Path] = []
    if isinstance(value, dict):
        for key, child in value.items():
            paths.extend(extract_candidate_paths(child, current_key=key))
        return paths
    if isinstance(value, list):
        for child in value:
            paths.extend(extract_candidate_paths(child, current_key=current_key))
        return paths
    if current_key in PATH_KEYS and isinstance(value, str):
        paths.append(Path(value).expanduser())
    return paths


def configured_matchers(env: dict[str, str] | os._Environ[str]) -> set[str]:
    raw = env.get("CLAUDE_CODEX_PLAN_REVIEW_MATCHERS", "")
    configured = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return configured or DEFAULT_PLAN_MATCHERS


def is_plan_file(path: Path, env: dict[str, str] | os._Environ[str]) -> bool:
    return path.name.lower() in configured_matchers(env)


def is_high_risk(plan_text: str) -> bool:
    lowered = plan_text.lower()
    return len(plan_text) > 3_000 or any(token in lowered for token in HIGH_RISK_TOKENS)


def selected_model(plan_text: str, env: dict[str, str] | os._Environ[str]) -> str | None:
    fast_model = env.get("CLAUDE_CODEX_PLAN_REVIEW_FAST_MODEL", "").strip() or DEFAULT_FAST_MODEL
    deep_model = env.get("CLAUDE_CODEX_PLAN_REVIEW_DEEP_MODEL", "").strip() or DEFAULT_DEEP_MODEL
    if deep_model and is_high_risk(plan_text):
        return deep_model
    return fast_model or deep_model or None


def timeout_seconds(env: dict[str, str] | os._Environ[str]) -> int:
    raw = env.get("CLAUDE_CODEX_PLAN_REVIEW_TIMEOUT_SECONDS", "")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return parsed if parsed > 0 else DEFAULT_TIMEOUT_SECONDS


def max_chars(env: dict[str, str] | os._Environ[str]) -> int:
    raw = env.get("CLAUDE_CODEX_PLAN_REVIEW_MAX_CHARS", "")
    if not raw:
        return DEFAULT_MAX_CHARS
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_MAX_CHARS
    return parsed if parsed > 0 else DEFAULT_MAX_CHARS


def cache_path(env: dict[str, str] | os._Environ[str]) -> Path:
    raw = env.get("CLAUDE_CODEX_PLAN_REVIEW_CACHE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cache" / "claude-codex-plan-review" / "reviews.json"


def load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "entries": {}}
    if not isinstance(loaded, dict):
        return {"version": 1, "entries": {}}
    entries = loaded.get("entries")
    if not isinstance(entries, dict):
        return {"version": 1, "entries": {}}
    return {"version": 1, "entries": entries}


def save_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        json.dump(cache, handle, indent=2, sort_keys=True)
        handle.write("\n")
        tmp_path = Path(handle.name)
    tmp_path.replace(path)


def cache_key(plan_path: Path, plan_text: str, model: str | None) -> str:
    digest = hashlib.sha256()
    digest.update(PROMPT_VERSION.encode("utf-8"))
    digest.update(str(plan_path).encode("utf-8"))
    digest.update((model or "<default-model>").encode("utf-8"))
    digest.update(plan_text.encode("utf-8"))
    return digest.hexdigest()


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def trim_review(text: str, limit: int = 2_400) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 15].rstrip() + "\n[truncated]"


def build_prompt(plan_path: Path, plan_text: str, truncated: bool) -> str:
    truncation_note = (
        "The plan content was truncated for prompt size. Review the available content only.\n\n"
        if truncated
        else ""
    )
    return (
        "Review the implementation plan below as a strict staff-level engineer.\n"
        "Return concise markdown with exactly these sections:\n"
        "Verdict: APPROVE | CONCERNS | BLOCK\n"
        "Findings:\n"
        "- concrete issue or `None`\n"
        "Questions:\n"
        "- unresolved question or `None`\n"
        "Missing Validation:\n"
        "- missing tests, rollback, observability, or `None`\n"
        "Summary:\n"
        "- one sentence\n\n"
        "Focus on hidden assumptions, security, migrations, rollback, observability, and testing.\n"
        "Do not praise the plan. Do not restate it. Stay under 220 words.\n\n"
        f"Plan file: {plan_path}\n\n"
        f"{truncation_note}"
        "Plan content:\n"
        f"{plan_text}"
    )


def workspace_for(payload: dict[str, Any], plan_path: Path) -> Path:
    raw_cwd = payload.get("cwd")
    if isinstance(raw_cwd, str) and raw_cwd.strip():
        cwd_path = Path(raw_cwd).expanduser()
        if cwd_path.exists():
            return cwd_path
    return plan_path.parent


def read_plan(plan_path: Path) -> str:
    return plan_path.read_text(encoding="utf-8")


def run_codex_review(
    plan_path: Path,
    plan_text: str,
    workspace: Path,
    env: dict[str, str] | os._Environ[str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str | None, str | None]:
    model = selected_model(plan_text, env)
    prompt_text, truncated = truncate_text(plan_text, max_chars(env))
    prompt = build_prompt(plan_path, prompt_text, truncated)
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
        str(workspace),
        "-o",
        str(output_path),
    ]
    if model:
        cmd.extend(["-m", model])
    cmd.append("-")

    try:
        completed = runner(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds(env),
            check=False,
        )
    except FileNotFoundError:
        output_path.unlink(missing_ok=True)
        return None, "Codex CLI is not installed or not on PATH. No second opinion was produced."
    except subprocess.TimeoutExpired:
        output_path.unlink(missing_ok=True)
        return None, "Codex review timed out. No second opinion was produced."

    review_text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
    output_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        return None, f"Codex review failed: {stderr}. No second opinion was produced."
    if not review_text:
        return None, "Codex returned no review text. No second opinion was produced."
    return trim_review(review_text), None


def review_plan(
    plan_path: Path,
    payload: dict[str, Any],
    env: dict[str, str] | os._Environ[str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    *,
    use_cache: bool = True,
) -> str:
    plan_text = read_plan(plan_path)
    model = selected_model(plan_text, env)
    review_cache_path = cache_path(env)
    key = cache_key(plan_path, plan_text, model)
    if use_cache and env.get("CLAUDE_CODEX_PLAN_REVIEW_DISABLE_CACHE") != "1":
        cache = load_cache(review_cache_path)
        cached_entry = cache["entries"].get(key)
        if isinstance(cached_entry, dict):
            cached_review = cached_entry.get("review")
            if isinstance(cached_review, str) and cached_review.strip():
                return f"Codex second opinion for {plan_path.name} [cached]:\n\n{cached_review.strip()}"
    else:
        cache = {"version": 1, "entries": {}}

    review_text, failure = run_codex_review(plan_path, plan_text, workspace_for(payload, plan_path), env, runner)
    if failure:
        return f"Codex second opinion unavailable for {plan_path.name}: {failure}"

    cache["entries"][key] = {
        "path": str(plan_path),
        "review": review_text,
        "model": model or "<default>",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if use_cache and env.get("CLAUDE_CODEX_PLAN_REVIEW_DISABLE_CACHE") != "1":
        save_cache(review_cache_path, cache)
    return f"Codex second opinion for {plan_path.name}:\n\n{review_text}"


def first_matching_plan_path(payload: dict[str, Any], env: dict[str, str] | os._Environ[str]) -> Path | None:
    tool_input = payload.get("tool_input", {})
    for candidate in extract_candidate_paths(tool_input):
        if candidate.exists() and is_plan_file(candidate, env):
            return candidate
    return None


def process_hook_payload(
    payload: dict[str, Any],
    env: dict[str, str] | os._Environ[str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    plan_path = first_matching_plan_path(payload, env)
    if plan_path is None:
        return hook_response()
    additional_context = review_plan(plan_path, payload, env, runner)
    return hook_response(additional_context)


def manual_mode(plan_file: str, cwd: str | None, no_cache: bool) -> int:
    plan_path = Path(plan_file).expanduser().resolve()
    if not plan_path.exists():
        print(f"Plan file not found: {plan_path}", file=sys.stderr)
        return 1
    payload: dict[str, Any] = {"cwd": cwd or str(plan_path.parent)}
    review = review_plan(plan_path, payload, os.environ, use_cache=not no_cache)
    print(review)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safe Codex-backed Claude plan review hook")
    parser.add_argument("--manual", metavar="PLAN_FILE", help="Run a manual review for one plan file")
    parser.add_argument("--cwd", help="Workspace root for manual mode")
    parser.add_argument("--no-cache", action="store_true", help="Disable cache in manual mode")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.manual:
        return manual_mode(args.manual, args.cwd, args.no_cache)
    payload = load_hook_payload(sys.stdin.read())
    print(json.dumps(process_hook_payload(payload, os.environ)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
