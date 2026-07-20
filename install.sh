#!/usr/bin/env bash
set -euo pipefail

REPO="xie-tj/claude-auto-retry"
BRANCH="${CLAUDE_AUTO_BRANCH:-main}"
HOME_DIR="${HOME:?HOME is required}"
APP_DIR="${CLAUDE_AUTO_APP_DIR:-$HOME_DIR/.local/share/claude-auto}"
BIN_DIR="${CLAUDE_AUTO_BIN_DIR:-}"
CONFIG_DIR="${CLAUDE_AUTO_CONFIG_DIR:-$HOME_DIR/.config/claude-auto}"
STATE_DIR="${CLAUDE_AUTO_STATE_DIR:-$HOME_DIR/.local/state/claude-auto}"
IPC_DIR="${CLAUDE_AUTO_IPC_DIR:-}"
SETTINGS_PATH="${CLAUDE_AUTO_SETTINGS_PATH:-$HOME_DIR/.claude/settings.json}"
SOURCE_PATH="$APP_DIR/claude_auto.py"
CONFIG_PATH="$CONFIG_DIR/config.json"
HOOK_COMMAND=""
INSTALL_LOCK_PID=""
INSTALL_LOCK_COORD=""

fail() {
  printf 'claude-auto: %s\n' "$*" >&2
  exit 1
}

find_python() {
  local candidate
  for candidate in "${CLAUDE_AUTO_PYTHON:-}" "$(command -v python3 2>/dev/null || true)" /usr/bin/python3; do
    if [[ -n "$candidate" && -x "$candidate" ]] && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' 2>/dev/null; then
      "$candidate" -c 'import os,sys; print(os.path.realpath(sys.executable))'
      return
    fi
  done
  fail "Python 3.9 or newer is required"
}

existing_config_value() {
  local key="$1"
  [[ -f "$CONFIG_PATH" ]] || return 0
  "$PYTHON" - "$CONFIG_PATH" "$key" <<'PY' 2>/dev/null || true
import json, sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2])
    if isinstance(value, str):
        print(value)
except (OSError, ValueError):
    pass
PY
}

existing_manifest_value() {
  local key="$1" manifest="$CONFIG_DIR/install-manifest.json"
  [[ -f "$manifest" ]] || return 0
  "$PYTHON" - "$manifest" "$key" <<'PY' 2>/dev/null || true
import json, sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2])
    if isinstance(value, str):
        print(value)
except (OSError, ValueError):
    pass
PY
}

absolute_path() {
  "$PYTHON" - "$1" <<'PY'
import os, sys
print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
}

resolve_executable() {
  "$PYTHON" - "$1" <<'PY'
import os, sys
print(os.path.realpath(os.path.expanduser(sys.argv[1])))
PY
}

shell_quote() {
  "$PYTHON" - "$1" <<'PY'
import shlex, sys
print(shlex.quote(sys.argv[1]))
PY
}

validate_install_paths() {
  "$PYTHON" - "$HOME_DIR" "$APP_DIR" "$CONFIG_DIR" "$STATE_DIR" "$BIN_DIR" "$IPC_DIR" <<'PY'
import os, sys
from pathlib import Path
home = Path(sys.argv[1]).resolve()
paths = {name: Path(value).resolve() for name, value in zip(
    ("application", "configuration", "state", "bin", "ipc"), sys.argv[2:]
)}
unsafe = {
    Path("/"), home, home / ".config", home / ".local", home / ".local" / "share",
    home / ".local" / "state", Path("/tmp"), Path("/var"), Path("/etc"),
    Path("/usr"), Path("/opt"), Path("/Applications"),
}
for name in ("application", "configuration", "state"):
    path = paths[name]
    if path in unsafe:
        raise SystemExit(f"claude-auto: refusing unsafe {name} directory: {path}")
    marker = path / ".claude-auto-owned"
    if path.exists() and any(path.iterdir()) and not marker.is_file():
        raise SystemExit(f"claude-auto: refusing non-empty unowned {name} directory: {path}")
bin_marker = paths["bin"] / ".claude-auto-bin-owned"
if not bin_marker.is_file():
    for command in ("claude", "claude-auto", "claude-raw"):
        if (paths["bin"] / command).exists():
            raise SystemExit(f"claude-auto: refusing to replace unowned command: {paths['bin'] / command}")
recursive = [paths[name] for name in ("application", "configuration", "state")]
for index, left in enumerate(recursive):
    for right in recursive[index + 1:]:
        if left == right or left in right.parents or right in left.parents:
            raise SystemExit(f"claude-auto: managed directories must not overlap: {left} and {right}")
longest = paths["ipc"] / ("0" * 32 + "-launch.sock")
if len(os.fsencode(longest)) > 95:
    raise SystemExit(f"claude-auto: IPC directory is too long for Unix sockets: {paths['ipc']}")
PY
}

