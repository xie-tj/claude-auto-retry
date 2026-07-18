import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "src" / "claude_auto.py"


def load_module(home):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CLAUDE_AUTO_")
    }
    environment.update(
        {
            "HOME": str(home),
            "CLAUDE_AUTO_APP_DIR": str(home / ".local" / "share" / "claude-auto"),
            "CLAUDE_AUTO_CONFIG_DIR": str(home / ".config" / "claude-auto"),
            "CLAUDE_AUTO_STATE_DIR": str(home / ".local" / "state" / "claude-auto"),
            "CLAUDE_AUTO_IPC_DIR": str(home / "ipc"),
            "CLAUDE_AUTO_SETTINGS_PATH": str(home / ".claude" / "settings.json"),
        }
    )
    with mock.patch.dict(os.environ, environment, clear=True):
        spec = importlib.util.spec_from_file_location("claude_auto_test", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        (self.home / ".local" / "share").mkdir(parents=True)
        self.module = load_module(self.home)

    def tearDown(self):
        self.temporary.cleanup()

    def test_classifies_supported_failures_only(self):
        classify = self.module.classify_failure
        self.assertEqual(
            classify({"last_assistant_message": "API Error: The operation timed out."}),
            "timeout",
        )
        self.assertEqual(
            classify(
                {
                    "error_details": (
                        "422 格式转换错误: Responses upstream service_unavailable_error: "
                        "Our servers are currently overloaded. Please try again later."
                    )
                }
            ),
            "overloaded",
        )
        self.assertEqual(
            classify({"error_type": "timeout", "error": "unknown"}),
            "timeout",
        )
        self.assertEqual(
            classify({"error_type": "overloaded", "error": "unknown"}),
            "overloaded",
        )
        self.assertEqual(
            classify({"error_details": {"error": {"type": "service_unavailable_error"}}}),
            "overloaded",
        )
        self.assertIsNone(classify({"error_details": "API Error: 422 invalid request"}))

    def test_double_dash_stops_wrapper_parsing(self):
        self.assertIsNone(self.module.direct_reason(["--", "install"]))
        self.assertEqual(self.module.direct_reason(["install"]), "management command")
        self.assertEqual(
            self.module.parse_session_request(
                ["-p", "--", "--session-id=00000000-0000-4000-8000-000000000000"]
            ),
            (None, False),
        )

    def test_recovery_does_not_replay_original_prompt(self):
        args = self.module.build_recovery_args(
            ["-p", "--model", "opus", "original prompt"],
            "00000000-0000-4000-8000-000000000000",
            "continue safely",
        )
        self.assertNotIn("original prompt", args)
        self.assertIn("--resume", args)
        self.assertIn("continue safely", args)
        self.assertEqual(args.count("--model"), 1)
        self.assertIsNone(
            self.module.build_recovery_args(
                ["-p", "--file", "task.pdf", "original task"],
                "00000000-0000-4000-8000-000000000000",
                "continue safely",
            )
        )
        replay = self.module.build_recovery_args(
            ["-p", "--replay-user-messages", "original prompt"],
            "00000000-0000-4000-8000-000000000000",
            "continue safely",
        )
        self.assertNotIn("--replay-user-messages", replay)
        self.assertNotIn("original prompt", replay)
        for option in ("--debug", "-d", "--prompt-suggestions"):
            recovered = self.module.build_recovery_args(
                ["-p", option, "api", "original prompt"],
                "00000000-0000-4000-8000-000000000000",
                "continue safely",
            )
            self.assertIn(option, recovered)
            self.assertIn("api", recovered)
            self.assertNotIn("original prompt", recovered)

    def test_recovery_provenance_is_one_shot(self):
        run_id = "a" * 32
        self.module.run_dir(run_id).mkdir(parents=True)
        prompt = "[claude-auto recovery 1/3][timeout] continue"
        self.assertFalse(self.module.consume_expected_recovery(run_id, prompt))
        self.module.mark_expected_recovery(run_id, prompt)
        self.assertTrue(self.module.consume_expected_recovery(run_id, prompt))
        self.assertFalse(self.module.consume_expected_recovery(run_id, prompt))

    def test_remove_hooks_preserves_unrelated_hooks(self):
        command = f"python3 {self.module.SCRIPT} hook"
        self.module.atomic_json(
            self.module.CONFIG_DIR / "install-manifest.json",
            {"hook_command": command},
        )
        related_text = f"notify --about {self.module.SCRIPT}"
        settings = {
            "model": "sonnet",
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": command}]},
                    {"hooks": [{"type": "command", "command": "notify-me"}]},
                    {"hooks": [{"type": "command", "command": related_text}]},
                ]
            },
        }
        result = self.module.remove_our_hooks(settings)
        self.assertEqual(result["model"], "sonnet")
        self.assertEqual(
            result["hooks"]["Stop"],
            [
                {"hooks": [{"type": "command", "command": "notify-me"}]},
                {"hooks": [{"type": "command", "command": related_text}]},
            ],
        )


if __name__ == "__main__":
    unittest.main()
