"""Real tmux target-selection checks using an isolated named server.

Run with ``python3 -m unittest tests.test_tmux_isolated -v``.  The test never
uses the default tmux socket: every invocation includes its private ``-L`` name.
"""

import fcntl
import importlib.util
import os
import pty
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "src" / "claude_auto.py"


class IsolatedTmuxTargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmux = shutil.which("tmux")
        if not cls.tmux:
            raise unittest.SkipTest("tmux is not installed")
        cls.socket = "claude_auto_test_{}_{}".format(
            uuid.uuid4().hex[:12], uuid.uuid4().hex[:12]
        )

    @classmethod
    def tearDownClass(cls):
        if cls.tmux:
            subprocess.run(
                [cls.tmux, "-L", cls.socket, "kill-server"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    @classmethod
    def tmux_run(cls, *args, check=True):
        return subprocess.run(
            [cls.tmux, "-L", cls.socket] + list(args),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
            timeout=10,
        )

    def test_real_untagged_unrelated_window_does_not_poison_owner_enumeration(self):
        host = "ordinary-host-{}".format(uuid.uuid4().hex[:10])
        owner_tag = uuid.uuid4().hex + uuid.uuid4().hex
        owner_window = "claude-auto-" + owner_tag[:48]
        self.tmux_run("new-session", "-d", "-s", host, "-n", "ordinary", "sleep 30")
        self.tmux_run(
            "new-window", "-d", "-t", host + ":", "-n", owner_window, "sleep 30"
        )
        exact_link = "={}:={}".format(host, owner_window)
        self.tmux_run(
            "set-option", "-w", "-t", exact_link,
            "@claude_auto_owner_tag", owner_tag,
        )
        stable = self.tmux_run(
            "display-message", "-p", "-t", exact_link,
            "#{session_id}\t#{window_id}",
        ).stdout.strip().split("\t")
        self.assertEqual(len(stable), 2)

        spec = importlib.util.spec_from_file_location(
            "claude_auto_isolated_tmux_test", SOURCE
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def app_tmux(arguments, **_):
            return self.tmux_run(*arguments, check=False)

        meta = {
            "owned_target": "window",
            "tmux_session_id": stable[0],
            "tmux_host_session_name": host,
            "window_id": stable[1],
            "tmux_window_name": owner_window,
            "tmux_owner_tag": owner_tag,
            "tmux_server_epoch": "isolated-test-epoch",
            "owned_window_link": exact_link,
        }
        # The ordinary initial window has no user option, so its line ends in
        # an empty owner-tag field.  The app must accept that unrelated record
        # while still identifying our complete stable-ID/tag candidate.
        with mock.patch.object(module, "tmux_run", side_effect=app_tmux), mock.patch.object(
            module, "tmux_server_epoch_matches", return_value=True
        ):
            self.assertTrue(module.owned_tmux_target_presence(meta))
            self.tmux_run(
                "set-option", "-u", "-w", "-t", exact_link,
                "@claude_auto_owner_tag",
            )
            self.assertIsNone(module.owned_tmux_target_presence(meta))

    def test_exact_owner_targets_fail_closed_after_disappearance(self):
        owner_tag = uuid.uuid4().hex + uuid.uuid4().hex
        owner_window = "claude-auto-" + owner_tag[:48]
        owner_session = "ca-session-" + owner_tag[:48]

        self.tmux_run("new-session", "-d", "-s", "host", "-n", "host-window", "sleep 30")
        self.tmux_run(
            "new-window", "-d", "-t", "host:", "-n", owner_window, "sleep 30"
        )
        exact_link = "=host:={}".format(owner_window)
        self.tmux_run(
            "set-option", "-w", "-t", exact_link,
            "@claude_auto_owner_tag", owner_tag,
        )
        listing = self.tmux_run(
            "list-windows", "-a", "-F",
            "#{session_name}\t#{window_name}\t#{@claude_auto_owner_tag}",
        ).stdout.splitlines()
        self.assertIn("host\t{}\t{}".format(owner_window, owner_tag), listing)

        # Model the target disappearing after enumeration but before teardown.
        # ``-k`` only removes the recorded host link (and destroys the window
        # only if it has no other links); it cannot select host-window instead.
        self.tmux_run("unlink-window", "-k", "-t", exact_link)
        host_windows = self.tmux_run(
            "list-windows", "-a", "-F", "#{session_name}\t#{window_name}"
        ).stdout.splitlines()
        self.assertEqual(host_windows, ["host\thost-window"])
        disappeared = self.tmux_run("unlink-window", "-k", "-t", exact_link, check=False)
        self.assertNotEqual(disappeared.returncode, 0)
        self.assertEqual(
            self.tmux_run("list-windows", "-a", "-F", "#{session_name}\t#{window_name}").stdout.splitlines(),
            ["host\thost-window"],
        )

        self.tmux_run(
            "new-session", "-d", "-s", owner_session,
            "-n", owner_window, "sleep 30",
        )
        self.tmux_run(
            "set-option", "-t", owner_session,
            "@claude_auto_owner_tag", owner_tag,
        )
        self.tmux_run(
            "set-option", "-w", "-t", "={}:={}".format(owner_session, owner_window),
            "@claude_auto_owner_tag", owner_tag,
        )
        sessions = self.tmux_run(
            "list-sessions", "-F", "#{session_name}\t#{@claude_auto_owner_tag}"
        ).stdout.splitlines()
        self.assertIn("{}\t{}".format(owner_session, owner_tag), sessions)
        self.tmux_run("kill-session", "-t", "=" + owner_session)
        disappeared = self.tmux_run(
            "kill-session", "-t", "=" + owner_session, check=False
        )
        self.assertNotEqual(disappeared.returncode, 0)
        self.assertEqual(
            self.tmux_run("list-sessions", "-F", "#{session_name}").stdout.splitlines(),
            ["host"],
        )


class IsolatedTmuxLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cae-", dir="/tmp")
        self.root = Path(self.temporary.name)
        self.tmux = shutil.which("tmux")
        if not self.tmux:
            self.temporary.cleanup()
            raise unittest.SkipTest("tmux is not installed")
        self.socket = "cae_{}".format(uuid.uuid4().hex[:16])
        self.tmux_wrapper = self.root / "tmux"
        self.raw_claude = self.root / "claude"
        self.tmux_wrapper.write_text(
            '#!/bin/sh\nexec "{}" -L "{}" "$@"\n'.format(
                self.tmux, self.socket
            )
        )
        self.raw_claude.write_text("#!/bin/sh\nsleep 0.2\nexit 0\n")
        self.tmux_wrapper.chmod(0o700)
        self.raw_claude.chmod(0o700)
        self.environment = os.environ.copy()
        self.environment.pop("TMUX", None)
        self.environment.pop("TMUX_PANE", None)
        self.environment.update(
            {
                "HOME": str(self.root / "home"),
                "CLAUDE_AUTO_APP_DIR": str(self.root / "app"),
                "CLAUDE_AUTO_CONFIG_DIR": str(self.root / "config"),
                "CLAUDE_AUTO_STATE_DIR": str(self.root / "state"),
                "CLAUDE_AUTO_IPC_DIR": str(self.root / "ipc"),
                "CLAUDE_AUTO_SETTINGS_PATH": str(self.root / "settings.json"),
                "CLAUDE_AUTO_RAW_CLAUDE": str(self.raw_claude),
                "CLAUDE_AUTO_TMUX": str(self.tmux_wrapper),
                "CLAUDE_AUTO_PYTHON": sys.executable,
            }
        )
        Path(self.environment["HOME"]).mkdir()

    def tearDown(self):
        if self.tmux:
            self.tmux_run("kill-server", check=False)
        self.temporary.cleanup()

    def tmux_run(self, *args, check=True):
        return subprocess.run(
            [self.tmux, "-L", self.socket] + list(args),
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
            timeout=10,
        )

    def start_launcher(self, environment=None):
        master, slave = pty.openpty()
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 24, 100, 0, 0),
        )
        process = subprocess.Popen(
            [sys.executable, str(SOURCE), "manage", "new"],
            env=environment or self.environment,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        return process, master

    def finish_launcher(self, process, master, expected_code=0):
        try:
            code = process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            self.fail("managed launcher did not exit")
        time.sleep(0.3)
        output = bytearray()
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        os.close(master)
        self.assertEqual(code, expected_code, output.decode(errors="replace"))

    def assert_no_run_was_recreated(self):
        runs = self.root / "state" / "runs"
        self.assertEqual(list(runs.iterdir()) if runs.exists() else [], [])

    def test_root_exit_closes_the_final_managed_session(self):
        self.raw_claude.write_text("#!/bin/sh\nsleep 0.2\nexit 7\n")
        process, master = self.start_launcher()
        self.finish_launcher(process, master, expected_code=7)

        listing = self.tmux_run("list-sessions", check=False)
        self.assertNotEqual(listing.returncode, 0)
        self.assert_no_run_was_recreated()

    def test_root_exit_closes_only_its_session_on_a_shared_server(self):
        self.tmux_run(
            "new-session", "-d", "-s", "unrelated", "-n", "healthy", "sleep", "30"
        )
        process, master = self.start_launcher()
        self.finish_launcher(process, master)

        self.assertEqual(
            self.tmux_run(
                "list-windows", "-a", "-F", "#{session_name}:#{window_name}"
            ).stdout.splitlines(),
            ["unrelated:healthy"],
        )
        self.assert_no_run_was_recreated()

    def test_nested_root_exit_unlinks_only_the_host_link(self):
        self.raw_claude.write_text("#!/bin/sh\nsleep 1\nexit 9\n")
        marker = self.root / "host-environment"
        self.tmux_run(
            "new-session", "-d", "-s", "host", "-n", "base",
            "sh", "-c", "env > {}; sleep 30".format(marker),
        )
        self.tmux_run(
            "new-session", "-d", "-s", "keeper", "-n", "keep", "sleep", "30"
        )
        deadline = time.monotonic() + 3
        marker_text = ""
        while time.monotonic() < deadline:
            marker_text = marker.read_text() if marker.exists() else ""
            if "TMUX=" in marker_text and "TMUX_PANE=" in marker_text:
                break
            time.sleep(0.02)
        self.assertIn("TMUX=", marker_text)
        self.assertIn("TMUX_PANE=", marker_text)
        nested_environment = self.environment.copy()
        for line in marker_text.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                if key in {"TMUX", "TMUX_PANE"}:
                    nested_environment[key] = value
        self.assertIn("TMUX", nested_environment)
        self.assertIn("TMUX_PANE", nested_environment)

        process, master = self.start_launcher(nested_environment)
        owner_window = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            windows = self.tmux_run(
                "list-windows", "-t", "host", "-F", "#{window_name}"
            ).stdout.splitlines()
            owner_window = next(
                (name for name in windows if name.startswith("claude-auto-")), None
            )
            if owner_window:
                break
            time.sleep(0.02)
        self.assertIsNotNone(owner_window)
        exact_owner_link = "=host:={}".format(owner_window)
        deadline = time.monotonic() + 5
        owner_ready = False
        while time.monotonic() < deadline:
            options = self.tmux_run(
                "display-message", "-p", "-t", exact_owner_link,
                "#{@claude_auto_owner_tag}\t#{remain-on-exit}",
                check=False,
            ).stdout.strip().split("\t")
            owner_ready = len(options) == 2 and len(options[0]) == 64 and options[1] == "on"
            if owner_ready:
                break
            time.sleep(0.02)
        self.assertTrue(owner_ready)
        self.tmux_run(
            "link-window", "-s", exact_owner_link, "-t", "keeper:"
        )
        self.finish_launcher(process, master, expected_code=9)

        self.assertEqual(
            self.tmux_run(
                "list-windows", "-t", "host", "-F", "#{window_name}"
            ).stdout.splitlines(),
            ["base"],
        )
        keeper_windows = self.tmux_run(
            "list-windows", "-t", "keeper", "-F", "#{window_name}"
        ).stdout.splitlines()
        self.assertEqual(set(keeper_windows), {"keep", owner_window})
        self.assertEqual(
            set(
                self.tmux_run(
                    "list-sessions", "-F", "#{session_name}"
                ).stdout.splitlines()
            ),
            {"host", "keeper"},
        )
        self.assert_no_run_was_recreated()


if __name__ == "__main__":
    unittest.main()
