from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
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

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

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
        self.assertIn("restored", verification["summary"])
        self.assertIn("Do not edit the frozen approved plan", verification["next_actions"][0])


if __name__ == "__main__":
    unittest.main()