acquire_install_lock() {
  local lock_dir coord ready
  lock_dir="$(dirname "$APP_DIR")"
  if [[ ! -d "$lock_dir" ]]; then
    install -d -m 700 "$lock_dir"
  fi
  coord="$(mktemp -d "${TMPDIR:-/tmp}/claude-auto-install.XXXXXX")"
  ready="$coord/ready"
  "$PYTHON" -c 'import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDONLY)
parent = os.getppid()
fcntl.flock(fd, fcntl.LOCK_EX)
open(sys.argv[2], "w").close()
while os.getppid() == parent:
    time.sleep(0.25)
os.close(fd)' "$lock_dir" "$ready" &
  INSTALL_LOCK_PID=$!
  INSTALL_LOCK_COORD="$coord"
  while [[ ! -e "$ready" ]]; do
    kill -0 "$INSTALL_LOCK_PID" 2>/dev/null || fail "could not acquire installation lock"
    sleep 0.01
  done
}

release_install_lock() {
  if [[ -n "$INSTALL_LOCK_PID" ]]; then
    kill "$INSTALL_LOCK_PID" 2>/dev/null || true
    wait "$INSTALL_LOCK_PID" 2>/dev/null || true
    INSTALL_LOCK_PID=""
  fi
  if [[ -n "$INSTALL_LOCK_COORD" ]]; then
    rm -rf "$INSTALL_LOCK_COORD"
    INSTALL_LOCK_COORD=""
  fi
}

find_raw_claude() {
  local configured candidate resolved absolute
  configured="$(existing_config_value raw_claude_path)"
  for candidate in "${CLAUDE_AUTO_RAW_CLAUDE:-}" "$configured" "$(command -v claude 2>/dev/null || true)" "$HOME_DIR/.local/bin/claude" /usr/local/bin/claude; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    resolved="$(resolve_executable "$candidate")"
    absolute="$(absolute_path "$candidate")"
    if [[ -f "$absolute" ]] && grep -Fq 'claude_auto.py" shim' "$absolute" 2>/dev/null; then
      continue
    fi
    [[ "$absolute" == "$BIN_DIR"/* || "$resolved" == "$BIN_DIR"/* ]] && continue
    printf '%s\n' "$absolute"
    return
  done
  fail "Claude Code was not found. Install it first: https://docs.anthropic.com/en/docs/claude-code"
}

find_tmux() {
  local configured candidate
  configured="$(existing_config_value tmux_path)"
  for candidate in "${CLAUDE_AUTO_TMUX:-}" "$configured" "$(command -v tmux 2>/dev/null || true)" /opt/homebrew/bin/tmux /usr/local/bin/tmux /usr/bin/tmux; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    resolve_executable "$candidate"
    return
  done
  fail "tmux is required. Install it with 'brew install tmux' (macOS) or your Linux package manager"
}

choose_shell_rc() {
  if [[ -n "${CLAUDE_AUTO_SHELL_RC:-}" ]]; then
    printf '%s\n' "$CLAUDE_AUTO_SHELL_RC"
  elif [[ "${SHELL:-}" == */zsh ]]; then
    printf '%s\n' "$HOME_DIR/.zshrc"
  elif [[ "${SHELL:-}" == */bash ]]; then
    printf '%s\n' "$HOME_DIR/.bashrc"
  else
    printf '%s\n' "$HOME_DIR/.profile"
  fi
}

install_source() {
  local script_dir="" local_source=""
  if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local_source="$script_dir/src/claude_auto.py"
  fi
  if [[ -n "$local_source" && -f "$local_source" ]]; then
    install -m 700 "$local_source" "$SOURCE_PATH"
  else
    command -v curl >/dev/null 2>&1 || fail "curl is required for remote installation"
    local temporary
    temporary="$(mktemp)"
    trap 'rm -f "$temporary"' EXIT
    curl -fsSL "https://raw.githubusercontent.com/$REPO/$BRANCH/src/claude_auto.py" -o "$temporary"
    "$PYTHON" -m py_compile "$temporary"
    install -m 700 "$temporary" "$SOURCE_PATH"
    rm -f "$temporary"
    trap - EXIT
  fi
}

