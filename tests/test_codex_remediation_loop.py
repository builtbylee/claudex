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

    def test_controller_decision_stops_when_resolved(self) -> None:
        verification = {
            "ready_to_approve": True,
            "overall_status": "resolved",
            "summary": "done",
            "validation_status": "passed",
            "unresolved": [],
            "regressions": [],
        }
        decision = loop.controller_decision(verifications=[verification], iteration=1, max_iterations=5)
        self.assertEqual(decision["action"], "stop_resolved")

    def test_controller_decision_stops_on_stagnation(self) -> None:
        v1 = {
            "ready_to_approve": False,
            "overall_status": "continue",
            "summary": "still open",
            "validation_status": "passed",
            "unresolved": [{"id": "F1", "severity": "must_fix", "reason": "x", "missing_acceptance_criteria": ["a"]}],
            "regressions": [],
        }
        v2 = {
            "ready_to_approve": False,
            "overall_status": "continue",
            "summary": "still open",
            "validation_status": "passed",
            "unresolved": [{"id": "F1", "severity": "must_fix", "reason": "x", "missing_acceptance_criteria": ["a"]}],
            "regressions": [],
        }
        v3 = {
            "ready_to_approve": False,
            "overall_status": "continue",
            "summary": "still open",
            "validation_status": "passed",
            "unresolved": [{"id": "F1", "severity": "must_fix", "reason": "x", "missing_acceptance_criteria": ["a"]}],
            "regressions": [],
        }
        decision = loop.controller_decision(verifications=[v1, v2, v3], iteration=3, max_iterations=5)
        self.assertEqual(decision["action"], "stop_stagnating")

    def test_controller_decision_stops_on_max_iterations(self) -> None:
        verification = {
            "ready_to_approve": False,
            "overall_status": "continue",
            "summary": "still open",
            "validation_status": "passed",
            "unresolved": [{"id": "F1", "severity": "must_fix", "reason": "x", "missing_acceptance_criteria": ["a"]}],
            "regressions": [],
        }
        decision = loop.controller_decision(verifications=[verification], iteration=5, max_iterations=5)
        self.assertEqual(decision["action"], "stop_max_iterations")


if __name__ == "__main__":
    unittest.main()
