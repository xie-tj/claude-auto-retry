import json
import os
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="ca-",
            dir="/tmp",
        )
        self.root = Path(self.temporary.name)
        self.home = self.root / "home with space"
        self.fake_bin = self.root / "bin"
        self.home.mkdir()
        self.fake_bin.mkdir()
        self.raw_claude = self.fake_bin / "claude-real"
        self.raw_version = self.fake_bin / "claude-version"
        self.tmux = self.fake_bin / "tmux"
        self.raw_version.write_text("#!/bin/sh\nprintf 'test Claude Code\\n'\n", encoding="utf-8")
        self.raw_version.chmod(0o755)
        self.raw_claude.symlink_to(self.raw_version)
        self.tmux.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.raw_claude.chmod(0o755)
        self.tmux.chmod(0o755)
        settings = {
            "model": "sonnet",
            "env": {"EXISTING_SECRET": "preserve-me"},
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "existing-stop-hook"}]}
                ]
            },
        }
        settings_path = self.home / ".claude" / "settings.json"
        settings_path.parent.mkdir()
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        (self.home / ".zshrc").write_text("export EXISTING=1\n", encoding="utf-8")
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("CLAUDE_AUTO_")
        }
        self.env.update(
            {
                "HOME": str(self.home),
                "SHELL": "/bin/zsh",
                "CLAUDE_AUTO_RAW_CLAUDE": str(self.raw_claude),
                "CLAUDE_AUTO_TMUX": str(self.tmux),
                "CLAUDE_AUTO_STATE_DIR": str(self.home / ".local" / "state" / "claude-auto"),
                "CLAUDE_AUTO_IPC_DIR": str(self.root / "ipc"),
                "CLAUDE_AUTO_PYTHON": sys.executable,
            }
        )
        self.marker = self.root / "unexpected-shell-execution"

    def tearDown(self):
        self.temporary.cleanup()

    def install(self):
        return subprocess.run(
            ["bash", str(INSTALLER)],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def settings(self):
        return json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def test_install_is_idempotent_and_preserves_settings(self):
        first = self.install()
        second = self.install()
        self.assertIn("installed successfully", first.stdout)
        self.assertIn("installed successfully", second.stdout)
        settings = self.settings()
        self.assertEqual(settings["model"], "sonnet")
        self.assertEqual(settings["env"]["EXISTING_SECRET"], "preserve-me")
        self.assertEqual(
            settings["hooks"]["Stop"][0]["hooks"][0]["command"],
            "existing-stop-hook",
        )
        source = str(self.home / ".local" / "share" / "claude-auto" / "claude_auto.py")
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "StopFailure", "SessionEnd"):
            matches = [
                hook
                for entry in settings["hooks"][event]
                for hook in entry.get("hooks", [])
                if source in hook.get("command", "")
                or "claude_auto.py" in hook.get("command", "")
            ]
            self.assertEqual(len(matches), 1, event)
        hook_command = next(
            hook["command"]
            for entry in settings["hooks"]["StopFailure"]
            for hook in entry.get("hooks", [])
            if "claude_auto.py" in hook.get("command", "")
        )
        hook_result = subprocess.run(
            hook_command,
            shell=True,
            env=self.env,
            text=True,
            input=json.dumps(
                {
                    "hook_event_name": "StopFailure",
                    "session_id": "installer-space-test",
                    "error_details": "API Error: 422 invalid request",
                }
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(hook_result.returncode, 0, hook_result.stderr)
        zshrc = (self.home / ".zshrc").read_text(encoding="utf-8")
        self.assertEqual(zshrc.count("# >>> claude-auto >>>"), 1)
        self.assertIn("export EXISTING=1", zshrc)
        config = json.loads(
            (self.home / ".config" / "claude-auto" / "config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(config["raw_claude_path"], str(self.raw_claude.absolute()))
        self.assertEqual(config["tmux_path"], str(self.tmux.resolve()))
        self.assertEqual(config["python_path"], str(Path(sys.executable).resolve()))
        self.assertEqual(config["ipc_dir"], str(self.root / "ipc"))
        shim = (self.home / ".local" / "claude-auto" / "bin" / "claude-auto").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            f"export CLAUDE_AUTO_CONFIG_DIR={shlex.quote(str(self.home / '.config' / 'claude-auto'))}",
            shim,
        )
        self.assertIn(
            f"export CLAUDE_AUTO_STATE_DIR={shlex.quote(str(self.home / '.local' / 'state' / 'claude-auto'))}",
            shim,
        )
        self.assertEqual(
            stat.S_IMODE((self.home / ".config" / "claude-auto" / "config.json").stat().st_mode),
            0o600,
        )
        ipc_dir = self.root / "ipc"
        ipc_dir.rmdir()
        doctor_env = self.env.copy()
        doctor_env.pop("CLAUDE_AUTO_IPC_DIR")
        doctor = subprocess.run(
            [str(self.home / ".local" / "claude-auto" / "bin" / "claude-auto"), "doctor"],
            env=doctor_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIn(doctor.returncode, (0, 1), doctor.stderr)
        self.assertIn("claude-auto 1.0.0", doctor.stdout)
        self.assertTrue(ipc_dir.is_dir())
        self.assertEqual(stat.S_IMODE(ipc_dir.stat().st_mode), 0o700)

    def test_reinstall_uses_saved_raw_claude_when_path_points_to_shim(self):
        self.install()
        first_config = json.loads(
            (self.home / ".config" / "claude-auto" / "config.json").read_text(encoding="utf-8")
        )
        second_env = self.env.copy()
        second_env.pop("CLAUDE_AUTO_RAW_CLAUDE")
        second_env.pop("CLAUDE_AUTO_IPC_DIR")
        second_env["PATH"] = (
            str(self.home / ".local" / "claude-auto" / "bin")
            + os.pathsep
            + second_env.get("PATH", "")
        )
        result = subprocess.run(
            ["bash", str(INSTALLER)],
            cwd=ROOT,
            env=second_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        self.assertIn("installed successfully", result.stdout)
        config = json.loads(
            (self.home / ".config" / "claude-auto" / "config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(config["raw_claude_path"], str(self.raw_claude.absolute()))
        self.assertEqual(config["ipc_dir"], first_config["ipc_dir"])

    def test_shell_paths_are_quoted_and_custom_bin_uninstalls(self):
        custom_bin = self.root / f"bin $(touch {self.marker})"
        custom_app = self.root / f"app $(touch {self.marker})"
        parent_mode = stat.S_IMODE(self.root.stat().st_mode)
        environment = self.env.copy()
        environment["CLAUDE_AUTO_BIN_DIR"] = str(custom_bin)
        environment["CLAUDE_AUTO_APP_DIR"] = str(custom_app)
        subprocess.run(
            ["bash", str(INSTALLER)],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        subprocess.run(
            [str(custom_bin / "claude-auto"), "self-test"],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertFalse(self.marker.exists())
        environment.pop("CLAUDE_AUTO_BIN_DIR")
        environment.pop("CLAUDE_AUTO_IPC_DIR")
        environment["PATH"] = str(custom_bin) + os.pathsep + environment.get("PATH", "")
        reinstall = subprocess.run(
            ["bash", str(INSTALLER)],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        self.assertIn("installed successfully", reinstall.stdout)
        config = json.loads(
            (self.home / ".config" / "claude-auto" / "config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(config["bin_dir"], str(custom_bin))
        self.assertFalse((self.home / ".local" / "claude-auto" / "bin").exists())
        subprocess.run(
            [str(custom_bin / "claude-auto"), "uninstall"],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        self.assertFalse(custom_app.exists())
        self.assertFalse(custom_bin.exists())
        self.assertFalse(self.marker.exists())
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), parent_mode)

    def test_install_refuses_unsafe_or_unowned_managed_directory(self):
        unsafe = self.env.copy()
        unsafe["CLAUDE_AUTO_STATE_DIR"] = str(self.home)
        result = subprocess.run(
            ["bash", str(INSTALLER)],
            cwd=ROOT,
            env=unsafe,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing unsafe", result.stderr)
        unowned = self.root / "unowned"
        unowned.mkdir()
        sentinel = unowned / "keep-me"
        sentinel.write_text("preserve", encoding="utf-8")
        unsafe["CLAUDE_AUTO_STATE_DIR"] = str(unowned)
        result = subprocess.run(
            ["bash", str(INSTALLER)],
            cwd=ROOT,
            env=unsafe,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(sentinel.exists())
        self.assertIn("unowned", result.stderr)

    def test_uninstall_preserves_unrelated_configuration(self):
        self.install()
        source_hook_text = str(self.home / ".local" / "share" / "claude-auto" / "claude_auto.py")
        settings = self.settings()
        settings["hooks"]["Stop"].append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "notify --about " + source_hook_text,
                    }
                ]
            }
        )
        (self.home / ".claude" / "settings.json").write_text(
            json.dumps(settings), encoding="utf-8"
        )
        command = self.home / ".local" / "claude-auto" / "bin" / "claude-auto"
        result = subprocess.run(
            [str(command), "uninstall"],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        self.assertIn("Homebrew and tmux were kept", result.stdout)
        settings = self.settings()
        self.assertEqual(settings["model"], "sonnet")
        self.assertEqual(settings["env"]["EXISTING_SECRET"], "preserve-me")
        self.assertEqual(
            settings["hooks"]["Stop"],
            [
                {"hooks": [{"type": "command", "command": "existing-stop-hook"}]},
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "notify --about " + source_hook_text,
                        }
                    ]
                },
            ],
        )
        self.assertNotIn("SessionStart", settings["hooks"])
        zshrc = (self.home / ".zshrc").read_text(encoding="utf-8")
        self.assertIn("export EXISTING=1", zshrc)
        self.assertNotIn("claude-auto", zshrc)
        self.assertFalse((self.home / ".local" / "share" / "claude-auto").exists())
        self.assertFalse((self.home / ".config" / "claude-auto").exists())
        self.assertTrue(self.raw_claude.exists())
        self.assertTrue(self.tmux.exists())

    def test_install_refuses_malformed_settings(self):
        settings_path = self.home / ".claude" / "settings.json"
        settings_path.write_text("{", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(INSTALLER)],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(settings_path.read_text(encoding="utf-8"), "{")
        self.assertIn("refusing installation", result.stderr)


if __name__ == "__main__":
    unittest.main()
