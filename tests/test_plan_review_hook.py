from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "hooks" / "codex_plan_review.py"
SPEC = importlib.util.spec_from_file_location("plan_review", MODULE_PATH)
plan_review = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(plan_review)


class PlanReviewHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cache_file = self.root / "cache.json"
        self.env = {
            "CLAUDE_CODEX_PLAN_REVIEW_CACHE": str(self.cache_file),
            "CLAUDE_CODEX_PLAN_REVIEW_FAST_MODEL": "fast-model",
            "CLAUDE_CODEX_PLAN_REVIEW_DEEP_MODEL": "deep-model",
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_plan(self, name: str = "PLAN.md", content: str = "simple plan") -> Path:
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def fake_runner(self, expected_output: str = "Verdict: CONCERNS") -> mock.Mock:
        def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            output_index = cmd.index("-o") + 1
            output_path = Path(cmd[output_index])
            output_path.write_text(expected_output, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        return mock.Mock(side_effect=_run)

    def test_extract_candidate_paths_handles_nested_payloads(self) -> None:
        payload = {
            "file_path": str(self.root / "PLAN.md"),
            "edits": [{"file_path": str(self.root / "OTHER.md")}],
        }
        paths = plan_review.extract_candidate_paths(payload)
        self.assertEqual({path.name for path in paths}, {"PLAN.md", "OTHER.md"})

    def test_is_plan_file_uses_defaults(self) -> None:
        self.assertTrue(plan_review.is_plan_file(Path("/tmp/PLAN.md"), self.env))
        self.assertFalse(plan_review.is_plan_file(Path("/tmp/notes.md"), self.env))

    def test_process_hook_payload_skips_non_plan_files(self) -> None:
        notes = self.root / "notes.md"
        notes.write_text("hello", encoding="utf-8")
        payload = {"tool_input": {"file_path": str(notes)}, "cwd": str(self.root)}
        response = plan_review.process_hook_payload(payload, self.env)
        self.assertEqual(response, {"continue": True})

    def test_process_hook_payload_returns_structured_context(self) -> None:
        plan_path = self.write_plan()
        payload = {"tool_input": {"file_path": str(plan_path)}, "cwd": str(self.root)}
        runner = self.fake_runner("Verdict: BLOCK\nFindings:\n- Missing rollback")
        response = plan_review.process_hook_payload(payload, self.env, runner)
        self.assertIn("hookSpecificOutput", response)
        context = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Codex second opinion for PLAN.md", context)
        self.assertIn("Missing rollback", context)

    def test_codex_invocation_is_read_only_and_never_dangerous(self) -> None:
        plan_path = self.write_plan(content="simple plan with auth changes")
        payload = {"tool_input": {"file_path": str(plan_path)}, "cwd": str(self.root)}
        runner = self.fake_runner()
        plan_review.process_hook_payload(payload, self.env, runner)
        cmd = runner.call_args.args[0]
        self.assertIn("read-only", cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertIn("deep-model", cmd)

    def test_default_model_is_gpt_5_4_when_env_not_set(self) -> None:
        env = {"CLAUDE_CODEX_PLAN_REVIEW_CACHE": str(self.cache_file)}
        plan_path = self.write_plan(content="short plan")
        payload = {"tool_input": {"file_path": str(plan_path)}, "cwd": str(self.root)}
        runner = self.fake_runner()
        plan_review.process_hook_payload(payload, env, runner)
        cmd = runner.call_args.args[0]
        model_index = cmd.index("-m") + 1
        self.assertEqual(cmd[model_index], "gpt-5.4")

    def test_cached_review_skips_second_codex_call(self) -> None:
        plan_path = self.write_plan()
        payload = {"tool_input": {"file_path": str(plan_path)}, "cwd": str(self.root)}
        runner = self.fake_runner("Verdict: APPROVE")
        first = plan_review.process_hook_payload(payload, self.env, runner)
        second = plan_review.process_hook_payload(payload, self.env, runner)
        self.assertEqual(runner.call_count, 1)
        self.assertIn("[cached]", second["hookSpecificOutput"]["additionalContext"])
        self.assertIn("Verdict: APPROVE", first["hookSpecificOutput"]["additionalContext"])

    def test_failure_is_reported_explicitly(self) -> None:
        plan_path = self.write_plan()
        payload = {"tool_input": {"file_path": str(plan_path)}, "cwd": str(self.root)}

        def failing_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 1, "", "simulated failure")

        response = plan_review.process_hook_payload(payload, self.env, failing_runner)
        context = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("unavailable", context.lower())
        self.assertIn("No second opinion was produced", context)

    def test_main_reads_stdin_and_prints_json(self) -> None:
        plan_path = self.write_plan()
        payload = {"tool_input": {"file_path": str(plan_path)}, "cwd": str(self.root)}
        with mock.patch("sys.stdin.read", return_value=json.dumps(payload)):
            with mock.patch.object(
                plan_review,
                "run_codex_review",
                return_value=("Verdict: CONCERNS", None),
            ) as mocked_review:
                with mock.patch("builtins.print") as mocked_print:
                    exit_code = plan_review.main([])
        self.assertEqual(exit_code, 0)
        mocked_print.assert_called_once()
        printed = mocked_print.call_args.args[0]
        loaded = json.loads(printed)
        self.assertTrue(loaded["continue"])
        self.assertTrue(mocked_review.called)


if __name__ == "__main__":
    unittest.main()
