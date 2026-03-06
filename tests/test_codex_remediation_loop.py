from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "tools" / "codex-remediation-loop" / "codex_remediation_loop.py"
SPEC = importlib.util.spec_from_file_location("codex_remediation_loop", MODULE_PATH)
loop = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = loop
SPEC.loader.exec_module(loop)


class CodexRemediationLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_state_root = loop.DEFAULT_STATE_ROOT
        loop.DEFAULT_STATE_ROOT = self.root / "state"

    def tearDown(self) -> None:
        loop.DEFAULT_STATE_ROOT = self.original_state_root
        self.temp_dir.cleanup()

    def write_executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def stub_env(self, mutate_plan: bool = False) -> dict[str, str]:
        bin_dir = self.root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        codex = """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]
schema = Path(args[args.index("--output-schema") + 1]).name
output = Path(args[args.index("-o") + 1])
cwd = Path(args[args.index("-C") + 1])
_prompt = sys.stdin.read()

if schema == "plan-edit.schema.json":
    plan_files = sorted(cwd.glob("*.md"))
    plan_path = plan_files[0]
    current = plan_path.read_text(encoding="utf-8")
    if "Approved plan" not in current:
        plan_path.write_text(current + "\\n\\n## Approved plan\\n- Add subtract() and keep tests green.\\n", encoding="utf-8")
    payload = {
        "overall_status": "approved",
        "summary": "Plan approved.",
        "changed_plan": True,
        "findings": [],
        "next_actions": []
    }
else:
    payload = {
        "overall_status": "resolved",
        "ready_to_approve": True,
        "summary": "Implementation matches the frozen plan.",
        "unresolved": [],
        "regressions": [],
        "next_actions": []
    }

output.write_text(json.dumps(payload), encoding="utf-8")
"""
        claude = f"""#!/usr/bin/env python3
import sys
from pathlib import Path

workspace = Path.cwd()
calc = workspace / "calc.py"
current = calc.read_text(encoding="utf-8")
if "def subtract" not in current:
    calc.write_text(current + "\\n\\ndef subtract(a, b):\\n    return a - b\\n", encoding="utf-8")
{'(workspace / "PLAN.md").write_text("mutated by claude\\n", encoding="utf-8")' if mutate_plan else ''}
sys.stdout.write("implemented\\n")
"""
        self.write_executable(bin_dir / "codex", codex)
        self.write_executable(bin_dir / "claude", claude)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["HOME"] = str(self.root / "home")
        return env

    def test_detect_validation_commands_for_python_workspace(self) -> None:
        (self.root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
        commands = loop.detect_validation_commands(self.root, {})
        self.assertEqual(commands, [{"kind": "test", "command": "python3 -m pytest -q"}])

    def test_detect_validation_commands_for_node_workspace(self) -> None:
        package_json = {
            "scripts": {
                "lint": "eslint .",
                "test": "vitest",
                "build": "vite build",
            }
        }
        (self.root / "package.json").write_text(json.dumps(package_json), encoding="utf-8")
        commands = loop.detect_validation_commands(self.root, {})
        self.assertEqual(
            commands,
            [
                {"kind": "lint", "command": "npm run lint"},
                {"kind": "test", "command": "npm run test"},
                {"kind": "build", "command": "npm run build"},
            ],
        )

    def test_changed_files_detects_content_change_and_new_file(self) -> None:
        before = {"a.txt": "1", "b.txt": "2"}
        after = {"a.txt": "9", "b.txt": "2", "c.txt": "3"}
        self.assertEqual(loop.changed_files(before, after), ["a.txt", "c.txt"])

    def test_plan_controller_approves_when_no_must_fix_findings_remain(self) -> None:
        review = {
            "overall_status": "approved",
            "summary": "ready",
            "changed_plan": True,
            "findings": [],
            "next_actions": [],
        }
        decision = loop.plan_controller_decision(reviews=[review], iteration=1, max_iterations=5)
        self.assertEqual(decision["action"], "approve_plan")

    def test_plan_controller_stops_on_repeated_same_must_fix_findings(self) -> None:
        review = {
            "overall_status": "continue",
            "summary": "still open",
            "changed_plan": True,
            "findings": [
                {
                    "id": "F1",
                    "severity": "must_fix",
                    "title": "missing rollback",
                    "details": "add rollback",
                    "acceptance_criteria": ["rollback section exists"],
                }
            ],
            "next_actions": ["fix rollback"],
        }
        decision = loop.plan_controller_decision(reviews=[review, review, review], iteration=3, max_iterations=5)
        self.assertEqual(decision["action"], "stop_plan_stagnating")

    def test_implementation_controller_stops_when_resolved(self) -> None:
        verification = {
            "ready_to_approve": True,
            "overall_status": "resolved",
            "summary": "done",
            "validation_status": "passed",
            "unresolved": [],
            "regressions": [],
            "next_actions": [],
        }
        decision = loop.implementation_controller_decision(verifications=[verification], iteration=1, max_iterations=5)
        self.assertEqual(decision["action"], "stop_resolved")

    def test_implementation_controller_stops_on_stagnation(self) -> None:
        verification = {
            "ready_to_approve": False,
            "overall_status": "continue",
            "summary": "still open",
            "validation_status": "passed",
            "unresolved": [
                {"id": "F1", "severity": "must_fix", "reason": "x", "missing_acceptance_criteria": ["a"]}
            ],
            "regressions": [],
            "next_actions": [],
        }
        decision = loop.implementation_controller_decision(
            verifications=[verification, verification, verification],
            iteration=3,
            max_iterations=5,
        )
        self.assertEqual(decision["action"], "stop_stagnating")

    def test_freeze_approved_plan_writes_snapshot_and_updates_workspace_plan(self) -> None:
        run_root = self.root / "state"
        paths = loop.run_paths(run_root)
        plan = self.root / "PLAN.md"
        plan.write_text("draft", encoding="utf-8")
        metadata = loop.freeze_approved_plan(paths, plan, "approved", plan_iteration=2, codex_model="gpt-5.4")
        self.assertEqual(plan.read_text(encoding="utf-8"), "approved")
        self.assertEqual(paths.approved_plan.read_text(encoding="utf-8"), "approved")
        self.assertEqual(metadata["plan_iteration"], 2)
        self.assertTrue(paths.approved_plan_meta.exists())

    def test_append_plan_mutation_regression_adds_must_fix(self) -> None:
        verification = {
            "summary": "existing",
            "regressions": [],
            "next_actions": [],
        }
        loop.append_plan_mutation_regression(verification, Path("/tmp/PLAN.md"))
        self.assertEqual(verification["regressions"][0]["id"], "R_PLAN_MUTATION")
        self.assertEqual(verification["regressions"][0]["category"], "structural_mismatch")
        self.assertIn("restored", verification["summary"])
        self.assertIn("Do not edit the frozen approved plan", verification["next_actions"][0])

    def test_compact_review_and_verification_preserve_categories(self) -> None:
        review = {
            "iteration": 1,
            "overall_status": "continue",
            "summary": "needs work",
            "findings": [
                {
                    "id": "F1",
                    "severity": "must_fix",
                    "category": "missing_logic",
                    "title": "missing rollback",
                    "details": "add rollback",
                    "acceptance_criteria": ["rollback exists"],
                }
            ],
            "next_actions": ["fix rollback"],
        }
        verification = {
            "iteration": 2,
            "overall_status": "continue",
            "ready_to_approve": False,
            "validation_status": "failed",
            "summary": "still open",
            "unresolved": [
                {
                    "id": "F1",
                    "severity": "must_fix",
                    "category": "wrong_location",
                    "reason": "changed wrong file",
                    "missing_acceptance_criteria": ["edit correct file"],
                }
            ],
            "regressions": [
                {
                    "id": "R1",
                    "severity": "should_fix",
                    "category": "behavioral_divergence",
                    "title": "behavior changed",
                    "reason": "edge case broken",
                }
            ],
            "next_actions": ["move fix"],
        }
        compact_review = loop.compact_plan_review(review)
        compact_verification = loop.compact_verification(verification)
        self.assertEqual(compact_review["findings"][0]["category"], "missing_logic")
        self.assertEqual(compact_verification["unresolved"][0]["category"], "wrong_location")
        self.assertEqual(compact_verification["regressions"][0]["category"], "behavioral_divergence")

    def test_write_and_load_plan_cache_round_trip(self) -> None:
        source_plan = "# Plan\n\nShip feature.\n"
        approved_plan = "# Plan\n\n## Approved plan\n- Ship feature safely.\n"
        loop.write_plan_cache(source_plan_body=source_plan, approved_plan_body=approved_plan, codex_model="gpt-5.4")
        loaded = loop.load_plan_cache(plan_body=source_plan, codex_model="gpt-5.4")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["approved_plan_body"], approved_plan)
        self.assertEqual(loaded["approved_plan_sha256"], loop.sha256_text(approved_plan))

    def test_run_validation_command_captures_timeout(self) -> None:
        with mock.patch.object(loop.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="slow", timeout=3)):
            result = loop.run_validation_command(self.root, {"kind": "test", "command": "slow"}, timeout_seconds=3)
        self.assertFalse(result["passed"])
        self.assertEqual(result["exit_code"], None)
        self.assertIn("Timed out", result["error"])

    def test_snapshot_reason_omits_snapshots_when_git_diff_is_available(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init"], cwd=str(workspace), check=True, capture_output=True, text=True)
        tracked = workspace / "tracked.txt"
        tracked.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=str(workspace), check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
            cwd=str(workspace),
            check=True,
            capture_output=True,
            text=True,
        )
        tracked.write_text("after\n", encoding="utf-8")
        diff_text = loop.git_diff(workspace, ["tracked.txt"])
        self.assertIsNone(loop.snapshot_reason(workspace, ["tracked.txt"], diff_text))

    def test_snapshot_reason_requests_snapshots_for_untracked_changes(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init"], cwd=str(workspace), check=True, capture_output=True, text=True)
        untracked = workspace / "new.txt"
        untracked.write_text("hello\n", encoding="utf-8")
        reason = loop.snapshot_reason(workspace, ["new.txt"], "")
        self.assertIn("untracked paths", reason or "")

    def test_cli_resolves_end_to_end_with_stubbed_clis(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (workspace / "test_calc.py").write_text(
            "from calc import add, subtract\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n\n"
            "def test_subtract():\n    assert subtract(5, 3) == 2\n",
            encoding="utf-8",
        )
        plan = workspace / "PLAN.md"
        plan.write_text("# Plan\n\nAdd subtract support.\n", encoding="utf-8")

        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "loop", "--plan", str(plan), "--cwd", str(workspace), "--max-iterations", "5"],
            text=True,
            capture_output=True,
            check=False,
            env=self.stub_env(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["status"], "stop_resolved")
        self.assertEqual(summary["plan_iterations_used"], 1)
        self.assertEqual(summary["implementation_iterations_used"], 1)
        self.assertEqual((workspace / "PLAN.md").read_text(encoding="utf-8"), Path(summary["approved_plan_path"]).read_text(encoding="utf-8"))
        self.assertIn("def subtract", (workspace / "calc.py").read_text(encoding="utf-8"))

    def test_cli_reuses_approved_plan_cache_when_plan_is_unchanged(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (workspace / "test_calc.py").write_text(
            "from calc import add, subtract\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n\n"
            "def test_subtract():\n    assert subtract(5, 3) == 2\n",
            encoding="utf-8",
        )
        plan = workspace / "PLAN.md"
        plan.write_text("# Plan\n\nAdd subtract support.\n", encoding="utf-8")
        env = self.stub_env()

        first = subprocess.run(
            [sys.executable, str(MODULE_PATH), "loop", "--plan", str(plan), "--cwd", str(workspace), "--max-iterations", "5"],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(first.returncode, 0, first.stderr or first.stdout)

        (workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

        second = subprocess.run(
            [sys.executable, str(MODULE_PATH), "loop", "--plan", str(plan), "--cwd", str(workspace), "--max-iterations", "5"],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
        summary = json.loads(second.stdout)
        self.assertEqual(summary["plan_iterations_used"], 0)
        self.assertTrue(summary["plan_cache_hit"])
        self.assertEqual(summary["implementation_iterations_used"], 1)

    def test_cli_returns_zero_for_explicit_stop_states_and_restores_plan(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (workspace / "test_calc.py").write_text(
            "from calc import add, subtract\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n\n"
            "def test_subtract():\n    assert subtract(5, 3) == 2\n",
            encoding="utf-8",
        )
        plan = workspace / "PLAN.md"
        plan.write_text("# Plan\n\nAdd subtract support.\n", encoding="utf-8")

        completed = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "loop",
                "--plan",
                str(plan),
                "--cwd",
                str(workspace),
                "--max-plan-iterations",
                "1",
                "--max-implementation-iterations",
                "1",
            ],
            text=True,
            capture_output=True,
            check=False,
            env=self.stub_env(mutate_plan=True),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["status"], "stop_max_iterations")
        self.assertEqual(summary["unresolved_must_fix_count"], 1)
        self.assertIn("Approved plan", (workspace / "PLAN.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