write_config() {
  "$PYTHON" - "$CONFIG_PATH" "$RAW_CLAUDE" "$TMUX" "$PYTHON" "$IPC_DIR" "$BIN_DIR" <<'PY'
import json, os, sys, tempfile
from pathlib import Path
path = Path(sys.argv[1])
try:
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
except (OSError, ValueError) as exc:
    raise SystemExit(f"claude-auto: refusing to replace unreadable config: {exc}")
if not isinstance(current, dict):
    raise SystemExit("claude-auto: config must be a JSON object")
defaults = {
    "version": 1,
}
for key, value in defaults.items():
    current.setdefault(key, value)
current.update({
    "raw_claude_path": sys.argv[2],
    "tmux_path": sys.argv[3],
    "python_path": sys.argv[4],
    "ipc_dir": sys.argv[5],
    "bin_dir": sys.argv[6],
})
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(current, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)
    os.chmod(path, 0o600)
finally:
    try: os.unlink(temporary)
    except FileNotFoundError: pass
PY
}

write_shims() {
  local app_q config_q state_q settings_q bin_q python_q source_q raw_q
  app_q="$(shell_quote "$APP_DIR")"
  config_q="$(shell_quote "$CONFIG_DIR")"
  state_q="$(shell_quote "$STATE_DIR")"
  settings_q="$(shell_quote "$SETTINGS_PATH")"
  bin_q="$(shell_quote "$BIN_DIR")"
  python_q="$(shell_quote "$PYTHON")"
  source_q="$(shell_quote "$SOURCE_PATH")"
  raw_q="$(shell_quote "$RAW_CLAUDE")"
  cat > "$BIN_DIR/claude" <<EOF
#!/bin/sh
export CLAUDE_AUTO_APP_DIR=$app_q
export CLAUDE_AUTO_CONFIG_DIR=$config_q
export CLAUDE_AUTO_STATE_DIR=$state_q
export CLAUDE_AUTO_SETTINGS_PATH=$settings_q
export CLAUDE_AUTO_BIN_DIR=$bin_q
exec $python_q $source_q shim "\$@"
EOF
  cat > "$BIN_DIR/claude-auto" <<EOF
#!/bin/sh
export CLAUDE_AUTO_APP_DIR=$app_q
export CLAUDE_AUTO_CONFIG_DIR=$config_q
export CLAUDE_AUTO_STATE_DIR=$state_q
export CLAUDE_AUTO_SETTINGS_PATH=$settings_q
export CLAUDE_AUTO_BIN_DIR=$bin_q
exec $python_q $source_q manage "\$@"
EOF
  cat > "$BIN_DIR/claude-raw" <<EOF
#!/bin/sh
export CLAUDE_AUTO_DISABLED=1
export CLAUDE_AUTO_CONFIG_DIR=$config_q
unset CLAUDE_AUTO_RUN_ID CLAUDE_AUTO_MANAGED
exec $raw_q "\$@"
EOF
  chmod 755 "$BIN_DIR/claude" "$BIN_DIR/claude-auto" "$BIN_DIR/claude-raw"
}

merge_hooks() {
  local previous_command
  HOOK_COMMAND="$(shell_quote "$PYTHON") $(shell_quote "$SOURCE_PATH") hook"
  previous_command="$(existing_manifest_value hook_command)"
  "$PYTHON" - "$SETTINGS_PATH" "$HOOK_COMMAND" "$previous_command" <<'PY'
import json, os, sys, tempfile
from pathlib import Path
path, command, previous = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
managed_commands = {command}
if previous:
    managed_commands.add(previous)
try:
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
except (OSError, ValueError) as exc:
    raise SystemExit(f"claude-auto: settings are unreadable; refusing installation: {exc}")
if not isinstance(settings, dict):
    raise SystemExit("claude-auto: settings must be a JSON object")
hooks = settings.setdefault("hooks", {})
if not isinstance(hooks, dict):
    raise SystemExit("claude-auto: settings.hooks must be a JSON object")
for event in ("SessionStart", "UserPromptSubmit", "Stop", "StopFailure", "SessionEnd"):
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        raise SystemExit(f"claude-auto: settings.hooks.{event} must be an array")
    found = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        commands = entry.get("hooks", [])
        if not isinstance(commands, list):
            continue
        for hook in commands:
            if (
                isinstance(hook, dict)
                and hook.get("type") == "command"
                and hook.get("command") in managed_commands
            ):
                hook.update({"type": "command", "command": command, "timeout": 10})
                found = True
    if not found:
        entries.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
finally:
    try: os.unlink(temporary)
    except FileNotFoundError: pass
PY
}

