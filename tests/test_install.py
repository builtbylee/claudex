from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "scripts" / "install.py"
SPEC = importlib.util.spec_from_file_location("install_script", MODULE_PATH)
install = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = install
SPEC.loader.exec_module(install)


class InstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ensure_hook_adds_expected_entry(self) -> None:
        updated = install.ensure_hook({})
        hooks = updated["hooks"]["PostToolUse"]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["matcher"], "Write|Edit|MultiEdit")
        self.assertEqual(hooks[0]["hooks"][0]["command"], install.HOOK_COMMAND)

    def test_ensure_hook_is_idempotent(self) -> None:
        original = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit|MultiEdit",
                        "hooks": [{"type": "command", "command": install.HOOK_COMMAND, "timeout": 90}],
                    }
                ]
            }
        }
        updated = install.ensure_hook(original)
        hooks = updated["hooks"]["PostToolUse"]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["hooks"][0]["timeout"], 180)

    def test_read_json_returns_empty_dict_for_missing_file(self) -> None:
        self.assertEqual(install.read_json(self.root / "missing.json"), {})

    def test_write_json_round_trips(self) -> None:
        target = self.root / "settings.json"
        install.write_json(target, {"hooks": {"PostToolUse": []}})
        loaded = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(loaded, {"hooks": {"PostToolUse": []}})
