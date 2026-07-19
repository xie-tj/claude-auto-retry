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

    def test_submission_status_reports_retry_phase_and_rounded_deadline(self):
        countdown = self.module.status_text(
            "countdown",
            {"name": "demo"},
            retry_count=1,
            category="timeout",
            remaining=4.01,
            binding=False,
        )
        self.assertIn("recovery 1/3 in 5s", countdown)

        initial = self.module.status_text(
            "submitting",
            {"name": "demo"},
            retry_count=1,
            remaining=4.01,
            binding=True,
            phase="initial_wait",
            sent_attempts=1,
        )
        self.assertIn("quick retry pending", initial)
        self.assertIn("final retry in 5s", initial)
        self.assertNotIn("取消", initial)

        failed = self.module.status_text(
            "unconfirmed",
            {"name": "demo"},
            retry_count=1,
            sent_attempts=2,
            failed_attempts=1,
        )
        self.assertIn("2 Enter sent/1 failed", failed)
        self.assertIn("press Enter if text remains", failed)

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

    def test_tmux_injection_sends_only_the_initial_submit_key(self):
        run_id = "4" * 32
        self.create_run(run_id)
        submit_keys = []
        sleeps = []

        def tmux_run(arguments, **_):
            if arguments[0] == "send-keys" and arguments[-1] == "Enter":
                submit_keys.append(arguments)
            return Result()

        prompt = self.module.recovery_message(1, "timeout")
        with mock.patch.object(
            self.module, "tmux_target_alive", return_value=True
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
        ), mock.patch.object(
            self.module.time, "sleep", side_effect=sleeps.append
        ):
            self.assertTrue(
                self.module.inject_recovery(
                    {"main_pane": "%42"}, prompt, run_id
                )
            )
        self.assertEqual(len(submit_keys), 1)
        self.assertEqual(sleeps, [self.module.PASTE_SETTLE_DELAY])

    def test_tmux_injection_honors_cancel_during_paste_settle_delay(self):
        run_id = "9" * 32
        directory = self.create_run(run_id)
        submit_keys = []

        def tmux_run(arguments, **_):
            if (
                arguments[0] == "send-keys"
                and arguments[-1] in {"Enter", "C-m"}
            ):
                submit_keys.append(arguments)
            return Result()

        def cancel_during_settle(_):
            (directory / "cancel").touch()

        prompt = self.module.recovery_message(1, "timeout")
        with mock.patch.object(
            self.module, "tmux_target_alive", return_value=True
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
        ), mock.patch.object(
            self.module.time,
            "sleep",
            side_effect=cancel_during_settle,
        ):
            self.assertFalse(
                self.module.inject_recovery(
                    {"main_pane": "%12"}, prompt, run_id
                )
            )
        self.assertEqual(submit_keys, [])
        self.assertFalse(
            self.module.consume_expected_recovery(run_id, prompt)
        )

    def test_tmux_submit_key_failure_clears_provenance(self):
        run_id = "6" * 32
        self.create_run(run_id)

        def tmux_run(arguments, **_):
            failed_submit = (
                arguments[0] == "send-keys"
                and arguments[-1] == "Enter"
            )
            return Result(returncode=1 if failed_submit else 0)

        prompt = self.module.recovery_message(1, "timeout")
        with mock.patch.object(
            self.module, "tmux_target_alive", return_value=True
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
        ), mock.patch.object(
            self.module.time, "sleep"
        ):
            self.assertFalse(
                self.module.inject_recovery(
                    {"main_pane": "%8"}, prompt, run_id
                )
            )
        self.assertFalse(
            self.module.consume_expected_recovery(run_id, prompt)
        )

    def test_manual_enter_after_unconfirmed_submit_keeps_recovery_provenance(self):
        run_id = "7" * 32
        self.create_run(run_id)
        prompt = self.module.recovery_message(1, "timeout")
        self.module.mark_expected_recovery(run_id, prompt)
        events = self.run_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-1",
                "prompt": prompt,
            },
            run_id,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "prompt_submit")
        self.assertTrue(events[0]["recovery"])
        self.assertFalse(
            self.module.consume_expected_recovery(run_id, prompt)
        )

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

    def test_watchdog_waits_for_prompt_submit_acknowledgement(self):
        run_id = "5" * 32
        directory = self.create_run(
            run_id,
            main_pane="%10",
            cancel_binding=False,
        )
        alive = {"value": True}
        with mock.patch.dict(
            self.module.ERROR_POLICIES,
            {
                "timeout": {
                    "delays": (0.01, 0.01, 0.01),
                    "label": "请求超时",
                }
            },
            clear=False,
        ), mock.patch.object(
            self.module,
            "tmux_target_alive",
            side_effect=lambda *_: alive["value"],
        ), mock.patch.object(
            self.module, "inject_recovery", return_value=True
        ), mock.patch.object(
            self.module, "render_watchdog_status"
        ), mock.patch.object(
            self.module, "release_tmux_binding"
        ), mock.patch.object(
            self.module, "cleanup_run"
        ):
            thread = threading.Thread(
                target=self.module.watchdog_main,
                args=(run_id, 0),
                daemon=True,
            )
            thread.start()
            deadline = time.time() + 2
            while not (directory / "ready").exists() and time.time() < deadline:
                time.sleep(0.01)
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
            while (
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state")
                not in {"submitting", "awaiting"}
                and time.time() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state"),
                "submitting",
            )
            self.module.send_run_event(
                run_id,
                {
                    "kind": "prompt_submit",
                    "at": time.time(),
                    "run_id": run_id,
                    "session_id": "session-1",
                    "recovery": True,
                },
            )
            deadline = time.time() + 2
            while (
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state")
                != "awaiting"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state"),
                "awaiting",
            )
            alive["value"] = False
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_any_prompt_submit_prevents_all_recovery_submit_retries(self):
        for recovery, expected_state in ((True, "awaiting"), (False, "ready")):
            with self.subTest(recovery=recovery):
                run_id = ("a" if recovery else "c") * 32
                directory = self.create_run(
                    run_id,
                    main_pane="%15",
                    tmux_identity="pane-15",
                    cancel_binding=False,
                )
                alive = {"value": True}
                retry_keys = []
                prompt = self.module.recovery_message(1, "timeout")

                def inject(*_):
                    self.module.mark_expected_recovery(run_id, prompt)
                    return True

                def tmux_run(arguments, **_):
                    if arguments[0] == "send-keys" and arguments[-1] == "Enter":
                        retry_keys.append(arguments)
                    return Result()

                with mock.patch.dict(
                    self.module.ERROR_POLICIES,
                    {"timeout": {"delays": (0.01,) * 3, "label": "请求超时"}},
                    clear=False,
                ), mock.patch.object(
                    self.module, "SUBMIT_RETRY_DELAY", 0.2
                ), mock.patch.object(
                    self.module, "SUBMISSION_ACK_TIMEOUT", 0.4
                ), mock.patch.object(
                    self.module,
                    "tmux_target_alive",
                    side_effect=lambda *_: alive["value"],
                ), mock.patch.object(
                    self.module, "inject_recovery", side_effect=inject
                ), mock.patch.object(
                    self.module, "tmux_run", side_effect=tmux_run
                ), mock.patch.object(
                    self.module, "render_watchdog_status"
                ), mock.patch.object(
                    self.module, "release_tmux_binding"
                ), mock.patch.object(
                    self.module, "cleanup_run"
                ):
                    thread = threading.Thread(
                        target=self.module.watchdog_main,
                        args=(run_id, 0),
                        daemon=True,
                    )
                    thread.start()
                    deadline = time.time() + 2
                    while not (directory / "ready").exists() and time.time() < deadline:
                        time.sleep(0.01)
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
                    while self.module.read_json(directory / "status.json", {}).get("state") != "submitting" and time.time() < deadline:
                        time.sleep(0.01)
                    self.module.send_run_event(
                        run_id,
                        {
                            "kind": "prompt_submit",
                            "at": time.time(),
                            "run_id": run_id,
                            "session_id": "session-1",
                            "recovery": recovery,
                        },
                    )
                    deadline = time.time() + 2
                    while self.module.read_json(directory / "status.json", {}).get("state") != expected_state and time.time() < deadline:
                        time.sleep(0.01)
                    time.sleep(0.5)
                    self.assertEqual(retry_keys, [])
                    self.assertEqual(
                        self.module.read_json(directory / "status.json", {}).get("state"),
                        expected_state,
                    )
                    alive["value"] = False
                    thread.join(2)
                    self.assertFalse(thread.is_alive())

    def test_consumed_recovery_waits_for_delayed_prompt_submit_without_retrying(self):
        run_id = "1" * 32
        directory = self.create_run(
            run_id,
            main_pane="%16",
            tmux_identity="pane-16",
            cancel_binding=False,
        )
        alive = {"value": True}
        retry_keys = []
        prompt = self.module.recovery_message(1, "timeout")

        def inject(*_):
            self.module.mark_expected_recovery(run_id, prompt)
            return True

        def tmux_run(arguments, **_):
            if arguments[0] == "send-keys" and arguments[-1] == "Enter":
                retry_keys.append(arguments)
            return Result()

        with mock.patch.dict(
            self.module.ERROR_POLICIES,
            {"timeout": {"delays": (0.01,) * 3, "label": "请求超时"}},
            clear=False,
        ), mock.patch.object(
            self.module, "SUBMIT_RETRY_DELAY", 0.05
        ), mock.patch.object(
            self.module, "SUBMISSION_ACK_TIMEOUT", 0.1
        ), mock.patch.object(
            self.module,
            "tmux_target_alive",
            side_effect=lambda *_: alive["value"],
        ), mock.patch.object(
            self.module, "inject_recovery", side_effect=inject
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
        ), mock.patch.object(
            self.module, "render_watchdog_status"
        ), mock.patch.object(
            self.module, "release_tmux_binding"
        ), mock.patch.object(
            self.module, "cleanup_run"
        ):
            thread = threading.Thread(
                target=self.module.watchdog_main,
                args=(run_id, 0),
                daemon=True,
            )
            thread.start()
            deadline = time.time() + 2
            while not (directory / "ready").exists() and time.time() < deadline:
                time.sleep(0.01)
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
            while self.module.read_json(directory / "status.json", {}).get("state") != "submitting" and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.module.consume_expected_recovery(run_id, prompt))
            deadline = time.time() + 2
            while self.module.read_json(directory / "status.json", {}).get("phase") != "syncing" and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.3)
            self.assertEqual(retry_keys, [])
            self.assertEqual(
                self.module.read_json(directory / "status.json", {}).get("phase"),
                "syncing",
            )
            self.module.send_run_event(
                run_id,
                {
                    "kind": "prompt_submit",
                    "at": time.time(),
                    "run_id": run_id,
                    "session_id": "session-1",
                    "recovery": True,
                },
            )
            deadline = time.time() + 2
            while self.module.read_json(directory / "status.json", {}).get("state") != "awaiting" and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(
                self.module.read_json(directory / "status.json", {}).get("state"),
                "awaiting",
            )
            alive["value"] = False
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_quick_retry_failure_still_allows_final_retry(self):
        run_id = "2" * 32
        directory = self.create_run(
            run_id,
            main_pane="%17",
            tmux_identity="pane-17",
            cancel_binding=False,
        )
        alive = {"value": True}
        retry_results = [Result(returncode=1), Result()]
        prompt = self.module.recovery_message(1, "timeout")

        def inject(*_):
            self.module.mark_expected_recovery(run_id, prompt)
            return True

        def tmux_run(arguments, **_):
            if arguments[0] == "send-keys" and arguments[-1] == "Enter":
                return retry_results.pop(0)
            return Result()

        with mock.patch.dict(
            self.module.ERROR_POLICIES,
            {"timeout": {"delays": (0.01,) * 3, "label": "请求超时"}},
            clear=False,
        ), mock.patch.object(
            self.module, "SUBMIT_RETRY_DELAY", 0.05
        ), mock.patch.object(
            self.module, "SUBMISSION_ACK_TIMEOUT", 0.15
        ), mock.patch.object(
            self.module,
            "tmux_target_alive",
            side_effect=lambda *_: alive["value"],
        ), mock.patch.object(
            self.module, "inject_recovery", side_effect=inject
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
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
            deadline = time.time() + 3
            while self.module.read_json(directory / "status.json", {}).get("state") != "unconfirmed" and time.time() < deadline:
                time.sleep(0.01)
            status = self.module.read_json(directory / "status.json", {})
            self.assertEqual(status.get("state"), "unconfirmed")
            self.assertEqual(status.get("sent_attempts"), 2)
            self.assertEqual(status.get("failed_attempts"), 1)
            self.assertEqual(retry_results, [])
            alive["value"] = False
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_final_retry_failure_stops_automatic_submission_and_keeps_provenance(self):
        run_id = "3" * 32
        directory = self.create_run(
            run_id,
            main_pane="%18",
            tmux_identity="pane-18",
            cancel_binding=False,
        )
        alive = {"value": True}
        retry_results = [Result(), Result(returncode=1)]
        prompt = self.module.recovery_message(1, "timeout")

        def inject(*_):
            self.module.mark_expected_recovery(run_id, prompt)
            return True

        def tmux_run(arguments, **_):
            if arguments[0] == "send-keys" and arguments[-1] == "Enter":
                return retry_results.pop(0)
            return Result()

        with mock.patch.dict(
            self.module.ERROR_POLICIES,
            {"timeout": {"delays": (0.01,) * 3, "label": "请求超时"}},
            clear=False,
        ), mock.patch.object(
            self.module, "SUBMIT_RETRY_DELAY", 0.05
        ), mock.patch.object(
            self.module, "SUBMISSION_ACK_TIMEOUT", 0.15
        ), mock.patch.object(
            self.module,
            "tmux_target_alive",
            side_effect=lambda *_: alive["value"],
        ), mock.patch.object(
            self.module, "inject_recovery", side_effect=inject
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
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
            deadline = time.time() + 3
            while self.module.read_json(directory / "status.json", {}).get("phase") != "final_failed" and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.3)
            status = self.module.read_json(directory / "status.json", {})
            self.assertEqual(status.get("state"), "submitting")
            self.assertEqual(status.get("phase"), "final_failed")
            self.assertEqual(status.get("sent_attempts"), 2)
            self.assertEqual(status.get("failed_attempts"), 1)
            self.assertTrue(self.module.expected_recovery_path(run_id).exists())
            self.assertEqual(retry_results, [])
            alive["value"] = False
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_lost_target_stops_submit_retries(self):
        run_id = "4" * 32
        directory = self.create_run(
            run_id,
            main_pane="%19",
            tmux_identity="pane-19",
            cancel_binding=False,
        )
        checks = {"count": 0}
        retry_keys = []
        prompt = self.module.recovery_message(1, "timeout")

        def target_alive(*_):
            checks["count"] += 1
            return checks["count"] < 4

        def inject(*_):
            self.module.mark_expected_recovery(run_id, prompt)
            return True

        def tmux_run(arguments, **_):
            if arguments[0] == "send-keys" and arguments[-1] == "Enter":
                retry_keys.append(arguments)
            return Result()

        with mock.patch.dict(
            self.module.ERROR_POLICIES,
            {"timeout": {"delays": (0.01,) * 3, "label": "请求超时"}},
            clear=False,
        ), mock.patch.object(
            self.module, "SUBMIT_RETRY_DELAY", 0.05
        ), mock.patch.object(
            self.module, "SUBMISSION_ACK_TIMEOUT", 0.2
        ), mock.patch.object(
            self.module, "tmux_target_alive", side_effect=target_alive
        ), mock.patch.object(
            self.module, "inject_recovery", side_effect=inject
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
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
            while self.module.read_json(directory / "status.json", {}).get("phase") != "target_lost" and time.time() < deadline:
                time.sleep(0.01)
            status = self.module.read_json(directory / "status.json", {})
            self.assertEqual(status.get("state"), "submitting")
            self.assertEqual(status.get("phase"), "target_lost")
            self.assertEqual(retry_keys, [])
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_tmux_identity_probe_failure_is_treated_as_lost_target(self):
        run_id = "5" * 32
        self.create_run(run_id)
        self.module.mark_expected_recovery(run_id, "recovery")
        with mock.patch.object(
            self.module,
            "tmux_target_identity",
            side_effect=OSError("tmux unavailable"),
        ):
            result = self.module.send_recovery_submit_key(
                {"main_pane": "%20", "tmux_identity": "pane-20"},
                run_id,
            )
        self.assertEqual(result, "target_lost")
        self.assertTrue(self.module.expected_recovery_path(run_id).exists())

    def test_unconfirmed_submit_retries_twice_then_requires_manual_action(self):
        run_id = "8" * 32
        directory = self.create_run(
            run_id,
            main_pane="%11",
            tmux_identity="pane-11",
            cancel_binding=False,
        )
        alive = {"value": True}
        retry_keys = []
        prompt = self.module.recovery_message(1, "timeout")

        def inject(*_):
            self.module.mark_expected_recovery(run_id, prompt)
            return True

        def tmux_run(arguments, **_):
            if arguments[0] == "send-keys" and arguments[-1] == "Enter":
                retry_keys.append(arguments)
            return Result()

        with mock.patch.dict(
            self.module.ERROR_POLICIES,
            {
                "timeout": {
                    "delays": (0.01, 0.01, 0.01),
                    "label": "请求超时",
                }
            },
            clear=False,
        ), mock.patch.object(
            self.module, "SUBMIT_RETRY_DELAY", 0.05
        ), mock.patch.object(
            self.module, "SUBMISSION_ACK_TIMEOUT", 0.15
        ), mock.patch.object(
            self.module,
            "tmux_target_alive",
            side_effect=lambda *_: alive["value"],
        ), mock.patch.object(
            self.module, "inject_recovery", side_effect=inject
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
        ), mock.patch.object(
            self.module, "render_watchdog_status"
        ), mock.patch.object(
            self.module, "release_tmux_binding"
        ), mock.patch.object(
            self.module, "cleanup_run"
        ):
            thread = threading.Thread(
                target=self.module.watchdog_main,
                args=(run_id, 0),
                daemon=True,
            )
            thread.start()
            deadline = time.time() + 2
            while not (directory / "ready").exists() and time.time() < deadline:
                time.sleep(0.01)
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
            deadline = time.time() + 3
            while (
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state")
                != "unconfirmed"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            status = self.module.read_json(directory / "status.json", {})
            self.assertEqual(status.get("state"), "unconfirmed")
            self.assertEqual(len(retry_keys), 2)
            self.assertEqual(status.get("sent_attempts"), 3)
            events = self.run_hook(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-1",
                    "prompt": prompt,
                },
                run_id,
            )
            self.assertTrue(events[0]["recovery"])
            self.module.send_run_event(run_id, events[0])
            deadline = time.time() + 2
            while self.module.read_json(directory / "status.json", {}).get("state") != "awaiting" and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(
                self.module.read_json(directory / "status.json", {}).get("state"),
                "awaiting",
            )
            self.assertEqual(len(retry_keys), 2)
            alive["value"] = False
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_new_failure_does_not_replace_unconfirmed_submission(self):
        run_id = "0" * 32
        directory = self.create_run(
            run_id,
            main_pane="%13",
            cancel_binding=False,
        )
        alive = {"value": True}
        injections = []
        prompt = self.module.recovery_message(1, "timeout")

        def inject(*args):
            injections.append(args)
            self.module.mark_expected_recovery(run_id, prompt)
            return True

        with mock.patch.dict(
            self.module.ERROR_POLICIES,
            {
                "timeout": {
                    "delays": (0.01, 0.01, 0.01),
                    "label": "请求超时",
                }
            },
            clear=False,
        ), mock.patch.object(
            self.module, "SUBMISSION_ACK_TIMEOUT", 0.05
        ), mock.patch.object(
            self.module,
            "tmux_target_alive",
            side_effect=lambda *_: alive["value"],
        ), mock.patch.object(
            self.module, "inject_recovery", side_effect=inject
        ), mock.patch.object(
            self.module, "render_watchdog_status"
        ), mock.patch.object(
            self.module, "release_tmux_binding"
        ), mock.patch.object(
            self.module, "cleanup_run"
        ):
            thread = threading.Thread(
                target=self.module.watchdog_main,
                args=(run_id, 0),
                daemon=True,
            )
            thread.start()
            deadline = time.time() + 2
            while not (directory / "ready").exists() and time.time() < deadline:
                time.sleep(0.01)
            first = {
                "kind": "recoverable_failure",
                "at": time.time(),
                "run_id": run_id,
                "session_id": "session-1",
                "prompt_id": "prompt-1",
                "category": "timeout",
                "subagent": False,
            }
            self.module.send_run_event(run_id, first)
            deadline = time.time() + 2
            while (
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state")
                != "unconfirmed"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            second = dict(first, prompt_id="prompt-2", at=time.time())
            self.module.send_run_event(run_id, second)
            time.sleep(0.35)
            self.assertEqual(len(injections), 1)
            self.assertEqual(
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state"),
                "unconfirmed",
            )
            events = self.run_hook(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-1",
                    "prompt": prompt,
                },
                run_id,
            )
            self.assertTrue(events[0]["recovery"])
            self.module.send_run_event(run_id, events[0])
            deadline = time.time() + 2
            while (
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state")
                not in {"countdown", "awaiting"}
                and time.time() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(len(injections), 1)
            self.assertEqual(
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state"),
                "countdown",
            )
            alive["value"] = False
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_cancelled_acknowledged_recovery_cannot_schedule_another_enter(self):
        run_id = "b" * 32
        directory = self.create_run(
            run_id,
            main_pane="%14",
            cancel_binding=False,
        )
        alive = {"value": True}
        injections = []
        prompt = self.module.recovery_message(1, "timeout")

        def inject(*args):
            injections.append(args)
            self.module.mark_expected_recovery(run_id, prompt)
            return True

        with mock.patch.dict(
            self.module.ERROR_POLICIES,
            {
                "timeout": {
                    "delays": (0.01, 0.01, 0.01),
                    "label": "请求超时",
                }
            },
            clear=False,
        ), mock.patch.object(
            self.module, "SUBMISSION_ACK_TIMEOUT", 0.5
        ), mock.patch.object(
            self.module,
            "tmux_target_alive",
            side_effect=lambda *_: alive["value"],
        ), mock.patch.object(
            self.module, "inject_recovery", side_effect=inject
        ), mock.patch.object(
            self.module, "render_watchdog_status"
        ), mock.patch.object(
            self.module, "release_tmux_binding"
        ), mock.patch.object(
            self.module, "cleanup_run"
        ):
            thread = threading.Thread(
                target=self.module.watchdog_main,
                args=(run_id, 0),
                daemon=True,
            )
            thread.start()
            deadline = time.time() + 2
            while not (directory / "ready").exists() and time.time() < deadline:
                time.sleep(0.01)
            failure = {
                "kind": "recoverable_failure",
                "at": time.time(),
                "run_id": run_id,
                "session_id": "session-1",
                "prompt_id": "prompt-1",
                "category": "timeout",
                "subagent": False,
            }
            self.module.send_run_event(run_id, failure)
            deadline = time.time() + 2
            while (
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state")
                != "submitting"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            events = self.run_hook(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-1",
                    "prompt": prompt,
                },
                run_id,
            )
            self.assertTrue(events[0]["recovery"])
            self.module.send_run_event(run_id, events[0])
            deadline = time.time() + 2
            while (
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state")
                != "awaiting"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            (directory / "cancel").touch()
            deadline = time.time() + 2
            while (
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state")
                != "cancelled"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            (directory / "cancel").touch()
            time.sleep(0.3)
            self.module.send_run_event(
                run_id,
                dict(failure, prompt_id="prompt-2", at=time.time()),
            )
            time.sleep(0.35)
            self.assertEqual(len(injections), 1)
            self.assertEqual(
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state"),
                "cancelled",
            )
            alive["value"] = False
            thread.join(2)
            self.assertFalse(thread.is_alive())

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