update_path() {
  local shell_rc="$1" start='# >>> claude-auto >>>' end='# <<< claude-auto <<<' bin_q
  bin_q="$(shell_quote "$BIN_DIR")"
  "$PYTHON" - "$shell_rc" "$bin_q" "$start" "$end" <<'PY'
import re, sys
from pathlib import Path
path, quoted_bin, start, end = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
text = path.read_text(encoding="utf-8") if path.exists() else ""
pattern = re.compile(r"(?:^|\n)" + re.escape(start) + r"\n.*?\n" + re.escape(end) + r"\n?", re.S)
text = pattern.sub("\n", text).rstrip()
block = f'{start}\nexport PATH={quoted_bin}:"$PATH"\n{end}'
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text((text + "\n\n" if text else "") + block + "\n", encoding="utf-8")
PY
  SHELL_RC="$shell_rc"
}

write_manifest() {
  "$PYTHON" - "$CONFIG_DIR/install-manifest.json" "$HOOK_COMMAND" "$SHELL_RC" "$APP_DIR" "$CONFIG_DIR" "$STATE_DIR" "$BIN_DIR" <<'PY'
import json, os, sys
from pathlib import Path
path = Path(sys.argv[1])
path.write_text(json.dumps({
    "version": 1,
    "hook_command": sys.argv[2],
    "shell_rc": sys.argv[3],
    "app_dir": sys.argv[4],
    "config_dir": sys.argv[5],
    "state_dir": sys.argv[6],
    "bin_dir": sys.argv[7],
    "installed_hooks": ["SessionStart", "UserPromptSubmit", "Stop", "StopFailure", "SessionEnd"],
}, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
}

PYTHON="$(find_python)"
APP_DIR="$(absolute_path "$APP_DIR")"
CONFIG_DIR="$(absolute_path "$CONFIG_DIR")"
STATE_DIR="$(absolute_path "$STATE_DIR")"
SETTINGS_PATH="$(absolute_path "$SETTINGS_PATH")"
SOURCE_PATH="$APP_DIR/claude_auto.py"
CONFIG_PATH="$CONFIG_DIR/config.json"
trap 'release_install_lock' EXIT
acquire_install_lock
if [[ -z "$BIN_DIR" ]]; then
  BIN_DIR="$(existing_config_value bin_dir)"
fi
BIN_DIR="$(absolute_path "${BIN_DIR:-$HOME_DIR/.local/claude-auto/bin}")"
if [[ -z "$IPC_DIR" ]]; then
  IPC_DIR="$(existing_config_value ipc_dir)"
fi
IPC_DIR="$(absolute_path "${IPC_DIR:-/tmp/claude-auto-$(id -u)}")"
validate_install_paths
RAW_CLAUDE="$(find_raw_claude)"
TMUX="$(find_tmux)"
SHELL_RC="$(choose_shell_rc)"

install -d -m 700 "$APP_DIR" "$CONFIG_DIR" "$STATE_DIR" "$STATE_DIR/runs" "$STATE_DIR/locks" "$STATE_DIR/unmanaged" "$IPC_DIR"
for owned in "$APP_DIR" "$CONFIG_DIR" "$STATE_DIR"; do
  install -m 600 /dev/null "$owned/.claude-auto-owned"
done
install -d -m 755 "$BIN_DIR"
install -m 600 /dev/null "$BIN_DIR/.claude-auto-bin-owned"
install_source
write_config
write_shims
merge_hooks
update_path "$SHELL_RC"
write_manifest

CLAUDE_AUTO_APP_DIR="$APP_DIR" \
CLAUDE_AUTO_CONFIG_DIR="$CONFIG_DIR" \
CLAUDE_AUTO_STATE_DIR="$STATE_DIR" \
CLAUDE_AUTO_SETTINGS_PATH="$SETTINGS_PATH" \
"$PYTHON" "$SOURCE_PATH" install-self-test >/dev/null

CLAUDE_AUTO_APP_DIR="$APP_DIR" \
CLAUDE_AUTO_CONFIG_DIR="$CONFIG_DIR" \
CLAUDE_AUTO_STATE_DIR="$STATE_DIR" \
CLAUDE_AUTO_SETTINGS_PATH="$SETTINGS_PATH" \
"$PYTHON" "$SOURCE_PATH" install-report-active

printf '%s\n' "claude-auto installed successfully."
printf '  Claude Code: %s\n' "$RAW_CLAUDE"
printf '  tmux:       %s\n' "$TMUX"
printf '  shell PATH: %s\n' "$SHELL_RC"
printf '%s\n' "Open a new terminal or run:"
printf '  source %q\n' "$SHELL_RC"
printf '%s\n' "Then verify with: claude-auto doctor"
