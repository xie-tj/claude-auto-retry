import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "src" / "claude_auto.py"


def load_module(home):
    environment = {
        "HOME": str(home),
        "CLAUDE_AUTO_APP_DIR": str(home / ".local" / "share" / "claude-auto"),
        "CLAUDE_AUTO_CONFIG_DIR": str(home / ".config" / "claude-auto"),
        "CLAUDE_AUTO_STATE_DIR": str(home / ".local" / "state" / "claude-auto"),
        "CLAUDE_AUTO_IPC_DIR": str(home / "ipc"),
        "CLAUDE_AUTO_SETTINGS_PATH": str(home / ".claude" / "settings.json"),
    }
    with mock.patch.dict(os.environ, environment, clear=False):
        spec = importlib.util.spec_from_file_location("claude_auto_runtime_test", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    module.ensure_dirs()
    return module


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RuntimeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="claude-auto-test-",
            dir="/tmp",
        )
        self.home = Path(self.temporary.name)
        (self.home / ".local" / "share").mkdir(parents=True)
        self.module = load_module(self.home)

    def tearDown(self):
        self.temporary.cleanup()

    def create_run(self, run_id, **metadata):
        directory = self.module.run_dir(run_id)
        directory.mkdir(parents=True, mode=0o700)
        value = {
            "run_id": run_id,
            "name": run_id[:8],
            "state": "running",
            "supervisor_pid": os.getpid(),
            "supervisor_identity": self.module.process_identity(os.getpid()),
        }
        value.update(metadata)
        self.module.atomic_json(directory / "meta.json", value)
        return directory

    def test_session_lock_is_single_flight(self):
        first = "a" * 32
        second = "b" * 32
        self.create_run(first)
        self.create_run(second)
        acquired, owner = self.module.session_lock_from_hook(first, "session-1", str(self.home))
        self.assertTrue(acquired)
        self.assertIsNone(owner)
        acquired, owner = self.module.session_lock_from_hook(second, "session-1", str(self.home))
        self.assertFalse(acquired)
        self.assertEqual(owner, first)
        self.assertEqual(self.module.get_meta(second)["state"], "blocked")

    def run_hook(self, payload, run_id):
        events = []
        environment = {"CLAUDE_AUTO_RUN_ID": run_id}
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            sys, "stdin", io.StringIO(json.dumps(payload))
        ), mock.patch.object(self.module, "send_run_event", side_effect=lambda _, event: events.append(event) or True):
            self.assertEqual(self.module.hook_main(), 0)
        return events

    def test_hook_routes_only_supported_normalized_failures(self):
        run_id = "c" * 32
        self.create_run(run_id)
        generic = self.run_hook(
            {
                "hook_event_name": "StopFailure",
                "session_id": "session-1",
                "prompt_id": "generic",
                "error_details": "API Error: 422 invalid request",
            },
            run_id,
        )
        self.assertEqual(generic, [])
        subagent = self.run_hook(
            {
                "hook_event_name": "StopFailure",
                "session_id": "session-1",
                "prompt_id": "subagent",
                "error_details": "The operation timed out",
                "agent_id": "agent-1",
            },
            run_id,
        )
        self.assertEqual(len(subagent), 1)
        self.assertEqual(subagent[0]["category"], "timeout")
        self.assertTrue(subagent[0]["subagent"])

    def test_hook_routes_structured_error_type_to_watchdog(self):
        run_id = "1" * 32
        self.create_run(run_id)
        events = self.run_hook(
            {
                "hook_event_name": "StopFailure",
                "session_id": "session-1",
                "prompt_id": "structured",
                "error": "unknown",
                "error_type": "overloaded",
            },
            run_id,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "recoverable_failure")
        self.assertEqual(events[0]["category"], "overloaded")
        self.assertFalse(events[0]["subagent"])

    def test_tmux_injection_targets_exact_pane_and_marks_provenance(self):
        run_id = "d" * 32
        self.create_run(run_id)
        calls = []

        def tmux_run(arguments, **_):
            calls.append(arguments)
            return Result()

        prompt = self.module.recovery_message(1, "timeout")
        with mock.patch.object(self.module, "tmux_target_alive", return_value=True), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
        ):
            self.assertTrue(self.module.inject_recovery({"main_pane": "%42"}, prompt, run_id))
        target_calls = [call for call in calls if call[0] in {"send-keys", "paste-buffer"}]
        self.assertTrue(target_calls)
        self.assertTrue(all("%42" in call for call in target_calls))
        self.assertTrue(self.module.consume_expected_recovery(run_id, prompt))
        self.assertFalse(self.module.consume_expected_recovery(run_id, prompt))

    def test_tmux_injection_failure_clears_provenance(self):
        run_id = "e" * 32
        self.create_run(run_id)

        def tmux_run(arguments, **_):
            return Result(returncode=1 if arguments[0] == "paste-buffer" else 0)

        prompt = self.module.recovery_message(1, "overloaded")
        with mock.patch.object(self.module, "tmux_target_alive", return_value=True), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
        ):
            self.assertFalse(self.module.inject_recovery({"main_pane": "%7"}, prompt, run_id))
        self.assertFalse(self.module.consume_expected_recovery(run_id, prompt))

    def test_client_interrupt_cancels_pending_recovery(self):
        run_id = "2" * 32
        self.create_run(run_id)
        with mock.patch.object(self.module, "tmux_run", side_effect=KeyboardInterrupt):
            self.assertEqual(self.module.attach_managed_session(run_id, "session-1"), 130)
        self.assertTrue((self.module.run_dir(run_id) / "cancel").exists())

    def test_headless_socket_failure_releases_state(self):
        lifecycle = mock.Mock()
        lifecycle.close = mock.Mock()
        self.module.IPC_DIR = self.home / "ipc"
        with mock.patch.object(
            self.module.socket.socket,
            "bind",
            side_effect=OSError("bind failed"),
        ), mock.patch.object(
            self.module, "add_managed_session_id", return_value=(["-p"], "session-1", False)
        ):
            self.assertEqual(self.module.headless_main(["-p"], lifecycle=lifecycle), 1)
        run_directories = list(self.module.RUNS_DIR.iterdir())
        self.assertEqual(len(run_directories), 1)
        meta = self.module.read_json(run_directories[0] / "meta.json", {})
        self.assertEqual(meta["state"], "crashed")
        self.assertFalse(self.module.lock_name("session", "session-1").exists())

    def test_stale_pid_or_tmux_identity_is_not_live(self):
        run_id = "3" * 32
        meta = {
            "state": "running",
            "supervisor_pid": os.getpid(),
            "supervisor_identity": "wrong-process",
            "main_pane": "%1",
            "tmux_identity": "wrong-pane",
        }
        with mock.patch.object(self.module, "tmux_target_identity", return_value="reused-pane"):
            self.assertFalse(self.module.run_is_live(run_id, meta))

    def test_overlong_ipc_directory_fails_before_socket_bind(self):
        self.module.IPC_DIR = Path("/tmp") / ("x" * 80)
        with self.assertRaisesRegex(RuntimeError, "too long"):
            self.module.ensure_private_ipc_dir()

    def test_cancelled_countdown_never_injects(self):
        run_id = "f" * 32
        directory = self.create_run(run_id, main_pane="%9", cancel_binding=False)
        sent = []
        alive = {"value": True}
        with mock.patch.object(self.module, "TIMEOUT_DELAYS", (0.2, 0.2, 0.2)), mock.patch.dict(
            self.module.ERROR_POLICIES,
            {"timeout": {"delays": (0.2, 0.2, 0.2), "label": "请求超时"}},
            clear=False,
        ), mock.patch.object(
            self.module, "tmux_target_alive", side_effect=lambda *_: alive["value"]
        ), mock.patch.object(
            self.module, "inject_recovery", side_effect=lambda *args: sent.append(args) or True
        ), mock.patch.object(
            self.module, "render_watchdog_status"
        ), mock.patch.object(
            self.module, "release_tmux_binding"
        ), mock.patch.object(
            self.module, "cleanup_run"
        ):
            thread = threading.Thread(target=self.module.watchdog_main, args=(run_id, 0), daemon=True)
            thread.start()
            deadline = time.time() + 2
            while not (directory / "ready").exists() and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue((directory / "ready").exists())
            self.module.send_run_event(
                run_id,
                {
                    "kind": "recoverable_failure",
                    "at": time.time(),
                    "run_id": run_id,
                    "session_id": "session-1",
                    "prompt_id": "prompt-1",
                    "category": "timeout",
                    "subagent": False,
                },
            )
            deadline = time.time() + 2
            while self.module.read_json(directory / "status.json", {}).get("state") != "countdown" and time.time() < deadline:
                time.sleep(0.01)
            (directory / "cancel").touch()
            time.sleep(0.35)
            self.assertEqual(sent, [])
            alive["value"] = False
            thread.join(2)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
