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

    def test_submission_status_hides_internal_enter_retry_phases(self):
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
        self.assertEqual(initial, "Submitting recovery 1/3")
        self.assertNotIn("retry", initial.casefold())

        failed = self.module.status_text(
            "unconfirmed",
            {"name": "demo"},
            retry_count=1,
            sent_attempts=2,
            failed_attempts=1,
        )
        self.assertEqual(
            failed,
            "Submit not confirmed · press Enter if recovery remains",
        )
        self.assertNotIn("2 Enter", failed)

    def test_status_text_uses_single_line_user_facing_states(self):
        cases = [
            (
                self.module.status_text("ready", {"name": "demo"}),
                "Recovery ready · v1.0.5",
            ),
            (
                self.module.status_text(
                    "countdown",
                    {"name": "demo"},
                    retry_count=1,
                    category="overloaded",
                    remaining=13.01,
                    binding=True,
                    skip_key="C-a X",
                ),
                "Service overloaded · recovery 1/3 in 14s · C-a X skip",
            ),
            (
                self.module.status_text(
                    "submitting", {"name": "demo"}, retry_count=1
                ),
                "Submitting recovery 1/3",
            ),
            (
                self.module.status_text(
                    "awaiting", {"name": "demo"}, retry_count=1
                ),
                "Recovery 1/3 active",
            ),
            (
                self.module.status_text(
                    "completed", {"name": "demo"}, retry_count=1
                ),
                "Recovery 1/3 complete",
            ),
            (
                self.module.status_text("skipped", {"name": "demo"}),
                "Recovery skipped",
            ),
            (
                self.module.status_text("cancelled", {"name": "demo"}),
                "Submit retries stopped",
            ),
            (
                self.module.status_text(
                    "unconfirmed", {"name": "demo"}, retry_count=1
                ),
                "Submit not confirmed · press Enter if recovery remains",
            ),
            (
                self.module.status_text("exhausted", {"name": "demo"}),
                "Recovery stopped after 3 attempts · inspect session",
            ),
            (
                self.module.status_text("unsupported", {"name": "demo"}),
                "Recovery stopped · inspect session",
            ),
        ]
        for actual, expected in cases:
            self.assertEqual(actual, expected)
            self.assertNotIn("\n", actual)

    def test_status_presentation_adapts_style_and_width(self):
        rich = self.module.present_status(
            "countdown",
            "Service overloaded · recovery 1/3 in 14s · C-b X skip",
            width=80,
            color=True,
            unicode=True,
        )
        self.assertEqual(
            rich,
            "#[fg=cyan]◔#[default] Service overloaded · recovery 1/3 in 14s · C-b X skip",
        )
        compact = self.module.present_status(
            "countdown",
            "Service overloaded · recovery 1/3 in 14s · C-b X skip",
            width=43,
            color=False,
            unicode=True,
        )
        self.assertEqual(compact, "◔ Service overloaded · recovery 1/3 in 14s")
        shortest = self.module.present_status(
            "countdown",
            "Service overloaded · recovery 1/3 in 14s · C-b X skip",
            width=22,
            color=False,
            unicode=False,
        )
        self.assertEqual(shortest, "[~] Recovery 1/3 14s")
        self.assertEqual(
            self.module.present_status(
                "ready", "Recovery ready · v1.0.5", width=80, color=False, unicode=False
            ),
            "[o] Recovery ready | v1.0.5",
        )
        self.assertEqual(
            self.module.present_status(
                "paused", "Recovery paused", width=80, color=True, unicode=True
            ),
            "#[dim]○#[default] Recovery paused",
        )
        self.assertEqual(
            self.module.present_status(
                "update_available",
                "Update installed · restart to update",
                width=80,
                color=True,
                unicode=True,
            ),
            "#[fg=yellow]!#[default] Update installed · restart to update",
        )
        self.assertEqual(
            self.module.present_status(
                "unsupported", "Recovery stopped · inspect session", width=80, color=True, unicode=True
            ),
            "#[fg=red]!#[default] Recovery stopped · inspect session",
        )

    def test_default_tmux_display_uses_bottom_border_on_main_pane(self):
        run_id = "2" * 32
        self.create_run(run_id, window_id="@7", main_pane="%42")
        calls = []

        def tmux_run(arguments, **_):
            calls.append(arguments)
            if arguments[-1] == "pane-border-status" and "-A" in arguments:
                return Result(stdout="off\n")
            if arguments[-1] == "pane-border-format" and "-A" in arguments:
                return Result(stdout="#{pane_index} #{pane_title}\n")
            return Result()

        with mock.patch.object(self.module, "tmux_run", side_effect=tmux_run):
            mode = self.module.prepare_watchdog_display(run_id, lines=24)

        self.assertEqual(mode, "border")
        self.assertIn(
            ["set-option", "-w", "-t", "@7", "pane-border-status", "bottom"],
            calls,
        )
        format_calls = [
            call
            for call in calls
            if call[:5] == ["set-option", "-w", "-t", "@7", "pane-border-format"]
        ]
        self.assertEqual(len(format_calls), 1)
        self.assertEqual(
            format_calls[0][-1],
            "#{?@claude_auto_watchdog_main, #{@claude_auto_watchdog_status} ,}",
        )
        self.assertNotIn("pane_active", format_calls[0][-1])
        self.assertIn(
            [
                "set-option", "-p", "-t", "%42",
                "@claude_auto_watchdog_main", "1",
            ],
            calls,
        )
        status_calls = [
            call
            for call in calls
            if call[:5] == [
                "set-option", "-w", "-t", "@7", "@claude_auto_watchdog_status"
            ]
        ]
        self.assertEqual(
            status_calls[-1][-1],
            "#[fg=cyan]◔#[default] Starting auto-recovery…",
        )
        saved = self.module.get_meta(run_id)["tmux_border_snapshot"]
        self.assertFalse(saved["pane-border-status"]["local"])
        self.assertFalse(saved["pane-border-format"]["local"])

    def test_border_renderer_only_updates_namespaced_status(self):
        calls = []
        meta = {"window_id": "@7", "main_pane": "%42"}
        with mock.patch.object(
            self.module,
            "tmux_run",
            side_effect=lambda arguments, **_: calls.append(arguments) or Result(),
        ):
            self.module.render_watchdog_status(
                "ready", "Recovery ready · v1.0.5", "border", meta
            )

        self.assertEqual(
            calls,
            [[
                "set-option", "-w", "-t", "@7", "@claude_auto_watchdog_status",
                "#[fg=cyan]●#[default] Recovery ready · v1.0.5",
            ]],
        )

    def test_border_renderer_retries_after_tmux_exception(self):
        attempts = []

        def tmux_run(arguments, **_):
            attempts.append(arguments)
            if len(attempts) == 1:
                raise OSError("tmux unavailable")
            return Result()

        with mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
        ):
            failed_frame = self.module.render_watchdog_status(
                "ready", "Recovery ready · v1.0.5", "border",
                {"window_id": "@7"},
            )
            rendered_frame = self.module.render_watchdog_status(
                "ready", "Recovery ready · v1.0.5", "border",
                {"window_id": "@7"}, failed_frame,
            )

        self.assertIsNone(failed_frame)
        self.assertEqual(
            rendered_frame,
            (
                "border",
                "@7",
                "#[fg=cyan]●#[default] Recovery ready · v1.0.5",
            ),
        )
        self.assertEqual(len(attempts), 2)

    def test_idle_watchdog_does_not_redraw_unchanged_border_status(self):
        run_id = "d" * 32
        self.create_run(
            run_id,
            main_pane="%42",
            window_id="@7",
            cancel_binding=False,
        )
        status_calls = []
        settled = threading.Event()
        unchanged_polls = 0

        def tmux_run(arguments, **_):
            if arguments[:5] == [
                "set-option", "-w", "-t", "@7",
                "@claude_auto_watchdog_status",
            ]:
                status_calls.append(arguments)
            return Result()

        def observe_terminal(_):
            nonlocal unchanged_polls
            if status_calls:
                unchanged_polls += 1
                if unchanged_polls >= 2:
                    settled.set()
            return None

        with mock.patch.object(
            self.module, "tmux_target_alive", return_value=True
        ), mock.patch.object(
            self.module, "terminal_failure_observation",
            side_effect=observe_terminal,
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
        ), mock.patch.object(
            self.module, "cleanup_run"
        ):
            thread = threading.Thread(
                target=self.module.watchdog_main,
                args=(run_id, "border"),
                daemon=True,
            )
            thread.start()
            self.assertTrue(
                settled.wait(2), "watchdog did not complete repeated render cycles"
            )
            self.module.send_run_event(
                run_id,
                self.module.event_record(
                    "session_end", run_id, session_id="session-1"
                ),
            )
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(status_calls), 1)

    def test_watchdog_renders_each_changed_border_status_once(self):
        run_id = "e" * 32
        self.create_run(
            run_id,
            main_pane="%42",
            window_id="@7",
            cancel_binding=False,
        )
        status_values = []
        first_rendered = threading.Event()
        settled = threading.Event()
        unchanged_polls = 0

        def tmux_run(arguments, **_):
            if arguments[:5] == [
                "set-option", "-w", "-t", "@7",
                "@claude_auto_watchdog_status",
            ]:
                status_values.append(arguments[-1])
                if len(status_values) == 1:
                    first_rendered.set()
            return Result()

        def observe_terminal(_):
            nonlocal unchanged_polls
            if len(status_values) >= 2:
                unchanged_polls += 1
                if unchanged_polls >= 2:
                    settled.set()
            return None

        with mock.patch.object(
            self.module, "tmux_target_alive", return_value=True
        ), mock.patch.object(
            self.module, "terminal_failure_observation",
            side_effect=observe_terminal,
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
        ), mock.patch.object(
            self.module, "cleanup_run"
        ):
            thread = threading.Thread(
                target=self.module.watchdog_main,
                args=(run_id, "border"),
                daemon=True,
            )
            thread.start()
            self.assertTrue(first_rendered.wait(2), "ready status was not rendered")
            self.module.GLOBAL_PAUSE.touch(mode=0o600)
            self.assertTrue(
                settled.wait(2), "paused status did not settle after rendering"
            )
            self.module.send_run_event(
                run_id,
                self.module.event_record(
                    "session_end", run_id, session_id="session-1"
                ),
            )
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(
            status_values,
            [
                "#[fg=cyan]●#[default] Recovery ready · v1.0.5",
                "#[dim]○#[default] Recovery paused globally · claude-auto resume",
            ],
        )

    def test_watchdog_retries_border_status_that_failed_to_render(self):
        run_id = "f" * 32
        self.create_run(
            run_id,
            main_pane="%42",
            window_id="@7",
            cancel_binding=False,
        )
        status_values = []
        settled = threading.Event()
        unchanged_polls = 0

        def tmux_run(arguments, **_):
            if arguments[:5] == [
                "set-option", "-w", "-t", "@7",
                "@claude_auto_watchdog_status",
            ]:
                status_values.append(arguments[-1])
                return Result(returncode=1 if len(status_values) == 1 else 0)
            return Result()

        def observe_terminal(_):
            nonlocal unchanged_polls
            if len(status_values) >= 2:
                unchanged_polls += 1
                if unchanged_polls >= 2:
                    settled.set()
            return None

        with mock.patch.object(
            self.module, "tmux_target_alive", return_value=True
        ), mock.patch.object(
            self.module, "terminal_failure_observation",
            side_effect=observe_terminal,
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
        ), mock.patch.object(
            self.module, "cleanup_run"
        ):
            thread = threading.Thread(
                target=self.module.watchdog_main,
                args=(run_id, "border"),
                daemon=True,
            )
            thread.start()
            self.assertTrue(
                settled.wait(2), "successful retry did not settle"
            )
            self.module.send_run_event(
                run_id,
                self.module.event_record(
                    "session_end", run_id, session_id="session-1"
                ),
            )
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(
            status_values,
            [
                "#[fg=cyan]●#[default] Recovery ready · v1.0.5",
                "#[fg=cyan]●#[default] Recovery ready · v1.0.5",
            ],
        )

    def test_tmux_display_preserves_custom_border_and_hides_on_short_terminal(self):
        custom_run = "3" * 32
        self.create_run(custom_run, window_id="@8", main_pane="%43")
        custom_calls = []

        def custom_tmux(arguments, **_):
            custom_calls.append(arguments)
            if arguments[-1] == "pane-border-status":
                return Result(stdout="top\n")
            return Result(stdout="#{pane_index} #{pane_title}\n")

        with mock.patch.object(self.module, "tmux_run", side_effect=custom_tmux):
            self.assertEqual(
                self.module.prepare_watchdog_display(custom_run, lines=24), "pane"
            )
        self.assertFalse(any(call[0] == "set-option" for call in custom_calls))

        short_run = "4" * 32
        self.create_run(short_run, window_id="@9", main_pane="%44")
        with mock.patch.object(self.module, "tmux_run") as tmux:
            self.assertEqual(
                self.module.prepare_watchdog_display(short_run, lines=15), "hidden"
            )
        tmux.assert_not_called()

    def test_tmux_border_restore_preserves_local_and_inherited_options(self):
        run_id = "5" * 32
        self.create_run(run_id, window_id="@10", main_pane="%45")
        calls = []

        restoring = {"value": False}
        managed = {}

        def tmux_run(arguments, **_):
            calls.append(arguments)
            if "-A" in arguments:
                if arguments[-1] == "pane-border-status":
                    return Result(stdout="off\n")
                return Result(stdout="#{pane_index} #{pane_title}\n")
            if arguments[0] == "show-options":
                name = arguments[-1]
                if restoring["value"]:
                    value = managed[name]
                    if "-v" in arguments:
                        return Result(stdout=value + "\n")
                    return Result(stdout=name + " " + value + "\n")
                if "-p" in arguments:
                    return Result(stdout="")
                if "-v" in arguments:
                    raw = {
                        "pane-border-status": "off\n",
                        "@claude_auto_watchdog_status": "existing status\n",
                    }
                    return Result(stdout=raw[name])
                formatted = {
                    "pane-border-status": "pane-border-status off\n",
                    "pane-border-format": "",
                    "@claude_auto_watchdog_status": (
                        '@claude_auto_watchdog_status "existing status"\n'
                    ),
                }
                return Result(stdout=formatted[name])
            return Result()

        with mock.patch.object(self.module, "tmux_run", side_effect=tmux_run):
            self.assertEqual(
                self.module.prepare_watchdog_display(run_id, lines=24), "border"
            )
            meta = self.module.get_meta(run_id)
            managed.update(meta["tmux_border_owned"])
            managed[self.module.TMUX_MAIN_PANE_OPTION] = meta[
                "tmux_main_pane_owned"
            ]
            restoring["value"] = True
            calls.clear()
            self.module.restore_watchdog_border(meta)

        mutations = [call for call in calls if call[0] == "set-option"]
        self.assertEqual(
            mutations,
            [
                [
                    "set-option", "-w", "-t", "@10",
                    "pane-border-status", "off",
                ],
                [
                    "set-option", "-u", "-w", "-t", "@10",
                    "pane-border-format",
                ],
                [
                    "set-option", "-w", "-t", "@10",
                    "@claude_auto_watchdog_status", "existing status",
                ],
                [
                    "set-option", "-u", "-p", "-t", "%45",
                    "@claude_auto_watchdog_main",
                ],
            ],
        )

    def test_tmux_border_restore_preserves_runtime_user_changes(self):
        run_id = "8" * 32
        self.create_run(
            run_id,
            window_id="@12",
            main_pane="%48",
            tmux_border_snapshot={
                "pane-border-status": {"local": False},
                "pane-border-format": {"local": False},
                "@claude_auto_watchdog_status": {"local": False},
            },
            tmux_border_owned={
                "pane-border-status": "bottom",
                "pane-border-format": (
                    "#{?@claude_auto_watchdog_main, "
                    "#{@claude_auto_watchdog_status} ,}"
                ),
                "@claude_auto_watchdog_status": "managed status",
            },
            tmux_main_pane_snapshot={"local": False},
            tmux_main_pane_owned="1",
        )
        calls = []
        current = {
            "pane-border-status": "top",
            "pane-border-format": "custom #{pane_index}",
            "@claude_auto_watchdog_status": "managed status",
            "@claude_auto_watchdog_main": "user marker",
        }

        def tmux_run(arguments, **_):
            calls.append(arguments)
            if arguments[0] != "show-options":
                return Result()
            name = arguments[-1]
            if "-v" in arguments:
                return Result(stdout=current[name] + "\n")
            return Result(stdout=name + " " + current[name] + "\n")

        with mock.patch.object(self.module, "tmux_run", side_effect=tmux_run):
            self.module.restore_watchdog_border(self.module.get_meta(run_id))

        mutations = [call for call in calls if call[0] == "set-option"]
        self.assertEqual(
            mutations,
            [[
                "set-option", "-u", "-w", "-t", "@12",
                "@claude_auto_watchdog_status",
            ]],
        )

    def test_watchdog_launch_uses_one_line_only_for_compatibility_pane(self):
        run_id = "6" * 32
        calls = []
        with mock.patch.object(
            self.module,
            "tmux_run",
            side_effect=lambda arguments, **_: calls.append(arguments)
            or Result(stdout="%46\n"),
        ), mock.patch.object(self.module.subprocess, "Popen") as popen:
            self.assertEqual(
                self.module.launch_watchdog(
                    run_id, "pane", "%45", str(self.home)
                ),
                "%46",
            )
        popen.assert_not_called()
        split = calls[0]
        self.assertEqual(split[:6], ["split-window", "-d", "-v", "-l", "1", "-P"])
        self.assertIn(" pane", split[-1])

        process = mock.Mock(pid=321)
        with mock.patch.object(
            self.module.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(
            self.module, "process_identity", return_value="identity"
        ):
            self.assertIsNone(
                self.module.launch_watchdog(
                    run_id, "border", "%45", str(self.home)
                )
            )
        self.assertEqual(popen.call_args.args[0][-1], "border")

    def test_tmux_border_setup_failure_restores_before_fallback(self):
        run_id = "7" * 32
        self.create_run(run_id, window_id="@11", main_pane="%47")
        calls = []
        failed = {"value": False}
        current = {}

        def tmux_run(arguments, **_):
            calls.append(arguments)
            if "-A" in arguments:
                if arguments[-1] == "pane-border-status":
                    return Result(stdout="off\n")
                return Result(stdout="#{pane_index} #{pane_title}\n")
            if arguments[0] == "show-options":
                scope = "pane" if "-p" in arguments else "window"
                name = arguments[-1]
                key = (scope, name)
                if key not in current:
                    return Result(stdout="")
                if "-v" in arguments:
                    return Result(stdout=current[key] + "\n")
                return Result(stdout=name + " " + current[key] + "\n")
            if (
                arguments[:5]
                == ["set-option", "-w", "-t", "@11", "pane-border-format"]
                and not failed["value"]
            ):
                failed["value"] = True
                return Result(returncode=1)
            if arguments[0] == "set-option":
                scope = "pane" if "-p" in arguments else "window"
                name = arguments[-1] if "-u" in arguments else arguments[-2]
                key = (scope, name)
                if "-u" in arguments:
                    current.pop(key, None)
                else:
                    current[key] = arguments[-1]
            return Result()

        with mock.patch.object(self.module, "tmux_run", side_effect=tmux_run):
            mode = self.module.prepare_watchdog_display(run_id, lines=24)

        self.assertEqual(mode, "pane")
        failure_index = next(
            index
            for index, call in enumerate(calls)
            if call[:5]
            == ["set-option", "-w", "-t", "@11", "pane-border-format"]
        )
        restore_calls = [
            call for call in calls[failure_index + 1:] if call[0] == "set-option"
        ]
        self.assertEqual(
            restore_calls,
            [
                [
                    "set-option", "-u", "-w", "-t", "@11",
                    "@claude_auto_watchdog_status",
                ],
                [
                    "set-option", "-u", "-p", "-t", "%47",
                    "@claude_auto_watchdog_main",
                ],
            ],
        )

    def test_tmux_skip_key_uses_effective_prefix_only_when_bound(self):
        calls = []
        with mock.patch.object(
            self.module,
            "tmux_run",
            side_effect=lambda arguments, **_: calls.append(arguments)
            or Result(stdout="C-a\n"),
        ):
            self.assertEqual(
                self.module.tmux_skip_key(True, "managed-session"), "C-a X"
            )
        self.assertEqual(
            calls,
            [["show-options", "-v", "-t", "managed-session", "prefix"]],
        )
        with mock.patch.object(
            self.module,
            "tmux_run",
            return_value=Result(stdout="None\n"),
        ):
            self.assertIsNone(self.module.tmux_skip_key(True, "managed-session"))
        with mock.patch.object(self.module, "tmux_run") as tmux:
            self.assertIsNone(self.module.tmux_skip_key(False, "managed-session"))
        tmux.assert_not_called()

    def test_idle_watchdog_detects_installed_script_update(self):
        run_id = "8" * 32
        directory = self.create_run(
            run_id,
            main_pane="%48",
            cancel_binding=False,
        )
        alive = {"value": True}
        probes = {"count": 0}

        def fingerprint():
            probes["count"] += 1
            return "old" if probes["count"] == 1 else "new"

        with mock.patch.object(
            self.module, "UPDATE_CHECK_INTERVAL", 0.05
        ), mock.patch.object(
            self.module, "script_fingerprint", side_effect=fingerprint
        ), mock.patch.object(
            self.module,
            "tmux_target_alive",
            side_effect=lambda *_: alive["value"],
        ), mock.patch.object(
            self.module, "render_watchdog_status"
        ), mock.patch.object(
            self.module, "cleanup_run"
        ):
            thread = threading.Thread(
                target=self.module.watchdog_main,
                args=(run_id, "hidden"),
                daemon=True,
            )
            thread.start()
            deadline = time.time() + 2
            while (
                self.module.read_json(directory / "status.json", {}).get("state")
                != "update_available"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(
                self.module.read_json(directory / "status.json", {}).get("state"),
                "update_available",
            )
            alive["value"] = False
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_watchdog_distinguishes_global_and_session_pause(self):
        run_id = "9" * 32
        directory = self.create_run(
            run_id,
            main_pane="%49",
            cancel_binding=False,
        )
        alive = {"value": True}
        self.module.GLOBAL_PAUSE.touch(mode=0o600)
        with mock.patch.object(
            self.module,
            "tmux_target_alive",
            side_effect=lambda *_: alive["value"],
        ), mock.patch.object(
            self.module, "render_watchdog_status"
        ), mock.patch.object(
            self.module, "cleanup_run"
        ):
            thread = threading.Thread(
                target=self.module.watchdog_main,
                args=(run_id, "hidden"),
                daemon=True,
            )
            thread.start()
            deadline = time.time() + 2
            while (
                self.module.read_json(directory / "status.json", {}).get("state")
                != "paused_global"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(
                self.module.read_json(directory / "status.json", {}).get("state"),
                "paused_global",
            )
            self.module.GLOBAL_PAUSE.unlink()
            deadline = time.time() + 2
            while (
                self.module.read_json(directory / "status.json", {}).get("state")
                != "ready"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            (directory / "paused").touch(mode=0o600)
            deadline = time.time() + 2
            while (
                self.module.read_json(directory / "status.json", {}).get("state")
                != "paused"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(
                self.module.read_json(directory / "status.json", {}).get("state"),
                "paused",
            )
            alive["value"] = False
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_terminal_capabilities_honor_no_color_dumb_and_encoding(self):
        stream = mock.Mock()
        stream.isatty.return_value = True
        stream.encoding = "utf-8"
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                self.module.terminal_capabilities(stream), (True, True)
            )
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
            self.assertEqual(
                self.module.terminal_capabilities(stream), (False, True)
            )
        with mock.patch.dict(os.environ, {"TERM": "dumb"}, clear=True):
            self.assertEqual(
                self.module.terminal_capabilities(stream), (False, True)
            )
        stream.encoding = "ascii"
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                self.module.terminal_capabilities(stream), (True, False)
            )

    def test_list_sorts_sessions_by_risk_with_plain_pipe_output(self):
        healthy = "a" * 32
        broken = "b" * 32
        active = "c" * 32
        self.create_run(
            healthy,
            name="aaa-ready",
            mode="interactive",
            cwd="/healthy",
            version=self.module.VERSION,
        )
        self.create_run(
            broken,
            name="zzz-broken",
            mode="interactive",
            cwd="/broken",
            version=self.module.VERSION,
        )
        self.create_run(
            active,
            name="mmm-recovering",
            mode="interactive",
            cwd="/active",
            version=self.module.VERSION,
        )
        self.module.atomic_json(
            self.module.run_dir(healthy) / "status.json", {"state": "ready"}
        )
        self.module.atomic_json(
            self.module.run_dir(broken) / "status.json", {"state": "unsupported"}
        )
        self.module.atomic_json(
            self.module.run_dir(active) / "status.json",
            {"state": "countdown", "retry_count": 1, "category": "timeout", "delay": 5},
        )
        output = io.StringIO()
        with mock.patch.object(sys, "stdout", output):
            self.assertEqual(self.module.list_runs(), 0)

        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0].split()[0:2], ["ERROR", "zzz-broken"])
        self.assertEqual(lines[1].split()[0:2], ["ACTIVE", "mmm-recovering"])
        self.assertEqual(lines[2].split()[0:2], ["OK", "aaa-ready"])
        self.assertNotIn("\x1b[", output.getvalue())

    def test_doctor_reports_all_risky_sessions_with_safe_actions(self):
        broken = "d" * 32
        update = "e" * 32
        paused = "f" * 32
        for run_id, name, cwd in (
            (broken, "broken", "/broken"),
            (update, "old-code", "/old"),
            (paused, "paused", "/paused"),
        ):
            self.create_run(
                run_id,
                name=name,
                mode="interactive",
                cwd=cwd,
                version=self.module.VERSION,
            )
        self.module.atomic_json(
            self.module.run_dir(broken) / "status.json", {"state": "target_lost"}
        )
        self.module.atomic_json(
            self.module.run_dir(update) / "status.json",
            {"state": "update_available"},
        )
        self.module.atomic_json(
            self.module.run_dir(paused) / "status.json", {"state": "paused"}
        )
        output = io.StringIO()
        with mock.patch.object(
            self.module,
            "compatibility_check",
            return_value=(True, "2.0.0", False, []),
        ), mock.patch.object(
            self.module, "TMUX", Path("/bin/sh")
        ), mock.patch.object(
            self.module, "tmux_run", return_value=Result(stdout="tmux 3.5\n")
        ), mock.patch.object(sys, "stdout", output):
            self.assertEqual(self.module.doctor(), 1)

        text = output.getvalue()
        self.assertLess(text.index("ERROR broken"), text.index("UPDATE old-code"))
        self.assertLess(text.index("UPDATE old-code"), text.index("PAUSED paused"))
        self.assertIn("claude-auto doctor", text)
        self.assertIn("restart this managed session", text)
        self.assertIn("claude-auto resume paused", text)
        self.assertIn("cwd=/broken", text)

    def test_cli_severity_tag_styles_only_on_tty(self):
        stream = mock.Mock()
        stream.isatty.return_value = True
        stream.encoding = "utf-8"
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                self.module.cli_severity_tag("error", stream),
                "\x1b[31m! ERROR\x1b[0m",
            )
            self.assertEqual(
                self.module.cli_severity_tag("ok", stream),
                "\x1b[32m✓ OK\x1b[0m",
            )
        stream.isatty.return_value = False
        self.assertEqual(self.module.cli_severity_tag("error", stream), "ERROR")

    def test_session_diagnostics_promotes_old_ready_runtime_to_update(self):
        old = "1" * 32
        broken = "2" * 32
        self.create_run(
            old,
            name="old-ready",
            mode="interactive",
            cwd="/old",
            version="1.0.0",
        )
        self.create_run(
            broken,
            name="old-broken",
            mode="interactive",
            cwd="/broken",
            version="1.0.0",
        )
        self.module.atomic_json(
            self.module.run_dir(old) / "status.json", {"state": "ready"}
        )
        self.module.atomic_json(
            self.module.run_dir(broken) / "status.json", {"state": "unsupported"}
        )
        paused = "3" * 32
        self.create_run(
            paused,
            name="old-paused",
            mode="interactive",
            cwd="/paused",
            version="1.0.0",
        )
        self.module.atomic_json(
            self.module.run_dir(paused) / "status.json", {"state": "paused"}
        )

        diagnostics = self.module.session_diagnostics()
        by_name = {row["name"]: row for row in diagnostics}
        self.assertEqual(by_name["old-ready"]["state"], "update_available")
        self.assertEqual(by_name["old-ready"]["severity"], "update")
        self.assertEqual(by_name["old-broken"]["state"], "unsupported")
        self.assertEqual(by_name["old-broken"]["severity"], "error")
        self.assertEqual(by_name["old-paused"]["state"], "update_available")
        self.assertEqual(by_name["old-paused"]["severity"], "update")

    def test_install_report_lists_live_sessions_without_mutating_or_leaking_argv(self):
        run_id = "3" * 32
        directory = self.create_run(
            run_id,
            name="active",
            mode="interactive",
            cwd="/work/project\nunsafe",
            session_id="00000000-0000-4000-8000-000000000000",
            argv=["--settings", "secret-value"],
        )
        before = (directory / "meta.json").read_bytes()
        output = io.StringIO()
        with mock.patch.object(
            self.module, "run_is_live", return_value=True
        ), mock.patch.object(sys, "stdout", output):
            self.assertEqual(self.module.install_report_active(), 0)

        text = output.getvalue()
        self.assertIn("1 managed session started before this installation is still active", text)
        self.assertIn('mode="interactive"', text)
        self.assertIn('cwd="/work/project\\nunsafe"', text)
        self.assertIn(
            'session_id="00000000-0000-4000-8000-000000000000"', text
        )
        self.assertIn(
            "claude --resume 00000000-0000-4000-8000-000000000000", text
        )
        self.assertIn("reapply any launch policy", text)
        self.assertNotIn("secret-value", text)
        self.assertEqual((directory / "meta.json").read_bytes(), before)

    def test_install_report_never_fabricates_resume_command(self):
        run_id = "4" * 32
        self.create_run(
            run_id,
            mode="interactive",
            cwd="/work",
            session_id="not-a-session; touch bad",
        )
        output = io.StringIO()
        with mock.patch.object(
            self.module, "run_is_live", return_value=True
        ), mock.patch.object(sys, "stdout", output):
            self.assertEqual(self.module.install_report_active(), 0)
        text = output.getvalue()
        self.assertIn("normal resume selection", text)
        self.assertNotIn("claude --resume", text)
        self.assertNotIn(run_id, text)

    def test_current_tmux_stock_border_format_is_accepted(self):
        value = (
            '#{?pane_active,#[reverse],}#{pane_index}#[default] "#{pane_title}"'
            '#{?#{mouse},#[align=right]#[range=control|8]'
            '[#{?#{window_zoomed_flag},u,z}]#[norange]'
            '#[range=control|9][x]#[norange],}'
        )
        self.assertTrue(self.module.stock_pane_border_format(value))
        self.assertFalse(
            self.module.stock_pane_border_format(value.replace("pane_title", "custom"))
        )

    def test_skip_and_pause_serialize_with_initial_submission(self):
        run_id = "5" * 32
        directory = self.create_run(
            run_id,
            name="serialized",
            main_pane="%50",
            cancel_binding=False,
        )
        alive = {"value": True}
        injection_started = threading.Event()
        release_injection = threading.Event()
        skip_result = []
        pause_result = []

        def inject(*_):
            injection_started.set()
            release_injection.wait(2)
            return time.monotonic()

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
            self.module, "inject_recovery", side_effect=inject
        ), mock.patch.object(
            self.module, "render_watchdog_status"
        ), mock.patch.object(
            self.module, "cleanup_run"
        ), mock.patch("builtins.print"):
            watchdog = threading.Thread(
                target=self.module.watchdog_main,
                args=(run_id, "hidden"),
                daemon=True,
            )
            watchdog.start()
            try:
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
                self.assertTrue(injection_started.wait(2))
                skipper = threading.Thread(
                    target=lambda: skip_result.append(
                        self.module.skip_run("serialized")
                    ),
                    daemon=True,
                )
                pauser = threading.Thread(
                    target=lambda: pause_result.append(
                        self.module.set_pause("serialized", True)
                    ),
                    daemon=True,
                )
                skipper.start()
                pauser.start()
                time.sleep(0.05)
                self.assertTrue(skipper.is_alive())
                self.assertTrue(pauser.is_alive())
                release_injection.set()
                skipper.join(2)
                pauser.join(2)
                self.assertEqual(skip_result, [1])
                self.assertEqual(pause_result, [0])
                self.assertFalse((directory / "cancel").exists())
                self.assertTrue((directory / "paused").exists())
            finally:
                release_injection.set()
                alive["value"] = False
                watchdog.join(2)
                self.assertFalse(watchdog.is_alive())

    def test_status_presentation_uses_terminal_cell_width(self):
        self.assertEqual(
            self.module.present_status(
                "paused", "恢复恢复恢复恢复恢复恢复", width=10,
                color=False, unicode=True,
            ),
            "○ 恢复恢…",
        )
        self.assertEqual(
            self.module.terminal_cell_width("○ 恢复恢…"), 9
        )
        self.assertEqual(
            self.module.present_status(
                "ready", "éééé", width=7,
                color=False, unicode=True,
            ),
            "● éééé",
        )
        self.assertEqual(
            self.module.present_status(
                "ready", "Recovery ready", width=1,
                color=False, unicode=True,
            ),
            "●",
        )
        self.assertEqual(
            self.module.present_status(
                "ready", "Recovery ready", width=0,
                color=False, unicode=True,
            ),
            "",
        )

    def test_fallback_pane_uses_ansi_not_tmux_style_tokens(self):
        buffer = io.StringIO()
        output = mock.Mock()
        output.isatty.return_value = True
        output.encoding = "utf-8"
        output.write.side_effect = buffer.write
        output.flush.side_effect = buffer.flush
        with mock.patch.object(sys, "stdout", output), mock.patch.dict(
            os.environ, {"TERM": "xterm-256color"}, clear=True
        ), mock.patch.object(
            self.module.shutil, "get_terminal_size", return_value=os.terminal_size((80, 24))
        ):
            self.module.render_watchdog_status(
                "ready", "Recovery ready · v1.0.5", "pane", {}
            )
        rendered = buffer.getvalue()
        self.assertIn("\x1b[36m●\x1b[0m", rendered)
        self.assertNotIn("#[", rendered)

    def test_pane_renderer_retries_after_terminal_write_failure(self):
        output = mock.Mock()
        output.isatty.return_value = True
        output.encoding = "utf-8"
        output.write.side_effect = [OSError("terminal unavailable"), None]

        with mock.patch.object(sys, "stdout", output), mock.patch.dict(
            os.environ, {"TERM": "xterm-256color"}, clear=True
        ), mock.patch.object(
            self.module.shutil, "get_terminal_size",
            return_value=os.terminal_size((80, 24)),
        ):
            failed_frame = self.module.render_watchdog_status(
                "ready", "Recovery ready · v1.0.5", "pane", {}
            )
            rendered_frame = self.module.render_watchdog_status(
                "ready", "Recovery ready · v1.0.5", "pane", {}, failed_frame
            )
            same_frame = self.module.render_watchdog_status(
                "ready", "Recovery ready · v1.0.5", "pane", {}, rendered_frame
            )

        self.assertIsNone(failed_frame)
        self.assertEqual(rendered_frame, same_frame)
        self.assertEqual(output.write.call_count, 2)
        self.assertEqual(output.flush.call_count, 1)

    def test_pane_renderer_deduplicates_final_frame_but_redraws_after_resize(self):
        buffer = io.StringIO()
        output = mock.Mock()
        output.isatty.return_value = True
        output.encoding = "utf-8"
        output.write.side_effect = buffer.write
        output.flush.side_effect = buffer.flush
        sizes = iter(
            [
                os.terminal_size((80, 24)),
                os.terminal_size((80, 24)),
                os.terminal_size((60, 24)),
            ]
        )

        with mock.patch.object(sys, "stdout", output), mock.patch.dict(
            os.environ, {"TERM": "xterm-256color"}, clear=True
        ), mock.patch.object(
            self.module.shutil, "get_terminal_size", side_effect=sizes
        ):
            frame = self.module.render_watchdog_status(
                "ready", "Recovery ready · v1.0.5", "pane", {}
            )
            frame = self.module.render_watchdog_status(
                "ready", "Recovery ready · v1.0.5", "pane", {}, frame
            )
            frame = self.module.render_watchdog_status(
                "ready", "Recovery ready · v1.0.5", "pane", {}, frame
            )

        self.assertEqual(output.write.call_count, 2)
        self.assertEqual(frame[0:2], ("pane", 60))

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

    def test_watchdog_cleans_up_only_its_run_after_lock_blocked(self):
        run_id = "c" * 32
        other_run_id = "d" * 32
        directory = self.create_run(
            run_id,
            session_id="duplicate-session",
            cancel_binding=False,
        )
        other_directory = self.create_run(other_run_id)
        self.module.update_meta(
            run_id,
            main_pane="%blocked",
            tmux_identity="blocked-pane-identity",
            watchdog_pane="%watchdog",
            watchdog_pane_identity="watchdog-pane-identity",
        )
        self.assertTrue(
            self.module.acquire_named_lock("session", "duplicate-session", run_id)[0]
        )
        killed_targets = []

        def target_alive(target, expected_identity=None):
            return (target, expected_identity) in {
                ("%blocked", "blocked-pane-identity"),
                ("%watchdog", "watchdog-pane-identity"),
            }

        def record_tmux(args, **kwargs):
            if args[0] == "kill-pane":
                killed_targets.append(args[2])
            return Result(stdout="")

        with mock.patch.object(
            self.module, "tmux_target_alive", side_effect=target_alive
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=record_tmux
        ), mock.patch.object(
            self.module, "render_watchdog_status"
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
            self.assertTrue((directory / "ready").exists())

            self.module.send_run_event(
                run_id,
                self.module.event_record("lock_blocked", other_run_id),
            )
            time.sleep(0.35)
            self.assertTrue(thread.is_alive())
            self.assertEqual(
                self.module.read_json(directory / "status.json", {}).get("state"),
                "ready",
            )

            self.module.send_run_event(
                run_id,
                self.module.event_record("lock_blocked", run_id),
            )
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(killed_targets, ["%blocked", "%watchdog"])
        self.assertFalse(directory.exists())
        self.assertFalse(self.module.lock_name("session", "duplicate-session").exists())
        self.assertTrue(other_directory.exists())

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
        self.assertEqual(len(generic), 1)
        self.assertEqual(generic[0]["kind"], "unsupported_failure")
        self.assertNotIn("category", generic[0])
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

    def test_unsupported_main_failure_is_reported_to_end_active_recovery(self):
        run_id = "2" * 32
        self.create_run(run_id)
        events = self.run_hook(
            {
                "hook_event_name": "StopFailure",
                "session_id": "session-1",
                "prompt_id": "unsupported",
                "error_details": "API Error: 400 invalid request",
            },
            run_id,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "unsupported_failure")
        self.assertFalse(events[0]["subagent"])

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

    def test_terminal_timeout_without_stopfailure_triggers_recovery(self):
        run_id = "8" * 32
        directory = self.create_run(
            run_id,
            main_pane="%42",
            tmux_identity="pane-42",
            cancel_binding=False,
            session_id="session-terminal-timeout",
        )
        alive = {"value": True}
        pane = {"text": "Ready"}
        calls = []

        def tmux_run(arguments, **_):
            if arguments[0] == "capture-pane":
                return Result(stdout=pane["text"])
            calls.append(arguments)
            return Result()

        with mock.patch.dict(
            self.module.ERROR_POLICIES,
            {"timeout": {"delays": (0, 0, 0), "label": "请求超时"}},
            clear=False,
        ), mock.patch.object(
            self.module,
            "tmux_target_alive",
            side_effect=lambda *_: alive["value"],
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
        ), mock.patch.object(
            self.module, "PASTE_SETTLE_DELAY", 0
        ), mock.patch.object(
            self.module, "render_watchdog_status"
        ), mock.patch.object(
            self.module, "release_tmux_binding"
        ), mock.patch.object(
            self.module, "cleanup_run"
        ):
            thread = threading.Thread(
                target=self.module.watchdog_main,
                args=(run_id, "hidden"),
                daemon=True,
            )
            thread.start()
            try:
                deadline = time.time() + 2
                while not (directory / "ready").exists() and time.time() < deadline:
                    time.sleep(0.01)
                self.assertTrue((directory / "ready").exists())

                pane["text"] = "⏺ API Error: The operation timed out."
                deadline = time.time() + 2
                while (
                    ["send-keys", "-t", "%42", "Enter"] not in calls
                    and time.time() < deadline
                ):
                    time.sleep(0.01)

                self.assertIn(["paste-buffer", "-b", "claude-auto-888888888888", "-d", "-t", "%42"], calls)
                self.assertIn(["send-keys", "-t", "%42", "Enter"], calls)
                self.assertEqual(
                    sum(call == ["send-keys", "-t", "%42", "Enter"] for call in calls),
                    1,
                )
            finally:
                alive["value"] = False
                thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_multiline_terminal_overload_triggers_recovery(self):
        run_id = "b" * 32
        directory = self.create_run(
            run_id,
            main_pane="%44",
            tmux_identity="pane-44",
            cancel_binding=False,
            session_id="session-terminal-overload",
        )
        alive = {"value": True}
        pane = {"text": "Ready"}
        calls = []

        def tmux_run(arguments, **_):
            if arguments[0] == "capture-pane":
                return Result(stdout=pane["text"])
            calls.append(arguments)
            return Result()

        with mock.patch.dict(
            self.module.ERROR_POLICIES,
            {"overloaded": {"delays": (0, 0, 0), "label": "服务过载"}},
            clear=False,
        ), mock.patch.object(
            self.module,
            "tmux_target_alive",
            side_effect=lambda *_: alive["value"],
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
        ), mock.patch.object(
            self.module, "TERMINAL_FAILURE_GRACE", 0
        ), mock.patch.object(
            self.module, "TERMINAL_POLL_INTERVAL", 0.01
        ), mock.patch.object(
            self.module, "PASTE_SETTLE_DELAY", 0
        ), mock.patch.object(
            self.module, "render_watchdog_status"
        ), mock.patch.object(
            self.module, "release_tmux_binding"
        ), mock.patch.object(
            self.module, "cleanup_run"
        ):
            thread = threading.Thread(
                target=self.module.watchdog_main,
                args=(run_id, "hidden"),
                daemon=True,
            )
            thread.start()
            try:
                deadline = time.time() + 2
                while not (directory / "ready").exists() and time.time() < deadline:
                    time.sleep(0.01)
                self.assertTrue((directory / "ready").exists())

                pane["text"] = (
                    "⏺ API Error: 422 format conversion error: Responses upstream "
                    "service_unavailable_error:\n"
                    "Our servers are currently overloaded. Please try again later."
                )
                submit = ["send-keys", "-t", "%44", "Enter"]
                deadline = time.time() + 2
                while submit not in calls and time.time() < deadline:
                    time.sleep(0.01)

                self.assertIn(
                    [
                        "paste-buffer", "-b", "claude-auto-bbbbbbbbbbbb",
                        "-d", "-t", "%44",
                    ],
                    calls,
                )
                self.assertEqual(calls.count(submit), 1)
                buffers = [call for call in calls if call[0] == "set-buffer"]
                self.assertEqual(len(buffers), 1)
                self.assertIn("服务过载", buffers[0][-1])
                self.assertTrue(thread.is_alive())
            finally:
                alive["value"] = False
                thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_terminal_fallback_ignores_subagent_failure_and_deduplicates_late_hook(self):
        run_id = "a" * 32
        directory = self.create_run(
            run_id,
            main_pane="%43",
            tmux_identity="pane-43",
            cancel_binding=False,
            session_id="session-terminal-timeout",
        )
        alive = {"value": True}
        pane = {"text": "Ready"}
        injections = []

        def tmux_run(arguments, **_):
            if arguments[0] == "capture-pane":
                return Result(stdout=pane["text"])
            return Result()

        def inject(*_):
            injections.append(True)
            return time.monotonic()

        with mock.patch.dict(
            self.module.ERROR_POLICIES,
            {"timeout": {"delays": (0, 0, 0), "label": "请求超时"}},
            clear=False,
        ), mock.patch.object(
            self.module, "TERMINAL_FAILURE_GRACE", 0.15
        ), mock.patch.object(
            self.module, "TERMINAL_POLL_INTERVAL", 0.01
        ), mock.patch.object(
            self.module,
            "tmux_target_alive",
            side_effect=lambda *_: alive["value"],
        ), mock.patch.object(
            self.module, "tmux_run", side_effect=tmux_run
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
                args=(run_id, "hidden"),
                daemon=True,
            )
            thread.start()
            try:
                deadline = time.time() + 2
                while not (directory / "ready").exists() and time.time() < deadline:
                    time.sleep(0.01)
                pane["text"] = "⏺ API Error: The operation timed out."
                time.sleep(0.05)
                self.module.send_run_event(
                    run_id,
                    {
                        "kind": "recoverable_failure",
                        "at": time.time(),
                        "run_id": run_id,
                        "session_id": "session-terminal-timeout",
                        "prompt_id": "subagent",
                        "category": "timeout",
                        "subagent": True,
                    },
                )
                deadline = time.time() + 2
                while not injections and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(injections), 1)
                self.module.send_run_event(
                    run_id,
                    {
                        "kind": "prompt_submit",
                        "at": time.time(),
                        "run_id": run_id,
                        "session_id": "session-terminal-timeout",
                        "recovery": True,
                    },
                )
                pane["text"] = "recovery prompt active"
                time.sleep(0.05)
                self.module.send_run_event(
                    run_id,
                    {
                        "kind": "recoverable_failure",
                        "at": time.time(),
                        "run_id": run_id,
                        "session_id": "session-terminal-timeout",
                        "prompt_id": "late-hook",
                        "category": "timeout",
                        "subagent": False,
                    },
                )
                time.sleep(0.25)
                self.assertEqual(len(injections), 1)
                self.assertEqual(
                    self.module.read_json(directory / "status.json", {}).get("retry_count"),
                    1,
                )
            finally:
                alive["value"] = False
                thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_repeated_terminal_timeouts_have_distinct_observations(self):
        error = "⏺ API Error: The operation timed out."

        def observe(position, output):
            def tmux_run(arguments, **_):
                if arguments[0] == "display-message":
                    return Result(stdout=position)
                return Result(stdout=output)

            with mock.patch.object(self.module, "tmux_run", side_effect=tmux_run):
                return self.module.terminal_failure_observation(
                    {"main_pane": "%42"}
                )

        first = observe("10:20:0", "old content\n" + error)
        changed_history = observe("10:20:0", "different content\n" + error)
        repeated = observe("11:21:0", "different content\n" + error)
        self.assertEqual(first[0], "timeout")
        self.assertEqual(changed_history[0], "timeout")
        self.assertEqual(repeated[0], "timeout")
        self.assertEqual(first[1], changed_history[1])
        self.assertNotEqual(changed_history[1], repeated[1])

    def test_terminal_failure_observation_accepts_multiline_current_error(self):
        text = (
            "old terminal output\n"
            "⏺ API Error: 422 format conversion error: Responses upstream "
            "service_unavailable_error:\n"
            "Our servers are currently overloaded. Please try again later."
        )
        with mock.patch.object(
            self.module,
            "tmux_run",
            return_value=Result(stdout=text),
        ):
            observation = self.module.terminal_failure_observation(
                {"main_pane": "%42"}
            )
        self.assertEqual(observation[0], "overloaded")
        self.assertEqual(len(observation[1]), 64)

    def test_terminal_failure_observation_joins_wrapped_pane_lines(self):
        calls = []

        def tmux_run(arguments, **_):
            calls.append(arguments)
            if arguments[0] == "display-message":
                return Result(stdout="10:20:0")
            return Result(stdout="⏺ API Error: The operation timed out.")

        with mock.patch.object(self.module, "tmux_run", side_effect=tmux_run):
            observation = self.module.terminal_failure_observation(
                {"main_pane": "%42"}
            )

        capture = next(call for call in calls if call[0] == "capture-pane")
        self.assertIn("-J", capture)
        self.assertEqual(observation[0], "timeout")

    def test_terminal_failure_observation_requires_decorated_current_status(self):
        rejected = (
            "请解决刚刚发生 ⏺ API Error: The operation timed out. 但是没有成功触发的问题",
            "API Error: The operation timed out.",
            "⏺ API Error: The operation timed out.\nordinary output",
            (
                "⏺ API Error: The operation timed out.\n"
                "The operation timed out earlier; continuing requested work."
            ),
            (
                "⏺ API Error: The operation timed out.\n"
                "ordinary discussion\n"
                "The servers are currently overloaded; what should I do?"
            ),
            (
                "⏺ API Error: Responses upstream service_unavailable_error:\n"
                "ordinary output\n"
                "Our servers are currently overloaded. Please try again later."
            ),
            (
                "⏺ API Error: Responses upstream service_unavailable_error:\n"
                "Our servers are currently overloaded. Please try again later.\n"
                "ordinary output"
            ),
        )
        for text in rejected:
            with self.subTest(text=text), mock.patch.object(
                self.module,
                "tmux_run",
                return_value=Result(stdout=text),
            ):
                self.assertIsNone(
                    self.module.terminal_failure_observation({"main_pane": "%42"})
                )

        with mock.patch.object(
            self.module,
            "tmux_run",
            return_value=Result(stdout="ordinary output\n⏺ API Error: The operation timed out."),
        ):
            observation = self.module.terminal_failure_observation(
                {"main_pane": "%42"}
            )
        self.assertEqual(observation[0], "timeout")
        self.assertEqual(len(observation[1]), 64)

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

    def test_skip_command_only_accepts_pending_countdown(self):
        run_id = "6" * 32
        directory = self.create_run(run_id, name="demo")
        self.module.write_status(run_id, "countdown", retry_count=1)
        output = io.StringIO()
        with mock.patch.object(sys, "stdout", output):
            self.assertEqual(self.module.management_main(["skip", "demo"]), 0)
        self.assertEqual(output.getvalue(), "Skipped pending recovery for demo.\n")
        self.assertTrue((directory / "cancel").exists())

        (directory / "cancel").unlink()
        self.module.write_status(run_id, "submitting", retry_count=1)
        output = io.StringIO()
        with mock.patch.object(sys, "stderr", output):
            self.assertEqual(self.module.management_main(["skip", "demo"]), 1)
        self.assertIn("already submitting", output.getvalue())
        self.assertFalse((directory / "cancel").exists())

    def test_headless_skip_stops_pending_recovery_countdown(self):
        run_id = "9" * 32
        attempts = []
        result = {}
        lifecycle = mock.Mock()
        failure = {
            "kind": "recoverable_failure",
            "at": time.time(),
            "run_id": run_id,
            "session_id": "00000000-0000-4000-8000-000000000009",
            "prompt_id": "prompt-1",
            "category": "timeout",
            "subagent": False,
        }

        def run_attempt(*_):
            attempts.append(True)
            if len(attempts) == 1:
                return 1, [failure], None, None
            return 0, [], None, None

        with mock.patch.object(
            self.module, "add_managed_session_id",
            return_value=(
                ["-p"],
                "00000000-0000-4000-8000-000000000009",
                False,
            ),
        ), mock.patch.object(
            self.module.uuid, "uuid4", return_value=mock.Mock(hex=run_id)
        ), mock.patch.object(
            self.module, "run_headless_attempt", side_effect=run_attempt
        ), mock.patch.dict(
            self.module.ERROR_POLICIES,
            {"timeout": {"delays": (10, 10, 10), "label": "请求超时"}},
            clear=False,
        ):
            thread = threading.Thread(
                target=lambda: result.update(
                    code=self.module.headless_main(["-p"], lifecycle=lifecycle)
                ),
                daemon=True,
            )
            thread.start()
            directory = self.module.run_dir(run_id)
            deadline = time.time() + 2
            while (
                self.module.read_json(directory / "status.json", {}).get("state")
                != "countdown"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(self.module.skip_run("headless-" + run_id[:8]), 0)
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result["code"], 1)
        self.assertEqual(len(attempts), 1)

    def test_client_interrupt_stops_pending_submit_retries(self):
        run_id = "2" * 32
        directory = self.create_run(
            run_id,
            main_pane="%18",
            tmux_identity="pane-18",
            cancel_binding=False,
        )
        alive = {"value": True}
        retry_keys = []
        prompt = self.module.recovery_message(1, "timeout")

        def inject(*_):
            self.module.mark_expected_recovery(run_id, prompt)
            return time.monotonic()

        def tmux_run(arguments, **_):
            if arguments[0] == "attach-session":
                raise KeyboardInterrupt
            if arguments[0] == "send-keys" and arguments[-1] == "Enter":
                retry_keys.append(arguments)
            return Result()

        with mock.patch.dict(
            self.module.ERROR_POLICIES,
            {"timeout": {"delays": (0.01, 0.01, 0.01), "label": "请求超时"}},
            clear=False,
        ), mock.patch.object(
            self.module, "SUBMIT_RETRY_DELAY", 0.2
        ), mock.patch.object(
            self.module, "SUBMISSION_ACK_TIMEOUT", 0.4
        ), mock.patch.object(
            self.module, "tmux_target_alive",
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
            self.module.send_run_event(run_id, {
                "kind": "recoverable_failure",
                "at": time.time(),
                "run_id": run_id,
                "session_id": "session-1",
                "prompt_id": "prompt-1",
                "category": "timeout",
                "subagent": False,
            })
            deadline = time.time() + 2
            while not self.module.expected_recovery_path(run_id).exists() and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(
                self.module.attach_managed_session(run_id, "managed"), 130
            )
            deadline = time.time() + 2
            while (
                self.module.read_json(directory / "status.json", {}).get("state")
                != "cancelled"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            time.sleep(0.5)
            self.assertEqual(retry_keys, [])
            self.assertFalse((directory / "cancel").exists())
            alive["value"] = False
            thread.join(2)

        self.assertFalse(thread.is_alive())

    def test_client_interrupt_without_pending_recovery_leaves_no_cancel_marker(self):
        run_id = "a" * 32
        self.create_run(run_id)
        self.module.write_status(run_id, "ready", retry_count=0)
        with mock.patch.object(self.module, "tmux_run", side_effect=KeyboardInterrupt):
            self.assertEqual(self.module.attach_managed_session(run_id, "session-1"), 130)
        self.assertFalse((self.module.run_dir(run_id) / "cancel").exists())

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
        ) as cleanup_run:
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
            self.assertTrue(thread.is_alive())
            cleanup_run.assert_not_called()
            self.module.send_run_event(
                run_id,
                {
                    "kind": "prompt_submit",
                    "at": time.time(),
                    "run_id": run_id,
                    "session_id": "session-1",
                    "recovery": False,
                },
            )
            deadline = time.time() + 2
            while (
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state")
                != "ready"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state"),
                "ready",
            )
            self.assertTrue(thread.is_alive())
            cleanup_run.assert_not_called()
            self.module.send_run_event(
                run_id,
                {
                    "kind": "unsupported_failure",
                    "at": time.time(),
                    "run_id": run_id,
                    "session_id": "session-1",
                    "prompt_id": "unsupported",
                    "subagent": False,
                },
            )
            deadline = time.time() + 2
            while (
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state")
                != "unsupported"
                and time.time() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(
                self.module.read_json(
                    directory / "status.json", {}
                ).get("state"),
                "unsupported",
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
            while not self.module.expected_recovery_path(run_id).exists() and time.time() < deadline:
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
            self.module, "send_recovery_submit_key", return_value="sent"
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

    def test_skip_shortcut_cannot_cancel_acknowledged_recovery(self):
        run_id = "b" * 32
        directory = self.create_run(
            run_id,
            main_pane="%14",
            tmux_session="managed",
            window_id="@14",
            cancel_binding=True,
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
            while not self.module.expected_recovery_path(run_id).exists() and time.time() < deadline:
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
            self.assertEqual(
                self.module.cancel_target("managed", "@14"), 1
            )
            self.assertFalse((directory / "cancel").exists())
            self.module.send_run_event(
                run_id,
                dict(failure, prompt_id="prompt-2", at=time.time()),
            )
            deadline = time.time() + 2
            while len(injections) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(injections), 2)
            self.assertIn(
                self.module.read_json(directory / "status.json", {}).get("state"),
                {"submitting", "awaiting"},
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
            self.module, "RESULT_FEEDBACK_SECONDS", 0.1
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
            deadline = time.time() + 2
            while self.module.read_json(directory / "status.json", {}).get("state") != "skipped" and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(
                self.module.read_json(directory / "status.json", {}).get("state"),
                "skipped",
            )
            self.assertEqual(sent, [])
            deadline = time.time() + 2
            while self.module.read_json(directory / "status.json", {}).get("state") != "ready" and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(
                self.module.read_json(directory / "status.json", {}).get("state"),
                "ready",
            )
            alive["value"] = False
            thread.join(2)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
