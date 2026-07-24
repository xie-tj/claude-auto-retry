#!/usr/bin/python3
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from pathlib import Path

VERSION = "1.0.6"
HOME = Path.home()
APP_DIR = Path(os.environ.get("CLAUDE_AUTO_APP_DIR", HOME / ".local" / "share" / "claude-auto"))
STATE_DIR = Path(os.environ.get("CLAUDE_AUTO_STATE_DIR", HOME / ".local" / "state" / "claude-auto"))
RUNS_DIR = STATE_DIR / "runs"
LOCKS_DIR = STATE_DIR / "locks"
UNMANAGED_DIR = STATE_DIR / "unmanaged"
CONFIG_DIR = Path(os.environ.get("CLAUDE_AUTO_CONFIG_DIR", HOME / ".config" / "claude-auto"))
CONFIG_PATH = CONFIG_DIR / "config.json"
GLOBAL_PAUSE = STATE_DIR / "paused"
GLOBAL_LOCK = STATE_DIR / "global.lock"
LIFECYCLE_LOCK_DIR = APP_DIR.parent
GLOBAL_RECOVERY_CONTROL = STATE_DIR / "recovery-control.lock"
BINDING_OWNER = STATE_DIR / "tmux-binding-owned"
SCRIPT = Path(__file__).resolve()
SETTINGS_PATH = Path(os.environ.get("CLAUDE_AUTO_SETTINGS_PATH", HOME / ".claude" / "settings.json"))
SHELL_RC_PATHS = (HOME / ".zshrc", HOME / ".bashrc", HOME / ".bash_profile", HOME / ".profile")


def bootstrap_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


_BOOTSTRAP_CONFIG = bootstrap_config()
SHIM_DIR = Path(
    os.environ.get("CLAUDE_AUTO_BIN_DIR")
    or _BOOTSTRAP_CONFIG.get("bin_dir")
    or HOME / ".local" / "claude-auto" / "bin"
)
IPC_DIR = Path(
    os.environ.get("CLAUDE_AUTO_IPC_DIR")
    or _BOOTSTRAP_CONFIG.get("ipc_dir")
    or "/tmp/claude-auto-{}".format(os.getuid())
)


def configured_executable(config_key, env_key, command, candidates=(), preserve_entry=False):
    configured = os.environ.get(env_key) or _BOOTSTRAP_CONFIG.get(config_key)
    paths = [configured] if configured else []
    discovered = shutil.which(command)
    if discovered:
        paths.append(discovered)
    paths.extend(candidates)
    for value in paths:
        if not value:
            continue
        path = Path(value).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.exists() and os.access(resolved, os.X_OK):
            if command == "claude" and SHIM_DIR in resolved.parents:
                continue
            return path.absolute() if preserve_entry else resolved
    return Path(configured or command)


RAW_CLAUDE = configured_executable(
    "raw_claude_path",
    "CLAUDE_AUTO_RAW_CLAUDE",
    "claude",
    (HOME / ".local" / "bin" / "claude", Path("/usr/local/bin/claude")),
    preserve_entry=True,
)
TMUX = configured_executable(
    "tmux_path",
    "CLAUDE_AUTO_TMUX",
    "tmux",
    (Path("/opt/homebrew/bin/tmux"), Path("/usr/local/bin/tmux"), Path("/usr/bin/tmux")),
)
PYTHON = configured_executable(
    "python_path",
    "CLAUDE_AUTO_PYTHON",
    "python3",
    (Path(sys.executable), Path("/usr/bin/python3")),
)
RECOVERY_PREFIX = "[claude-auto recovery "
TARGET_EVENTS = {"StopFailure", "Stop", "SessionStart", "SessionEnd", "UserPromptSubmit"}
ERROR_POLICIES = {
    "timeout": {"delays": (5, 15, 30), "label": "请求超时"},
    "stream_error": {"delays": (5, 15, 30), "label": "响应流解码错误"},
    "overloaded": {"delays": (15, 30, 60), "label": "上游服务过载"},
}
TIMEOUT_DELAYS = ERROR_POLICIES["timeout"]["delays"]
OVERLOADED_DELAYS = ERROR_POLICIES["overloaded"]["delays"]
MAX_RECOVERIES = 3
EVENT_MAX_AGE = 300
RECOVERY_PROVENANCE_TTL = 5 * 60
RETRY_STATE_EXPIRY = 10 * 60
PASTE_SETTLE_DELAY = 0.25
SUBMIT_RETRY_DELAY = 0.25
SUBMISSION_ACK_TIMEOUT = 5
TERMINAL_FAILURE_GRACE = 1
TERMINAL_HOOK_DEDUP_WINDOW = 5
TERMINAL_POLL_INTERVAL = 0.25
RESULT_FEEDBACK_SECONDS = 2
UPDATE_CHECK_INTERVAL = 5
TEMP_MAX_MEMORY = 8 * 1024 * 1024
STALE_RETENTION = 24 * 60 * 60
TEMP_RETENTION = 60 * 60

MANAGEMENT_COMMANDS = {
    "agents", "auth", "auto-mode", "doctor", "gateway", "install", "mcp",
    "plugin", "plugins", "project", "setup-token", "ultrareview", "update", "upgrade",
}
DIRECT_FLAGS = {
    "--safe-mode", "--bare", "--bg", "--background", "--tmux", "--no-session-persistence",
}
DIRECT_SIMPLE_FLAGS = {"-h", "--help", "-v", "--version"}
VALUE_OPTIONS = {
    "--agent", "--agents", "--append-system-prompt", "--append-system-prompt-file",
    "--debug-file", "--effort", "--fallback-model", "--json-schema", "--max-budget-usd",
    "--model", "-n", "--name", "--output-format", "--permission-mode", "--setting-sources",
    "--settings", "--system-prompt", "--system-prompt-file", "--input-format",
    "--session-id", "--remote-control-session-name-prefix", "--max-turns",
}
VARIADIC_OPTIONS = {
    "--add-dir", "--allowedTools", "--allowed-tools", "--betas", "--disallowedTools",
    "--disallowed-tools", "--file", "--mcp-config", "--tools",
}
REPEAT_VALUE_OPTIONS = {"--plugin-dir", "--plugin-url"}
BOOL_OPTIONS = {
    "-p", "--print", "--allow-dangerously-skip-permissions", "--ax-screen-reader", "--brief",
    "--chrome", "--dangerously-skip-permissions", "--disable-slash-commands",
    "--exclude-dynamic-system-prompt-sections", "--forward-subagent-text", "--ide",
    "--include-hook-events", "--include-partial-messages", "--no-chrome",
    "--no-session-persistence", "--replay-user-messages", "--strict-mcp-config", "--verbose",
    "--fork-session",
}
OPTIONAL_VALUE_OPTIONS = {"-d", "--debug", "--prompt-suggestions"}


def ensure_private_ipc_dir():
    try:
        IPC_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = IPC_DIR.stat()
        if info.st_uid != os.getuid() or (info.st_mode & 0o077):
            raise PermissionError("IPC directory is not private: {}".format(IPC_DIR))
        IPC_DIR.chmod(0o700)
        longest = IPC_DIR / ("0" * 32 + "-launch.sock")
        if len(os.fsencode(str(longest))) > 95:
            raise OSError("Unix socket path is too long; choose a shorter CLAUDE_AUTO_IPC_DIR")
    except OSError as exc:
        raise RuntimeError("cannot use private IPC directory {}: {}".format(IPC_DIR, exc))


def ipc_socket_path(run_id, name):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id or ""):
        raise ValueError("invalid run id")
    return IPC_DIR / (run_id + "-" + name + ".sock")


def ensure_dirs():
    ensure_private_ipc_dir()
    for path in (STATE_DIR, RUNS_DIR, LOCKS_DIR, UNMANAGED_DIR, CONFIG_DIR):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.chmod(0o700)
        except OSError:
            pass


def atomic_json(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {} if default is None else default


def append_jsonl(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


class DescriptorLock:
    def __init__(self, fd):
        self.fd = fd

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __del__(self):
        self.close()


def lifecycle_lock(exclusive=False):
    fd = os.open(str(LIFECYCLE_LOCK_DIR), os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    return DescriptorLock(fd)


def global_lock():
    ensure_dirs()
    handle = open(GLOBAL_LOCK, "a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


class CompositeLock:
    def __init__(self, handles):
        self.handles = handles

    def close(self):
        while self.handles:
            self.handles.pop().close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        self.close()


def recovery_control_lock(run_id=None, global_exclusive=False):
    ensure_dirs()
    handles = []
    global_handle = open(GLOBAL_RECOVERY_CONTROL, "a+")
    fcntl.flock(
        global_handle.fileno(),
        fcntl.LOCK_EX if global_exclusive else fcntl.LOCK_SH,
    )
    handles.append(global_handle)
    if run_id:
        run_handle = open(run_dir(run_id) / "recovery-control.lock", "a+")
        fcntl.flock(run_handle.fileno(), fcntl.LOCK_EX)
        handles.append(run_handle)
    return CompositeLock(handles)


def run_dir(run_id):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id or ""):
        raise ValueError("invalid run id")
    return RUNS_DIR / run_id


def run_meta_path(run_id):
    return run_dir(run_id) / "meta.json"


def update_meta(run_id, **updates):
    directory = run_dir(run_id)
    if not directory.is_dir():
        return {}
    lock_path = directory / "meta.lock"
    try:
        handle = open(lock_path, "a+")
    except FileNotFoundError:
        return {}
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        if not directory.is_dir():
            return {}
        meta_path = directory / "meta.json"
        meta = read_json(meta_path, None)
        if not isinstance(meta, dict) or not meta:
            return {}
        meta.update(updates)
        meta["updated_at"] = time.time()
        atomic_json(meta_path, meta)
    return meta


def get_meta(run_id):
    return read_json(run_meta_path(run_id), {})


def pid_alive(pid):
    try:
        if not pid:
            return False
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def process_identity(pid):
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return None
    if not pid_alive(pid):
        return None
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        remainder = stat_path.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return "proc:{}".format(remainder[19])
    except (FileNotFoundError, IndexError, OSError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    started = " ".join(result.stdout.split())
    return "ps:{}".format(started) if result.returncode == 0 and started else None


def process_identity_matches(pid, expected):
    return bool(expected) and process_identity(pid) == expected


def tmux_run(args, check=False, capture=True, timeout=10):
    command = [str(TMUX)] + list(args)
    return subprocess.run(
        command,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=timeout,
    )


def tmux_target_identity(target):
    if not target or not TMUX.exists():
        return None
    result = tmux_run(
        ["display-message", "-p", "-t", target, "#{session_created}:#{window_id}:#{pane_id}:#{pane_start_command}"],
        capture=True,
    )
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value or None


def tmux_target_alive(target, expected_identity=None):
    try:
        identity = tmux_target_identity(target)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if not identity:
        return False
    return expected_identity is None or identity == expected_identity


def run_is_live(run_id, meta=None):
    meta = meta or get_meta(run_id)
    if meta.get("state") in {"exited", "blocked", "uninstalled"}:
        return False
    if process_identity_matches(meta.get("supervisor_pid"), meta.get("supervisor_identity")):
        return True
    if process_identity_matches(meta.get("watchdog_pid"), meta.get("watchdog_identity")):
        return True
    return tmux_target_alive(meta.get("main_pane"), meta.get("tmux_identity"))


def lock_name(kind, value):
    digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
    return LOCKS_DIR / (kind + "-" + digest)


def acquire_named_lock(kind, value, run_id):
    ensure_dirs()
    path = lock_name(kind, value)
    with global_lock():
        if path.exists():
            owner = read_json(path / "owner.json", {})
            owner_run = owner.get("run_id")
            if owner_run == run_id:
                return True, owner_run
            if owner_run and run_is_live(owner_run):
                return False, owner_run
            shutil.rmtree(path, ignore_errors=True)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            owner = read_json(path / "owner.json", {})
            return False, owner.get("run_id")
        atomic_json(path / "owner.json", {"run_id": run_id, "created_at": time.time()})
        return True, run_id


def release_named_lock(kind, value, run_id):
    if not value:
        return
    path = lock_name(kind, value)
    with global_lock():
        owner = read_json(path / "owner.json", {})
        if owner.get("run_id") == run_id:
            shutil.rmtree(path, ignore_errors=True)


def canonical_project(cwd=None):
    return str(Path(cwd or os.getcwd()).resolve())


def normalize_text(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif value is None:
        value = ""
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(value.split())


def structured_error_values(value):
    values = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if normalize_text(key) in {"error", "type", "code", "name"}:
                values.append(normalize_text(nested))
            values.extend(structured_error_values(nested))
    elif isinstance(value, list):
        for nested in value:
            values.extend(structured_error_values(nested))
    return values


def classify_failure(payload):
    error_type = normalize_text(payload.get("error_type"))
    error = normalize_text(payload.get("error"))
    details_value = payload.get("error_details")
    details = normalize_text(details_value)
    last = normalize_text(payload.get("last_assistant_message"))
    structured = set(structured_error_values(details_value))
    combined = " ".join(part for part in (error_type, error, details, last) if part)
    if (
        error_type in {"overloaded", "service_unavailable_error"}
        or error == "overloaded"
        or structured.intersection({"overloaded", "service_unavailable_error"})
        or "service_unavailable_error" in combined
        or "servers are currently overloaded" in combined
        or "server is currently overloaded" in combined
    ):
        return "overloaded"
    if "stream error: error decoding response body" in combined:
        return "stream_error"
    if (
        "the operation timed out" in combined
        or "operation timed out" in combined
        or "request timed out" in combined
        or error_type in {"timeout", "request_timeout"}
        or error in {"timeout", "request_timeout"}
        or structured.intersection({"timeout", "request_timeout"})
    ):
        return "timeout"
    return None


def script_fingerprint():
    try:
        digest = hashlib.sha256()
        with open(SCRIPT, "rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def event_record(kind, run_id=None, **fields):
    record = {"at": time.time(), "kind": kind}
    if run_id:
        record["run_id"] = run_id
    record.update(fields)
    return record


def send_run_event(run_id, event):
    directory = run_dir(run_id)
    socket_path = ipc_socket_path(run_id, "events")
    payload = json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            client.sendto(payload, str(socket_path))
            return True
        finally:
            client.close()
    except OSError:
        append_jsonl(directory / "pending.jsonl", event)
        return False


def expected_recovery_path(run_id):
    return run_dir(run_id) / "expected-recovery.json"


def mark_expected_recovery(run_id, prompt):
    atomic_json(
        expected_recovery_path(run_id),
        {"sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "expires_at": time.time() + RECOVERY_PROVENANCE_TTL},
    )


def consume_expected_recovery(run_id, prompt):
    path = expected_recovery_path(run_id)
    lock_path = run_dir(run_id) / "expected-recovery.lock"
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        expected = read_json(path, {})
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return (
            float(expected.get("expires_at") or 0) >= time.time()
            and expected.get("sha256") == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        )


def clear_expected_recovery(run_id):
    try:
        expected_recovery_path(run_id).unlink()
    except FileNotFoundError:
        pass


def session_lock_from_hook(run_id, session_id, cwd):
    meta = get_meta(run_id)
    expected = meta.get("session_id")
    if expected and expected != session_id:
        release_named_lock("session", expected, run_id)
    ok, owner = acquire_named_lock("session", session_id, run_id)
    project_lock = meta.get("project_lock")
    if project_lock:
        release_named_lock("project", project_lock, run_id)
    if ok:
        update_meta(run_id, session_id=session_id, project_lock=None, lock_state="owned")
        return True, None
    update_meta(run_id, session_id=session_id, project_lock=None, lock_state="blocked", state="blocked")
    return False, owner


def hook_main():
    """Report recovery observations only.

    Hook JSON and the per-user event socket are deliberately not lifecycle
    authority.  In particular SessionEnd and lock_blocked may update recovery
    state, but only pane_run_main observes the direct raw-Claude child and may
    close an interactive tmux target.
    """
    if os.environ.get("CLAUDE_AUTO_DISABLED") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    event_name = payload.get("hook_event_name")
    if event_name not in TARGET_EVENTS:
        return 0
    session_id = str(payload.get("session_id") or "unknown")
    run_id = os.environ.get("CLAUDE_AUTO_RUN_ID", "")
    try:
        managed = run_dir(run_id).exists()
    except ValueError:
        managed = False
    if not managed:
        if event_name == "SessionEnd":
            try:
                (UNMANAGED_DIR / (hashlib.sha256(session_id.encode()).hexdigest() + ".jsonl")).unlink()
            except FileNotFoundError:
                pass
        elif event_name == "StopFailure":
            category = classify_failure(payload)
            if category:
                append_jsonl(
                    UNMANAGED_DIR / (hashlib.sha256(session_id.encode()).hexdigest() + ".jsonl"),
                    event_record("unmanaged_failure", category=category),
                )
        return 0

    agent_id = payload.get("agent_id")
    subagent = bool(agent_id)
    if event_name == "SessionStart":
        # Lock ownership is safe state bookkeeping; it has no teardown effect.
        if subagent or session_id == "unknown":
            return 0
        ok, owner = session_lock_from_hook(run_id, session_id, os.getcwd())
        send_run_event(run_id, event_record("session_start", run_id, session_id=session_id))
        if not ok:
            message = "该 Claude session 已由另一个受管进程使用；请用 claude-auto list/attach 连接已有会话。"
            send_run_event(run_id, event_record("lock_blocked", run_id, session_id=session_id, owner=owner))
            json.dump({"continue": False, "stopReason": message, "suppressOutput": False}, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
        return 0
    if event_name == "SessionEnd":
        send_run_event(run_id, event_record("session_end", run_id, session_id=session_id, subagent=subagent))
        return 0
    if event_name == "Stop":
        if not subagent:
            send_run_event(run_id, event_record("turn_success", run_id, session_id=session_id))
        return 0
    if event_name == "UserPromptSubmit":
        if subagent:
            return 0
        prompt = str(payload.get("prompt") or "")
        send_run_event(run_id, event_record("prompt_submit", run_id, session_id=session_id, recovery=consume_expected_recovery(run_id, prompt)))
        return 0
    category = classify_failure(payload)
    fields = {
        "session_id": session_id,
        "prompt_id": payload.get("prompt_id"),
        "subagent": subagent,
        "agent_id": str(agent_id) if agent_id else None,
    }
    if category:
        fields["category"] = category
    send_run_event(
        run_id,
        event_record(
            "recoverable_failure" if category else "unsupported_failure",
            run_id,
            **fields
        ),
    )
    return 0


def recovery_message(number, category):
    return (
        "[claude-auto recovery {}/3][{}] 继续完成刚才因 API {} 中断的任务。"
        "先检查当前状态和已有结果，不要重复已经完成的命令、文件写入或外部操作。"
        "对于删除、推送、部署、支付或其他远程写入，先通过可观察状态确认是否已执行；"
        "只有状态明确且后续操作安全时才继续，无法确认时请停止并说明。"
    ).format(number, category, ERROR_POLICIES[category]["label"])


RECOVERY_LABELS = {
    "timeout": "Timeout",
    "stream_error": "Stream interrupted",
    "overloaded": "Service overloaded",
}


STATUS_PRESENTATION = {
    "starting": ("◔", "[~]", "cyan"),
    "ready": ("●", "[o]", "cyan"),
    "countdown": ("◔", "[~]", "cyan"),
    "submitting": ("●", "[o]", "cyan"),
    "awaiting": ("●", "[o]", "cyan"),
    "completed": ("✓", "[+]", "green"),
    "skipped": ("○", "[-]", "dim"),
    "cancelled": ("○", "[-]", "dim"),
    "paused": ("○", "[-]", "dim"),
    "paused_global": ("○", "[-]", "dim"),
    "update_available": ("!", "[!]", "yellow"),
    "unconfirmed": ("!", "[!]", "yellow"),
    "stale": ("!", "[!]", "red"),
    "exhausted": ("!", "[!]", "red"),
    "unsupported": ("!", "[!]", "red"),
    "blocked": ("!", "[!]", "red"),
    "incompatible": ("!", "[!]", "red"),
    "target_lost": ("!", "[!]", "red"),
    "end_cleanup_failed": ("!", "[!]", "red"),
}


def tmux_status_style(state, symbol):
    _, _, style = STATUS_PRESENTATION.get(state, ("•", "[*]", ""))
    if not style:
        return symbol
    opening = "#[dim]" if style == "dim" else "#[fg={}]".format(style)
    return opening + symbol + "#[default]"


def ansi_status_style(state, symbol):
    _, _, style = STATUS_PRESENTATION.get(state, ("•", "[*]", ""))
    code = {
        "cyan": "36",
        "green": "32",
        "yellow": "33",
        "red": "31",
        "dim": "2",
    }.get(style)
    return "\033[{}m{}\033[0m".format(code, symbol) if code else symbol


def terminal_capabilities(stream=None, assume_tty=False):
    stream = stream or sys.stdout
    is_tty = assume_tty or bool(getattr(stream, "isatty", lambda: False)())
    color = (
        is_tty
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "").lower() != "dumb"
    )
    encoding = (getattr(stream, "encoding", None) or "utf-8").lower()
    unicode = "utf" in encoding
    return color, unicode


def terminal_cell_width(text):
    width = 0
    for character in text:
        category = unicodedata.category(character)
        if category.startswith("C") or category in {"Mn", "Me"}:
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
    return width


def truncate_terminal_cells(text, width):
    if width <= 0:
        return ""
    result = []
    used = 0
    for character in text:
        cells = terminal_cell_width(character)
        if used + cells > width:
            break
        result.append(character)
        used += cells
    return "".join(result)


def present_status(state, text, width=80, color=False, unicode=True):
    symbol, ascii_symbol, style = STATUS_PRESENTATION.get(
        state, ("•", "[*]", "")
    )
    if not unicode:
        symbol = ascii_symbol
        text = text.replace(" · ", " | ").replace("…", "...")

    width = max(0, int(width or 0))
    symbol_width = terminal_cell_width(symbol)
    if not width:
        return ""
    if symbol_width > width:
        return truncate_terminal_cells(symbol, width)

    available = max(0, width - symbol_width - 1)
    candidates = [text]
    if state == "countdown":
        without_skip = re.sub(r" · C-[^ ]+ X skip$", "", text)
        candidates.append(without_skip)
        match = re.search(r"recovery (\d+/\d+) in (\d+)s", text, re.IGNORECASE)
        if match:
            candidates.append(
                "Recovery {} {}s".format(match.group(1), match.group(2))
            )

    content = candidates[-1]
    for candidate in candidates:
        if terminal_cell_width(candidate) <= available:
            content = candidate
            break
    if terminal_cell_width(content) > available:
        marker = "…" if unicode else "..."
        marker_width = terminal_cell_width(marker)
        if marker_width <= available:
            content = (
                truncate_terminal_cells(content, available - marker_width) + marker
            )
        else:
            content = ""

    if color and style:
        symbol = tmux_status_style(state, symbol)
    return symbol + (" " + content if content else "")


def status_text(
    state,
    meta,
    retry_count=0,
    category=None,
    remaining=None,
    binding=True,
    phase=None,
    sent_attempts=0,
    failed_attempts=0,
    skip_key=None,
):
    del phase, sent_attempts, failed_attempts
    if state == "countdown":
        text = "{} · recovery {}/3 in {}s".format(
            RECOVERY_LABELS.get(category, "Recovery"),
            retry_count,
            max(0, int(math.ceil(remaining or 0))),
        )
        if binding and skip_key:
            text += " · {} skip".format(skip_key)
        return text
    if state == "submitting":
        return "Submitting recovery {}/3".format(retry_count)
    if state == "awaiting":
        return "Recovery {}/3 active".format(retry_count)
    if state == "completed":
        return "Recovery {}/3 complete".format(retry_count)
    labels = {
        "ready": "Recovery ready · v{}".format(VERSION),
        "unconfirmed": "Submit not confirmed · press Enter if recovery remains",
        "paused_global": "Recovery paused globally · claude-auto resume",
        "paused": "Recovery paused · claude-auto resume {}".format(meta.get("name", "<name>")),
        "skipped": "Recovery skipped",
        "cancelled": "Submit retries stopped",
        "stale": "Recovery event expired · inspect session",
        "exhausted": "Recovery stopped after 3 attempts · inspect session",
        "unsupported": "Recovery stopped · inspect session",
        "blocked": "Duplicate session · run claude-auto doctor",
        "incompatible": "Compatibility issue · run claude-auto doctor",
        "target_lost": "Target unavailable · run claude-auto doctor",
        "end_cleanup_failed": "End cleanup failed · run claude-auto cleanup <session>",
        "update_available": "Update installed · restart to update",
        "starting": "Starting auto-recovery…",
    }
    return labels.get(state, str(state).replace("_", " ").capitalize())


def write_status(run_id, state, **fields):
    directory = run_dir(run_id)
    status = {"state": state, "at": time.time()}
    status.update(fields)
    atomic_json(directory / "status.json", status)
    update_meta(run_id, status=state)


TMUX_STOCK_BORDER_FORMATS = {
    "#{pane_index} #{pane_title}",
    (
        '#{?pane_active,#[reverse],}#{pane_index}#[default] "#{pane_title}"'
        '#{?#{mouse},#[align=right]#[range=control|8]'
        '[#{?#{window_zoomed_flag},u,z}]#[norange]'
        '#[range=control|9][x]#[norange],}'
    ),
}
TMUX_BORDER_DEFAULTS = {
    "pane-border-status": "off",
}
TMUX_MAIN_PANE_OPTION = "@claude_auto_watchdog_main"
TMUX_BORDER_OPTIONS = (
    "pane-border-status",
    "pane-border-format",
    "@claude_auto_watchdog_status",
)


def stock_pane_border_format(value):
    return value in TMUX_STOCK_BORDER_FORMATS


def effective_window_option(window_id, name):
    try:
        result = tmux_run(
            ["show-options", "-A", "-w", "-v", "-t", window_id, name],
            capture=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").rstrip("\n")


def local_window_option_snapshot(window_id, name):
    try:
        presence = tmux_run(
            ["show-options", "-w", "-q", "-t", window_id, name],
            capture=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if presence.returncode != 0:
        return None
    if not (presence.stdout or "").rstrip("\n"):
        return {"local": False}
    try:
        value = tmux_run(
            ["show-options", "-w", "-q", "-v", "-t", window_id, name],
            capture=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if value.returncode != 0:
        return None
    return {"local": True, "value": (value.stdout or "").rstrip("\n")}


def local_pane_option_snapshot(pane_id, name):
    try:
        presence = tmux_run(
            ["show-options", "-p", "-q", "-t", pane_id, name],
            capture=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if presence.returncode != 0:
        return None
    if not (presence.stdout or "").rstrip("\n"):
        return {"local": False}
    value = tmux_run(
        ["show-options", "-p", "-q", "-v", "-t", pane_id, name],
        capture=True,
    )
    if value.returncode != 0:
        return None
    return {"local": True, "value": (value.stdout or "").rstrip("\n")}


def restore_watchdog_border(meta):
    window = meta.get("window_id")
    snapshot = meta.get("tmux_border_snapshot")
    owned = meta.get("tmux_border_owned")
    if not window or not isinstance(snapshot, dict):
        return
    for name in TMUX_BORDER_OPTIONS:
        saved = snapshot.get(name)
        if not isinstance(saved, dict):
            continue
        if isinstance(owned, dict) and name in owned:
            current = local_window_option_snapshot(window, name)
            if current != {"local": True, "value": owned[name]}:
                continue
        try:
            if saved.get("local"):
                tmux_run(
                    ["set-option", "-w", "-t", window, name, saved.get("value", "")],
                    capture=True,
                )
            else:
                tmux_run(
                    ["set-option", "-u", "-w", "-t", window, name],
                    capture=True,
                )
        except (OSError, subprocess.TimeoutExpired):
            pass
    pane = meta.get("main_pane")
    pane_saved = meta.get("tmux_main_pane_snapshot")
    pane_owned = meta.get("tmux_main_pane_owned")
    if pane and isinstance(pane_saved, dict):
        if pane_owned is not None:
            current = local_pane_option_snapshot(pane, TMUX_MAIN_PANE_OPTION)
            if current != {"local": True, "value": pane_owned}:
                return
        try:
            if pane_saved.get("local"):
                tmux_run(
                    [
                        "set-option", "-p", "-t", pane, TMUX_MAIN_PANE_OPTION,
                        pane_saved.get("value", ""),
                    ],
                    capture=True,
                )
            else:
                tmux_run(
                    ["set-option", "-u", "-p", "-t", pane, TMUX_MAIN_PANE_OPTION],
                    capture=True,
                )
        except (OSError, subprocess.TimeoutExpired):
            pass


def prepare_watchdog_display(run_id, lines):
    if lines < 16:
        return "hidden"
    meta = get_meta(run_id)
    window = meta.get("window_id")
    pane = meta.get("main_pane")
    if not window or not pane:
        return "hidden"
    for name, expected in TMUX_BORDER_DEFAULTS.items():
        if effective_window_option(window, name) != expected:
            return "pane"
    if not stock_pane_border_format(
        effective_window_option(window, "pane-border-format")
    ):
        return "pane"
    snapshot = {}
    for name in TMUX_BORDER_OPTIONS:
        saved = local_window_option_snapshot(window, name)
        if saved is None:
            return "pane"
        snapshot[name] = saved
    pane_snapshot = local_pane_option_snapshot(pane, TMUX_MAIN_PANE_OPTION)
    if pane_snapshot is None:
        return "pane"
    color, unicode = terminal_capabilities(sys.stdout, assume_tty=True)
    status = present_status(
        "starting", status_text("starting", meta), width=100, color=color, unicode=unicode
    )
    pane_format = "#{?@claude_auto_watchdog_main, #{@claude_auto_watchdog_status} ,}"
    owned = {
        "pane-border-status": "bottom",
        "pane-border-format": pane_format,
        "@claude_auto_watchdog_status": status,
    }
    update_meta(
        run_id,
        tmux_border_snapshot=snapshot,
        tmux_main_pane_snapshot=pane_snapshot,
        tmux_border_owned=owned,
        tmux_main_pane_owned="1",
    )
    commands = (
        ["set-option", "-p", "-t", pane, TMUX_MAIN_PANE_OPTION, "1"],
        ["set-option", "-w", "-t", window, "@claude_auto_watchdog_status", status],
        ["set-option", "-w", "-t", window, "pane-border-format", pane_format],
        ["set-option", "-w", "-t", window, "pane-border-status", "bottom"],
    )
    try:
        for command in commands:
            if tmux_run(command, capture=True).returncode != 0:
                raise OSError("tmux rejected watchdog border option")
    except (OSError, subprocess.TimeoutExpired):
        restore_watchdog_border(get_meta(run_id))
        return "pane"
    return "border"


def pane_status(state, text, width, color, unicode):
    line = present_status(
        state, text, width=width, color=False, unicode=unicode
    )
    if not color or not line:
        return line
    symbol, ascii_symbol, _ = STATUS_PRESENTATION.get(state, ("•", "[*]", ""))
    symbol = symbol if unicode else ascii_symbol
    return ansi_status_style(state, symbol) + line[len(symbol):]


def render_watchdog_status(state, text, mode, meta, previous_frame=None):
    if mode == "hidden":
        return ("hidden",)
    if mode == "pane":
        width = shutil.get_terminal_size((100, 1)).columns
        color, unicode = terminal_capabilities(sys.stdout)
        line = pane_status(
            state, text, width=max(1, width - 1), color=color, unicode=unicode
        )
        frame = ("pane", width, line)
        if frame == previous_frame:
            return previous_frame
        try:
            sys.stdout.write("\033[2J\033[H" + line)
            sys.stdout.flush()
        except OSError:
            return previous_frame
        return frame
    if mode == "border":
        window = meta.get("window_id")
        if not window:
            return previous_frame
        color, unicode = terminal_capabilities(sys.stdout, assume_tty=True)
        value = present_status(
            state, text.replace("#", "##"), width=100, color=color, unicode=unicode
        )
        frame = ("border", window, value)
        if frame == previous_frame:
            return previous_frame
        try:
            result = tmux_run(
                [
                    "set-option", "-w", "-t", window,
                    "@claude_auto_watchdog_status", value,
                ],
                capture=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            return previous_frame
        if result.returncode != 0:
            return previous_frame
        if meta.get("run_id"):
            owned = meta.get("tmux_border_owned")
            owned = dict(owned) if isinstance(owned, dict) else {}
            owned["@claude_auto_watchdog_status"] = value
            meta["tmux_border_owned"] = owned
            update_meta(meta["run_id"], tmux_border_owned=owned)
        return frame
    return previous_frame


def read_pending_events(directory):
    path = directory / "pending.jsonl"
    if not path.exists():
        return []
    processing = directory / "pending.processing"
    try:
        os.replace(path, processing)
    except FileNotFoundError:
        return []
    events = []
    try:
        with open(processing, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    finally:
        try:
            processing.unlink()
        except FileNotFoundError:
            pass
    return events


def consume_cancel(run_id):
    marker = run_dir(run_id) / "cancel"
    try:
        marker.unlink()
        return True
    except FileNotFoundError:
        return False


def is_paused(run_id):
    return GLOBAL_PAUSE.exists() or (run_dir(run_id) / "paused").exists()


def terminal_failure_observation(meta):
    pane = meta.get("main_pane")
    if not pane:
        return None
    try:
        position = tmux_run(
            [
                "display-message", "-p", "-t", pane,
                "#{history_size}:#{cursor_y}:#{cursor_x}",
            ],
            capture=True,
            timeout=1,
        )
        result = tmux_run(
            ["capture-pane", "-p", "-J", "-t", pane, "-S", "-12"],
            capture=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if position.returncode != 0 or result.returncode != 0:
        return None
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    marker_index = None
    for index in range(len(lines) - 1, -1, -1):
        if re.match(r"^⏺\s*api error\s*:", normalize_text(lines[index])):
            marker_index = index
            break
    if marker_index is None:
        return None
    error_lines = lines[marker_index:]
    marker_category = classify_failure(
        {"last_assistant_message": error_lines[0]}
    )
    if not marker_category:
        return None
    if len(error_lines) > 1:
        tail = normalize_text(error_lines[-1])
        if len(error_lines) != 2 or marker_category != "overloaded" or not re.fullmatch(
            r"our servers are currently overloaded\. please try again later\.?",
            tail,
        ):
            return None
    error_text = "\n".join(error_lines)
    identity = "{}\n{}".format((position.stdout or "").strip(), error_text)
    return marker_category, hashlib.sha256(identity.encode("utf-8")).hexdigest()


def inject_recovery(meta, message, run_id):
    pane = meta.get("main_pane")
    if not pane or not tmux_target_alive(pane, meta.get("tmux_identity")):
        return False
    buffer_name = "claude-auto-" + run_id[:12]
    mark_expected_recovery(run_id, message)
    try:
        result = tmux_run(["set-buffer", "-b", buffer_name, "--", message], capture=True)
        if result.returncode != 0:
            clear_expected_recovery(run_id)
            return False
        tmux_run(["send-keys", "-t", pane, "C-u"], capture=True)
        result = tmux_run(["paste-buffer", "-b", buffer_name, "-d", "-t", pane], capture=True)
        if result.returncode != 0:
            clear_expected_recovery(run_id)
            return False
        time.sleep(PASTE_SETTLE_DELAY)
        if is_paused(run_id) or (run_dir(run_id) / "cancel").exists():
            clear_expected_recovery(run_id)
            return False
        result = tmux_run(["send-keys", "-t", pane, "Enter"], capture=True)
        if result.returncode != 0:
            clear_expected_recovery(run_id)
            return False
        return time.monotonic()
    finally:
        tmux_run(["delete-buffer", "-b", buffer_name], capture=True)


def send_recovery_submit_key(meta, run_id):
    path = expected_recovery_path(run_id)
    lock_path = run_dir(run_id) / "expected-recovery.lock"
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        if not path.exists():
            return "consumed"
        if is_paused(run_id):
            return "paused"
        if (run_dir(run_id) / "cancel").exists():
            return "cancelled"
        pane = meta.get("main_pane")
        if not pane or not tmux_target_alive(pane, meta.get("tmux_identity")):
            return "target_lost"
        try:
            result = tmux_run(["send-keys", "-t", pane, "Enter"], capture=True)
        except (OSError, subprocess.TimeoutExpired):
            return "failed"
        return "sent" if result.returncode == 0 else "failed"


def log_event(run_id, kind, **fields):
    record = event_record(kind, run_id, **fields)
    append_jsonl(run_dir(run_id) / "events.jsonl", record)


def active_interactive_runs(exclude=None):
    result = []
    if not RUNS_DIR.exists():
        return result
    for path in RUNS_DIR.iterdir():
        if not path.is_dir() or path.name == exclude:
            continue
        meta = read_json(path / "meta.json", {})
        if meta.get("mode") == "interactive" and run_is_live(path.name, meta):
            result.append((path.name, meta))
    return result


def tmux_skip_key(binding, tmux_session=None):
    if not binding or not tmux_session:
        return None
    try:
        result = tmux_run(
            ["show-options", "-v", "-t", tmux_session, "prefix"],
            capture=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    prefix = (result.stdout or "").strip() if result.returncode == 0 else ""
    if not prefix or prefix.lower() == "none":
        return None
    return "{} X".format(prefix)


def prefix_x_binding():
    result = tmux_run(["list-keys", "-T", "prefix"], capture=True)
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        if re.match(r"^bind-key\s+(?:-r\s+)?-T\s+prefix\s+X(?:\s|$)", line):
            return line
    return ""


def our_binding_present():
    binding = prefix_x_binding()
    return bool(binding and "cancel-target" in binding and str(SCRIPT) in binding)


def release_tmux_binding(run_id):
    if not BINDING_OWNER.exists() or not TMUX.exists():
        return
    with global_lock():
        if active_interactive_runs(exclude=run_id):
            return
        if our_binding_present():
            tmux_run(["unbind-key", "-T", "prefix", "X"], capture=True)
        try:
            BINDING_OWNER.unlink()
        except FileNotFoundError:
            pass


def cleanup_run(run_id, normal):
    directory = run_dir(run_id)
    if not directory.is_dir():
        return
    try:
        meta = get_meta(run_id)
    except ValueError:
        return
    restore_watchdog_border(meta)
    release_named_lock("session", meta.get("session_id"), run_id)
    release_named_lock("project", meta.get("project_lock"), run_id)
    update_meta(run_id, state="exited" if normal else "crashed", ended_at=time.time())
    release_tmux_binding(run_id)
    directory = run_dir(run_id)
    for socket_name in ("events", "launch"):
        try:
            ipc_socket_path(run_id, socket_name).unlink()
        except FileNotFoundError:
            pass
    for name in (
        "ready", "cancel", "paused", "pending.jsonl", "pending.processing",
        "expected-recovery.json", "expected-recovery.lock",
    ):
        try:
            (directory / name).unlink()
        except FileNotFoundError:
            pass
    if normal:
        shutil.rmtree(directory, ignore_errors=True)


def watchdog_main(run_id, display_mode):
    directory = run_dir(run_id)
    meta = get_meta(run_id)
    update_meta(
        run_id,
        watchdog_pid=os.getpid(),
        watchdog_identity=process_identity(os.getpid()),
        state="running",
    )
    socket_path = ipc_socket_path(run_id, "events")
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.settimeout(0.25)
    (directory / "ready").touch(mode=0o600, exist_ok=True)
    binding = bool(meta.get("cancel_binding"))
    skip_key = tmux_skip_key(binding, meta.get("tmux_session"))
    retry_count = 0
    retry_updated_at = time.time()
    pending = None
    submission = None
    deferred_failures = []
    recovery_suppressed = False
    seen = set()
    got_session_end = False
    lock_blocked = False
    current_state = "ready"
    write_status(run_id, "ready", retry_count=0)
    last_render = 0.0
    rendered_frame = None
    loaded_fingerprint = script_fingerprint()
    next_update_check = time.monotonic() + UPDATE_CHECK_INTERVAL
    pane_dead_since = None
    terminal_seen = None
    terminal_candidate = None
    terminal_fallback_at = None
    terminal_fallback_observation = None
    terminal_hook_at = None
    terminal_hook_category = None
    terminal_fired_at = None
    terminal_fired_category = None
    next_terminal_poll = time.monotonic()

    def handle(event, deferred=False):
        nonlocal retry_count, retry_updated_at, pending, submission, deferred_failures, recovery_suppressed, got_session_end, lock_blocked, current_state, meta, terminal_fallback_at, terminal_fallback_observation, terminal_hook_at, terminal_hook_category, terminal_fired_at, terminal_fired_category
        kind = event.get("kind")
        if kind == "lock_blocked" and event.get("run_id") != run_id:
            return
        category = event.get("category")
        terminal_source = event.get("source") == "terminal_fallback"
        if (
            kind == "recoverable_failure"
            and not terminal_source
            and not event.get("subagent")
            and terminal_fired_category == category
            and terminal_fired_at is not None
            and time.monotonic() - terminal_fired_at <= TERMINAL_HOOK_DEDUP_WINDOW
        ):
            log_event(run_id, "terminal_fallback_hook_deduplicated", category=category)
            return
        if kind in {"recoverable_failure", "unsupported_failure"} and not event.get("subagent"):
            terminal_fallback_at = None
            terminal_fallback_observation = None
            terminal_hook_at = time.monotonic()
            terminal_hook_category = category
        if terminal_source:
            terminal_fired_at = time.monotonic()
            terminal_fired_category = category
        meta = get_meta(run_id)
        if kind == "session_start":
            log_event(run_id, kind)
            return
        if kind == "lock_blocked":
            # This is state only.  Datagram contents must never close panes.
            lock_blocked = True
            current_state = "blocked"
            pending = None
            submission = None
            deferred_failures = []
            log_event(run_id, kind)
            write_status(run_id, current_state, retry_count=retry_count)
            return
        if kind == "session_end":
            # A hook cannot prove that it represents the root conversation.
            # The direct child waiter in pane_run_main owns normal teardown.
            log_event(run_id, "session_end_observed", subagent=bool(event.get("subagent")))
            return
        if kind == "turn_success":
            completed_recovery = current_state == "awaiting" and retry_count > 0
            completed_count = retry_count
            retry_count = 0
            retry_updated_at = time.time()
            pending = None
            submission = None
            deferred_failures = []
            recovery_suppressed = False
            clear_expected_recovery(run_id)
            current_state = "completed" if completed_recovery else "ready"
            log_event(run_id, kind)
            write_status(
                run_id,
                current_state,
                retry_count=completed_count if completed_recovery else 0,
                feedback_until=time.time() + RESULT_FEEDBACK_SECONDS if completed_recovery else None,
            )
            return
        if kind == "unsupported_failure":
            if event.get("subagent"):
                log_event(run_id, "subagent_failure_observed", category="unsupported")
                return
            pending = None
            submission = None
            deferred_failures = []
            clear_expected_recovery(run_id)
            current_state = "unsupported"
            log_event(run_id, kind)
            write_status(run_id, current_state, retry_count=retry_count)
            return
        if kind == "prompt_submit":
            if event.get("recovery"):
                submission = None
                log_event(run_id, "recovery_prompt_submitted", retry_count=retry_count)
                if current_state in {"submitting", "unconfirmed"}:
                    current_state = "awaiting"
                    if deferred_failures:
                        handle(deferred_failures.pop(0), deferred=True)
                    else:
                        write_status(run_id, current_state, retry_count=retry_count)
            else:
                retry_count = 0
                retry_updated_at = time.time()
                pending = None
                submission = None
                deferred_failures = []
                recovery_suppressed = False
                clear_expected_recovery(run_id)
                current_state = "ready"
                log_event(run_id, "manual_prompt_cancelled_recovery")
                write_status(run_id, current_state, retry_count=0)
            return
        if kind != "recoverable_failure":
            return
        category = event.get("category")
        if event.get("subagent"):
            log_event(run_id, "subagent_failure_observed", category=category)
            return
        if recovery_suppressed:
            log_event(run_id, "recovery_failure_suppressed", category=category)
            return
        key = "{}:{}:{}".format(event.get("session_id"), event.get("prompt_id"), category)
        if not deferred:
            if key in seen:
                return
            seen.add(key)
        if current_state in {"submitting", "unconfirmed"}:
            deferred_failures.append(event)
            log_event(run_id, "recovery_failure_deferred", category=category)
            return
        submission = None
        age = time.time() - float(event.get("at") or 0)
        if age > EVENT_MAX_AGE:
            pending = None
            current_state = "stale"
            log_event(run_id, "recovery_stale", category=category)
            write_status(run_id, current_state, retry_count=retry_count)
            return
        if is_paused(run_id):
            pending = None
            current_state = "paused"
            log_event(run_id, "recovery_paused", category=category)
            write_status(run_id, current_state, retry_count=retry_count)
            return
        if retry_count and time.time() - retry_updated_at > RETRY_STATE_EXPIRY:
            retry_count = 0
        if retry_count >= MAX_RECOVERIES:
            pending = None
            current_state = "exhausted"
            log_event(run_id, "recovery_exhausted", category=category, retry_count=retry_count)
            write_status(run_id, current_state, retry_count=retry_count)
            return
        delay = ERROR_POLICIES[category]["delays"][retry_count]
        retry_count += 1
        retry_updated_at = time.time()
        pending = {
            "category": category,
            "deadline": time.monotonic() + delay,
            "created_at": time.time(),
            "number": retry_count,
        }
        current_state = "countdown"
        log_event(run_id, "recovery_scheduled", category=category, retry_count=retry_count, delay=delay)
        write_status(run_id, current_state, retry_count=retry_count, category=category, delay=delay)

    try:
        while True:
            for event in read_pending_events(directory):
                handle(event)
            try:
                raw = server.recv(65535)
                handle(json.loads(raw.decode("utf-8")))
            except socket.timeout:
                pass
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

            if not tmux_target_alive(meta.get("main_pane"), meta.get("tmux_identity")):
                if pane_dead_since is None:
                    pane_dead_since = time.monotonic()
                elif time.monotonic() - pane_dead_since >= 1:
                    break
            else:
                pane_dead_since = None
            now = time.monotonic()
            if now >= next_terminal_poll:
                next_terminal_poll = now + TERMINAL_POLL_INTERVAL
                observed = terminal_failure_observation(meta)
                if observed != terminal_seen:
                    terminal_seen = observed
                    terminal_candidate = observed
                    category = observed[0] if observed else None
                    recently_hooked = (
                        category
                        and terminal_hook_category == category
                        and terminal_hook_at is not None
                        and now - terminal_hook_at <= TERMINAL_HOOK_DEDUP_WINDOW
                    )
                    terminal_fallback_at = (
                        now + TERMINAL_FAILURE_GRACE
                        if observed and not recently_hooked
                        else None
                    )
                    terminal_fallback_observation = (
                        observed if not recently_hooked else None
                    )
            if (
                terminal_fallback_at is not None
                and now >= terminal_fallback_at
                and terminal_candidate == terminal_fallback_observation
            ):
                observation = terminal_fallback_observation
                category = observation[0]
                terminal_fallback_at = None
                terminal_fallback_observation = None
                handle(
                    event_record(
                        "recoverable_failure",
                        run_id,
                        session_id=meta.get("session_id") or "unknown",
                        prompt_id="terminal-{}".format(time.time_ns()),
                        category=category,
                        subagent=False,
                        source="terminal_fallback",
                    )
                )
            cancel_requested = (directory / "cancel").exists()
            if cancel_requested and current_state == "countdown" and pending:
                consume_cancel(run_id)
                log_event(run_id, "recovery_skipped", retry_count=retry_count)
                pending = None
                deferred_failures = []
                clear_expected_recovery(run_id)
                current_state = "skipped"
                write_status(
                    run_id,
                    current_state,
                    retry_count=retry_count,
                    feedback_until=time.time() + RESULT_FEEDBACK_SECONDS,
                )
            elif cancel_requested and submission:
                consume_cancel(run_id)
                log_event(run_id, "client_interrupt_cancelled_recovery")
                submission = None
                deferred_failures = []
                clear_expected_recovery(run_id)
                recovery_suppressed = True
                current_state = "cancelled"
                write_status(run_id, current_state, retry_count=retry_count)
            paused_now = is_paused(run_id)
            paused_state = "paused_global" if GLOBAL_PAUSE.exists() else "paused"
            if paused_now and current_state not in {"paused", "paused_global"}:
                if pending or submission or deferred_failures:
                    log_event(run_id, "recovery_paused", retry_count=retry_count)
                recovery_suppressed = (
                    recovery_suppressed
                    or bool(submission)
                    or current_state in {"unconfirmed", "awaiting"}
                )
                pending = None
                submission = None
                deferred_failures = []
                clear_expected_recovery(run_id)
                current_state = paused_state
                write_status(run_id, current_state, retry_count=retry_count)
            elif paused_now and current_state != paused_state:
                current_state = paused_state
                write_status(run_id, current_state, retry_count=retry_count)
            elif current_state in {"paused", "paused_global"} and not paused_now:
                retry_count = 0
                retry_updated_at = time.time()
                current_state = "ready"
                write_status(run_id, current_state, retry_count=0)
            if current_state in {"completed", "skipped"}:
                status = read_json(directory / "status.json", {})
                if time.time() >= float(status.get("feedback_until") or 0):
                    current_state = "ready"
                    write_status(run_id, current_state, retry_count=0)
            now = time.monotonic()
            if current_state in {"ready", "update_available"} and now >= next_update_check:
                current_fingerprint = script_fingerprint()
                next_update_check = now + UPDATE_CHECK_INTERVAL
                if (
                    loaded_fingerprint
                    and current_fingerprint
                    and current_fingerprint != loaded_fingerprint
                    and current_state != "update_available"
                ):
                    current_state = "update_available"
                    write_status(run_id, current_state, retry_count=0)
            if pending and time.time() - pending["created_at"] > EVENT_MAX_AGE:
                pending = None
                current_state = "stale"
                log_event(run_id, "recovery_stale", retry_count=retry_count)
                write_status(run_id, current_state, retry_count=retry_count)
            if pending and time.monotonic() >= pending["deadline"]:
                message = recovery_message(pending["number"], pending["category"])
                with recovery_control_lock(run_id):
                    if (directory / "cancel").exists():
                        consume_cancel(run_id)
                        submitted_at = False
                        current_state = "skipped"
                        log_event(run_id, "recovery_skipped", retry_count=retry_count)
                        write_status(
                            run_id,
                            current_state,
                            retry_count=retry_count,
                            feedback_until=time.time() + RESULT_FEEDBACK_SECONDS,
                        )
                    elif is_paused(run_id):
                        submitted_at = False
                        current_state = (
                            "paused_global" if GLOBAL_PAUSE.exists() else "paused"
                        )
                        log_event(run_id, "recovery_paused", retry_count=retry_count)
                        write_status(run_id, current_state, retry_count=retry_count)
                    else:
                        current_state = "submitting"
                        write_status(
                            run_id,
                            current_state,
                            retry_count=retry_count,
                            phase="initial_wait",
                            sent_attempts=0,
                            failed_attempts=0,
                        )
                        submitted_at = inject_recovery(meta, message, run_id)
                if submitted_at is not False:
                    log_event(
                        run_id,
                        "recovery_submit_attempt",
                        attempt="initial",
                        result="sent",
                        category=pending["category"],
                        retry_count=pending["number"],
                    )
                    if isinstance(submitted_at, bool):
                        submitted_at = time.monotonic()
                    submission = {
                        "quick_deadline": submitted_at + SUBMIT_RETRY_DELAY,
                        "final_deadline": submitted_at + SUBMISSION_ACK_TIMEOUT,
                        "ack_deadline": None,
                        "number": pending["number"],
                        "phase": "initial_wait",
                        "quick_done": False,
                        "final_done": False,
                        "sent_attempts": 1,
                        "failed_attempts": 0,
                    }
                    write_status(
                        run_id,
                        current_state,
                        retry_count=retry_count,
                        phase=submission["phase"],
                        sent_attempts=1,
                        failed_attempts=0,
                    )
                elif current_state == "submitting":
                    log_event(run_id, "recovery_injection_failed", retry_count=pending["number"])
                    current_state = "stale"
                    write_status(run_id, current_state, retry_count=retry_count)
                pending = None
            now = time.monotonic()
            if submission and not submission["quick_done"] and now >= submission["quick_deadline"]:
                result = send_recovery_submit_key(meta, run_id)
                submission["quick_done"] = True
                if result == "sent":
                    submission["sent_attempts"] += 1
                    submission["phase"] = "quick_retry_sent"
                elif result == "failed":
                    submission["failed_attempts"] += 1
                    submission["phase"] = "quick_retry_failed"
                elif result == "consumed":
                    submission["phase"] = "syncing"
                elif result == "target_lost":
                    submission["phase"] = "target_lost"
                    submission["final_done"] = True
                elif result in {"paused", "cancelled"}:
                    if result == "cancelled":
                        consume_cancel(run_id)
                    submission = None
                    current_state = result
                log_event(
                    run_id,
                    "recovery_submit_attempt",
                    attempt="quick_retry",
                    result=result,
                    retry_count=retry_count,
                )
                if submission:
                    write_status(
                        run_id,
                        current_state,
                        retry_count=retry_count,
                        phase=submission["phase"],
                        sent_attempts=submission["sent_attempts"],
                        failed_attempts=submission["failed_attempts"],
                    )
                else:
                    clear_expected_recovery(run_id)
                    write_status(run_id, current_state, retry_count=retry_count)
            if (
                submission
                and submission["phase"] not in {"syncing", "target_lost", "final_failed"}
                and not submission["final_done"]
                and now >= submission["final_deadline"]
            ):
                result = send_recovery_submit_key(meta, run_id)
                submission["final_done"] = True
                if result == "sent":
                    submission["sent_attempts"] += 1
                    submission["phase"] = "final_retry_sent"
                    submission["ack_deadline"] = time.monotonic() + SUBMISSION_ACK_TIMEOUT
                elif result == "consumed":
                    submission["phase"] = "syncing"
                elif result == "target_lost":
                    submission["phase"] = "target_lost"
                elif result in {"paused", "cancelled"}:
                    if result == "cancelled":
                        consume_cancel(run_id)
                    submission = None
                    current_state = result
                else:
                    submission["failed_attempts"] += 1
                    submission["phase"] = "final_failed"
                log_event(
                    run_id,
                    "recovery_submit_attempt",
                    attempt="final_retry",
                    result=result,
                    retry_count=retry_count,
                )
                if submission:
                    write_status(
                        run_id,
                        current_state,
                        retry_count=retry_count,
                        phase=submission["phase"],
                        sent_attempts=submission["sent_attempts"],
                        failed_attempts=submission["failed_attempts"],
                    )
                else:
                    clear_expected_recovery(run_id)
                    write_status(run_id, current_state, retry_count=retry_count)
            if submission and submission["ack_deadline"] is not None and now >= submission["ack_deadline"]:
                log_event(
                    run_id,
                    "recovery_submit_unconfirmed",
                    retry_count=submission["number"],
                    sent_attempts=submission["sent_attempts"],
                    failed_attempts=submission["failed_attempts"],
                )
                sent_attempts = submission["sent_attempts"]
                failed_attempts = submission["failed_attempts"]
                submission = None
                current_state = "unconfirmed"
                write_status(
                    run_id,
                    current_state,
                    retry_count=retry_count,
                    sent_attempts=sent_attempts,
                    failed_attempts=failed_attempts,
                )
            now = time.monotonic()
            if now - last_render >= 0.25:
                if current_state == "countdown" and pending:
                    text = status_text(
                        current_state,
                        meta,
                        retry_count,
                        pending["category"],
                        pending["deadline"] - now,
                        binding,
                        skip_key=skip_key,
                    )
                elif current_state == "submitting" and submission:
                    if submission["phase"] in {"initial_wait", "quick_retry_sent", "quick_retry_failed"}:
                        remaining = submission["final_deadline"] - now
                    elif submission["phase"] == "final_retry_sent":
                        remaining = submission["ack_deadline"] - now
                    else:
                        remaining = None
                    text = status_text(
                        current_state,
                        meta,
                        retry_count,
                        remaining=remaining,
                        binding=binding,
                        phase=submission["phase"],
                        sent_attempts=submission["sent_attempts"],
                        failed_attempts=submission["failed_attempts"],
                    )
                elif current_state == "unconfirmed":
                    status = read_json(directory / "status.json", {})
                    text = status_text(
                        current_state,
                        meta,
                        retry_count,
                        binding=binding,
                        sent_attempts=status.get("sent_attempts", 0),
                        failed_attempts=status.get("failed_attempts", 0),
                    )
                else:
                    text = status_text(current_state, meta, retry_count, binding=binding)
                rendered_frame = render_watchdog_status(
                    current_state, text, display_mode, meta, rendered_frame
                )
                last_render = now
    finally:
        server.close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        final_meta = get_meta(run_id)
        # The root wrapper and owner monitor preserve lifecycle state until the
        # outer launcher confirms the exact tmux target disappeared.
        if final_meta.get("state") not in {"root_exited", "ending", "end_cleanup_failed"}:
            cleanup_run(run_id, False)
    return 0


def receive_launch_spec(run_id):
    socket_path = ipc_socket_path(run_id, "launch")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + 12
    try:
        while True:
            try:
                client.connect(str(socket_path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("launch specification channel was not available")
                time.sleep(0.05)
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(map(len, chunks)) > 16 * 1024 * 1024:
                raise ValueError("launch specification is too large")
        args = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError("invalid launch specification")
        return args
    finally:
        client.close()


def start_launch_server(run_id, claude_args):
    directory = run_dir(run_id)
    socket_path = ipc_socket_path(run_id, "launch")
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    payload = json.dumps(list(claude_args), ensure_ascii=True).encode("utf-8")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.listen(1)
    server.settimeout(15)
    done = threading.Event()

    def serve():
        nonlocal payload
        try:
            connection, _ = server.accept()
            with connection:
                connection.sendall(payload)
        except OSError:
            pass
        finally:
            payload = b""
            server.close()
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass
            done.set()

    thread = threading.Thread(target=serve, name="claude-auto-launch", daemon=True)
    thread.start()
    return server, done


def pane_run_main(run_id):
    """Run and wait for the root raw Claude process in its own tmux pane."""
    directory = run_dir(run_id)
    try:
        claude_args = receive_launch_spec(run_id)
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print("claude-auto: cannot receive launch specification: {}".format(exc), file=sys.stderr)
        return 1
    deadline = time.monotonic() + 12
    while not (directory / "ready").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not (directory / "ready").exists():
        print("claude-auto: recovery watchdog did not become ready", file=sys.stderr)
        return 1
    env = os.environ.copy()
    env["CLAUDE_AUTO_RUN_ID"] = run_id
    env["CLAUDE_AUTO_MANAGED"] = "interactive"
    env.pop("CLAUDE_AUTO_DISABLED", None)
    try:
        root = subprocess.Popen([str(RAW_CLAUDE)] + claude_args, env=env)
        update_meta(run_id, root_child_pid=root.pid, state="running")
        code = root.wait()
    except OSError as exc:
        print("claude-auto: cannot start raw Claude: {}".format(exc), file=sys.stderr)
        code = 127
    except KeyboardInterrupt:
        try:
            root.send_signal(signal.SIGINT)
            code = root.wait(timeout=5)
        except (UnboundLocalError, OSError, subprocess.TimeoutExpired):
            code = 130
    # This process, and only this process, waited for the direct root child.
    # It records that fact but does not destroy the tmux resource containing
    # itself; a session-external owner monitor performs the verified close.
    update_meta(
        run_id,
        state="root_exited",
        root_exited_at=time.time(),
        root_exit_code=code,
    )
    return code


def owner_monitor_main(run_id):
    """Close this run's tagged tmux target after the direct root child exits."""
    directory = run_dir(run_id)
    deadline = time.monotonic() + 7 * 24 * 60 * 60
    while directory.exists() and time.monotonic() < deadline:
        meta = get_meta(run_id)
        state = meta.get("state")
        if state == "root_exited":
            return 0 if close_ended_owned_target(run_id) else 1
        if state in {"end_cleanup_failed", "exited", "crashed"}:
            return 0 if state == "exited" else 1
        time.sleep(0.05)
    return 0 if not directory.exists() else 1


def launch_owner_monitor(run_id):
    monitor = subprocess.Popen(
        [str(PYTHON), str(SCRIPT), "owner-monitor", run_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    update_meta(
        run_id,
        owner_monitor_pid=monitor.pid,
        owner_monitor_identity=process_identity(monitor.pid),
    )
    return monitor


def sanitize_name(value):
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value)
    value = re.sub(r"-+", "-", value).strip("-")
    return (value or "claude")[:40]


def unique_tmux_name(base):
    name = base[:64]
    index = 2
    active_names = {meta.get("name") for _, meta in active_interactive_runs()}
    while (
        tmux_run(["has-session", "-t", "=" + name], capture=True).returncode == 0
        or name in active_names
    ):
        suffix = "-{}".format(index)
        name = base[: 64 - len(suffix)] + suffix
        index += 1
    return name


def parse_session_request(args):
    session_id = None
    needs_resolution = False
    fork = has_option(args, {"--fork-session"})
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            break
        if token.startswith("--session-id="):
            session_id = token.split("=", 1)[1]
        elif token == "--session-id" and index + 1 < len(args):
            session_id = args[index + 1]
            index += 1
        elif token in {"-c", "--continue"}:
            needs_resolution = True
        elif token.startswith("--resume="):
            value = token.split("=", 1)[1]
            if re.fullmatch(r"[0-9a-fA-F-]{36}", value):
                session_id = value
            else:
                needs_resolution = True
        elif token in {"-r", "--resume"}:
            if index + 1 < len(args) and not args[index + 1].startswith("-"):
                value = args[index + 1]
                index += 1
                if re.fullmatch(r"[0-9a-fA-F-]{36}", value):
                    session_id = value
                else:
                    needs_resolution = True
            else:
                needs_resolution = True
        index += 1
    if fork:
        session_id = None
        needs_resolution = True
    return session_id, needs_resolution


def add_managed_session_id(args):
    session_id, needs_resolution = parse_session_request(args)
    if session_id or needs_resolution:
        return list(args), session_id, needs_resolution
    session_id = str(uuid.uuid4())
    return ["--session-id", session_id] + list(args), session_id, False


def acquire_initial_locks(run_id, session_id, needs_resolution, cwd):
    project_lock = None
    if session_id:
        ok, owner = acquire_named_lock("session", session_id, run_id)
        if not ok:
            return False, "session is already managed by {}".format(owner), None
    elif needs_resolution:
        project_lock = canonical_project(cwd)
        ok, owner = acquire_named_lock("project", project_lock, run_id)
        if not ok:
            return False, "another --continue/--resume is resolving in this project ({})".format(owner), None
    return True, None, project_lock


def ensure_cancel_binding():
    if not TMUX.exists():
        return False
    existing = prefix_x_binding()
    if existing is None:
        return False
    if existing:
        return our_binding_present()
    command = "{} {} cancel-target '#{{session_name}}' '#{{window_id}}'".format(
        shlex.quote(str(PYTHON)), shlex.quote(str(SCRIPT))
    )
    result = tmux_run(["bind-key", "-T", "prefix", "X", "run-shell", command], capture=True)
    if result.returncode == 0 and our_binding_present():
        atomic_json(BINDING_OWNER, {"installed_at": time.time(), "version": VERSION})
        return True
    return False


def launch_watchdog(run_id, display_mode, main_pane, cwd):
    if display_mode == "pane":
        watchdog_command = shlex.join(
            [str(PYTHON), str(SCRIPT), "watchdog", run_id, display_mode]
        )
        watchdog_pane = tmux_run(
            [
                "split-window", "-d", "-v", "-l", "1", "-P", "-F",
                "#{pane_id}", "-t", main_pane, "-c", cwd, watchdog_command,
            ],
            check=True,
        ).stdout.strip()
        update_meta(
            run_id,
            watchdog_pane=watchdog_pane,
            watchdog_pane_identity=tmux_target_identity(watchdog_pane),
        )
        return watchdog_pane
    watchdog = subprocess.Popen(
        [str(PYTHON), str(SCRIPT), "watchdog", run_id, display_mode],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    update_meta(
        run_id,
        watchdog_pid=watchdog.pid,
        watchdog_identity=process_identity(watchdog.pid),
        watchdog_pane=None,
    )
    return None


TMUX_OWNER_OPTION = "@claude_auto_owner_tag"
TMUX_OWNER_NAME_PREFIX = "claude-auto-"
TMUX_OWNER_NAME_HEX_LENGTH = 48


def tmux_server_epoch():
    """Return a locale-independent tmux server generation marker."""
    try:
        result = tmux_run(
            ["display-message", "-p", "#{pid}\t#{start_time}\t#{socket_path}"],
            capture=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    fields = (result.stdout or "").rstrip("\n").split("\t")
    if (
        result.returncode != 0
        or len(fields) != 3
        or not fields[0].isdigit()
        or not fields[1].isdigit()
        or not fields[2]
        or any("\x00" in field for field in fields)
    ):
        return None
    return "\t".join(fields)


def tmux_server_epoch_matches(meta):
    expected = meta.get("tmux_server_epoch")
    return isinstance(expected, str) and bool(expected) and tmux_server_epoch() == expected


def tmux_server_generation_gone(meta):
    expected = meta.get("tmux_server_epoch")
    if not isinstance(expected, str):
        return False
    fields = expected.split("\t")
    if len(fields) != 3 or not fields[0].isdigit() or not fields[2]:
        return False
    pid = int(fields[0])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # tmux can leave its Unix socket path behind after the server exits.
        # The recorded server PID being gone is sufficient to prove that this
        # generation, and therefore every resource it owned, has ended.
        return True
    except (PermissionError, OSError):
        return False
    return False


def wait_for_tmux_server_generation_gone(meta, timeout=2.0):
    deadline = time.monotonic() + timeout
    while True:
        if tmux_server_generation_gone(meta):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def tmux_owner_target_name(owner_tag):
    if not isinstance(owner_tag, str) or not re.fullmatch(r"[0-9a-f]{64}", owner_tag):
        raise ValueError("invalid tmux owner tag")
    # The name is an unguessable 192-bit prefix; the complete tag is checked in
    # the tmux user option before any destructive command.
    return TMUX_OWNER_NAME_PREFIX + owner_tag[:TMUX_OWNER_NAME_HEX_LENGTH]


def tmux_owner_bridge_name(owner_tag):
    return tmux_owner_target_name(owner_tag) + "-bridge"


def tmux_exact_session_target(session_name):
    if not isinstance(session_name, str) or not session_name or ":" in session_name:
        return None
    return "=" + session_name


def tmux_exact_window_target(session_name, window_name):
    if (
        not isinstance(session_name, str) or not session_name or ":" in session_name
        or not isinstance(window_name, str) or not window_name or ":" in window_name
    ):
        return None
    return "={}:={}".format(session_name, window_name)


def owned_window_link_target(meta):
    if meta.get("owned_target") != "window":
        return None
    try:
        if meta.get("tmux_window_name") != tmux_owner_target_name(meta.get("tmux_owner_tag")):
            return None
    except ValueError:
        return None
    target = tmux_exact_window_target(meta.get("tmux_host_session_name"), meta.get("tmux_window_name"))
    return target if target and meta.get("owned_window_link") == target else None


def legacy_tmux_metadata(meta):
    return bool(
        meta.get("owned_target") in {"session", "window"}
        and (not meta.get("tmux_owner_tag") or not meta.get("tmux_server_epoch"))
    )


def owned_tmux_target_identity(meta):
    kind = meta.get("owned_target")
    session_id = meta.get("tmux_session_id")
    window_id = meta.get("window_id")
    owner_tag = meta.get("tmux_owner_tag")
    target_name = meta.get("tmux_window_name")
    if kind not in {"session", "window"} or not all(
        isinstance(value, str) and value for value in (session_id, window_id, owner_tag, target_name)
    ):
        return None
    try:
        if target_name != tmux_owner_target_name(owner_tag):
            return None
    except ValueError:
        return None
    if kind == "session":
        session_name = meta.get("tmux_session_name")
        if session_name != target_name or not tmux_exact_session_target(session_name):
            return None
        return kind, session_id, window_id, session_name, target_name, owner_tag
    host = meta.get("tmux_host_session_name")
    if not owned_window_link_target(meta):
        return None
    return kind, session_id, window_id, host, target_name, owner_tag


def parse_tmux_records(result, fields, optional_empty_fields=()):
    """Parse an all-object listing without rejecting unrelated empty tags."""
    if result.returncode != 0:
        return None
    optional = set(optional_empty_fields)
    if any(not isinstance(index, int) or index < 0 or index >= fields for index in optional):
        return None
    records = set()
    for line in (result.stdout or "").splitlines():
        record = tuple(line.split("\t"))
        if len(record) != fields or any(not value for index, value in enumerate(record) if index not in optional):
            return None
        records.add(record)
    return records


def owned_tmux_target_presence(meta):
    """Return True, False, or None for exactly this tagged generation."""
    expected = owned_tmux_target_identity(meta)
    if not expected or not tmux_server_epoch_matches(meta):
        return None
    kind, session_id, window_id, session_name, window_name, owner_tag = expected
    try:
        result = tmux_run(
            ["list-windows", "-a", "-F", "#{session_id}\t#{window_id}\t#{session_name}\t#{window_name}\t#{" + TMUX_OWNER_OPTION + "}"],
            capture=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    windows = parse_tmux_records(result, 5, optional_empty_fields=(4,))
    if windows is None:
        return None
    exact_window = (session_id, window_id, session_name, window_name, owner_tag)
    stable_windows = [record for record in windows if record[:2] == exact_window[:2]]
    if kind == "window":
        return True if exact_window in windows else (None if stable_windows else False)
    try:
        result = tmux_run(
            ["list-sessions", "-F", "#{session_id}\t#{session_name}\t#{" + TMUX_OWNER_OPTION + "}"],
            capture=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sessions = parse_tmux_records(result, 3, optional_empty_fields=(2,))
    if sessions is None:
        return None
    exact_session = (session_id, session_name, owner_tag)
    stable_sessions = [record for record in sessions if record[0] == session_id]
    if exact_session in sessions and exact_window in windows:
        return True
    return None if stable_sessions or stable_windows else False


def mark_owned_tmux_close_failure(run_id, reason="close_failed"):
    meta = get_meta(run_id)
    update_meta(
        run_id,
        state="end_cleanup_failed",
        end_close_attempts=int(meta.get("end_close_attempts") or 0),
        end_close_failed_at=time.time(),
        end_close_failure=reason,
    )
    write_status(run_id, "end_cleanup_failed", retry_count=0)


def unlink_owned_tmux_window(meta, target):
    """Remove one session link without destroying links in other sessions."""
    bridge_name = tmux_owner_bridge_name(meta.get("tmux_owner_tag"))
    created = False
    unlinked = False
    bridge_closed = False
    try:
        result = tmux_run(
            [
                "new-session", "-d", "-P", "-F", "#{session_id}",
                "-s", bridge_name, "-n", "placeholder", "sleep 5",
            ],
            capture=True,
        )
        bridge_id = (result.stdout or "").strip()
        if result.returncode != 0 or not re.fullmatch(r"\$[0-9]+", bridge_id):
            return False
        created = True
        if tmux_run(
            ["set-option", "-t", bridge_id, TMUX_OWNER_OPTION, meta.get("tmux_owner_tag")],
            capture=True,
        ).returncode != 0:
            return False
        if tmux_run(
            ["link-window", "-s", target, "-t", bridge_id + ":"],
            capture=True,
        ).returncode != 0:
            return False
        unlinked = tmux_run(
            ["unlink-window", "-t", target], capture=True
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        if created:
            try:
                bridge_closed = tmux_run(
                    ["kill-session", "-t", bridge_id], capture=True
                ).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                bridge_closed = False
    return unlinked and bridge_closed


def close_owned_tmux_target(meta):
    """Destroy one pre-verified exact target, never a resolver fallback."""
    expected = owned_tmux_target_identity(meta)
    if not expected or owned_tmux_target_presence(meta) is not True:
        return False
    if expected[0] == "session":
        target = tmux_exact_session_target(expected[3])
        command = ["kill-session", "-t", target] if target else None
        if not command:
            return False
        try:
            return tmux_run(command, capture=True).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    target = owned_window_link_target(meta)
    return bool(target) and unlink_owned_tmux_window(meta, target)


def close_ended_owned_target(run_id):
    """The external owner monitor performs normal interactive teardown."""
    meta = dict(get_meta(run_id))
    presence = owned_tmux_target_presence(meta)
    if presence is False:
        update_meta(run_id, state="ending", ending_at=time.time(), target_disappeared=True)
        return True
    if (
        presence is None
        and meta.get("state") == "root_exited"
        and meta.get("owned_target") == "session"
        and wait_for_tmux_server_generation_gone(meta)
    ):
        update_meta(
            run_id,
            state="ending",
            ending_at=time.time(),
            target_disappeared=True,
            end_close_succeeded=True,
            end_close_generation=meta.get("tmux_server_epoch"),
        )
        return True
    if presence is not True:
        mark_owned_tmux_close_failure(
            run_id, "legacy_metadata_refused" if legacy_tmux_metadata(meta) else "target_unverified"
        )
        return False
    attempts = int(meta.get("end_close_attempts") or 0) + 1
    update_meta(
        run_id,
        state="ending",
        ending_at=time.time(),
        end_close_attempts=attempts,
        end_close_started_generation=meta.get("tmux_server_epoch"),
    )
    if not close_owned_tmux_target(meta):
        mark_owned_tmux_close_failure(run_id, "close_command_failed")
        return False
    # A standalone kill can take down the entire tmux server immediately after
    # this exact command.  The recorded matching generation makes that success
    # usable by the outer launcher without probing a newly started server.
    update_meta(
        run_id,
        state="ending",
        end_close_succeeded=True,
        end_close_generation=meta.get("tmux_server_epoch"),
        end_close_succeeded_at=time.time(),
    )
    return True


def successful_final_session_close(meta):
    if meta.get("owned_target") != "session":
        return False
    generation = meta.get("tmux_server_epoch")
    if meta.get("end_close_succeeded") is True:
        return meta.get("end_close_generation") == generation
    return (
        meta.get("state") == "ending"
        and meta.get("end_close_started_generation") == generation
        and tmux_server_generation_gone(meta)
    )


def observe_owned_target_disappearance(run_id, wait=False, outcome=None):
    """Let the outer launcher clean state only after exact disappearance."""
    deadline = None if wait else time.monotonic() + 3
    owner_transition_deadline = None
    while True:
        directory = run_dir(run_id)
        if not directory.exists():
            return True
        meta = get_meta(run_id)
        presence = owned_tmux_target_presence(meta)
        if presence is False or (presence is None and successful_final_session_close(meta)):
            if outcome is not None:
                outcome["root_exit_code"] = meta.get("root_exit_code")
            cleanup_run(run_id, True)
            return True
        state = meta.get("state")
        if state == "end_cleanup_failed":
            return False
        now = time.monotonic()
        if state in {"root_exited", "ending"}:
            if owner_transition_deadline is None:
                owner_transition_deadline = now + 3
            if now >= owner_transition_deadline:
                mark_owned_tmux_close_failure(run_id, "owner_monitor_timeout")
                return False
            # The external owner monitor may be between observing the root
            # exit, closing the exact target, and persisting its result.
            time.sleep(0.05)
            continue
        if presence is None:
            mark_owned_tmux_close_failure(
                run_id, "legacy_metadata_refused" if legacy_tmux_metadata(meta) else "target_unverified"
            )
            return False
        if deadline is not None and now >= deadline:
            # An attach/detach is not authority to close a still-live session.
            return None
        time.sleep(0.1)


def finish_owned_tmux_target(run_id):
    """Explicit public retry for a retained failed close, never IPC-driven."""
    if not run_dir(run_id).exists():
        return True
    meta = dict(get_meta(run_id))
    presence = owned_tmux_target_presence(meta)
    if presence is False:
        cleanup_run(run_id, True)
        return True
    if presence is not True:
        mark_owned_tmux_close_failure(
            run_id, "legacy_metadata_refused" if legacy_tmux_metadata(meta) else "target_unverified"
        )
        return False
    update_meta(
        run_id,
        state="ending",
        ending_at=time.time(),
        end_close_attempts=int(meta.get("end_close_attempts") or 0) + 1,
    )
    if not close_owned_tmux_target(meta):
        mark_owned_tmux_close_failure(run_id, "close_command_failed")
        return False
    update_meta(
        run_id,
        end_close_succeeded=True,
        end_close_generation=meta.get("tmux_server_epoch"),
    )
    observed = observe_owned_target_disappearance(run_id, wait=False)
    if observed is True:
        return True
    mark_owned_tmux_close_failure(run_id, "target_still_present")
    return False


def attach_managed_session(run_id, tmux_session):
    try:
        return tmux_run(
            ["attach-session", "-t", tmux_session],
            capture=False,
            timeout=7 * 24 * 60 * 60,
        ).returncode
    except KeyboardInterrupt:
        directory = run_dir(run_id)
        state = read_json(directory / "status.json", {}).get("state")
        if state in {"countdown", "submitting", "unconfirmed"}:
            with recovery_control_lock(run_id):
                state = read_json(directory / "status.json", {}).get("state")
                if state in {"countdown", "submitting", "unconfirmed"}:
                    (directory / "cancel").touch(mode=0o600, exist_ok=True)
                    log_event(run_id, "client_interrupt_requested_recovery_cancel")
        return 130


def interactive_main(args, lifecycle=None):
    lifecycle = lifecycle or lifecycle_lock()
    if not SCRIPT.exists():
        lifecycle.close()
        raw_exec(args, disabled=False)
    ensure_dirs()
    cleanup_stale()
    if not TMUX.exists():
        lifecycle.close()
        print("claude-auto: tmux is not installed at {}".format(TMUX), file=sys.stderr)
        return 127
    args, session_id, needs_resolution = add_managed_session_id(args)
    run_id = uuid.uuid4().hex
    cwd = canonical_project()
    project_name = sanitize_name(Path(cwd).name or "home")
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    requested = os.environ.pop("CLAUDE_AUTO_NAME", None)
    display_name = sanitize_name(requested) if requested else "{}-{}".format(project_name, timestamp)
    display_name = unique_tmux_name(display_name)
    owner_tag = secrets.token_hex(32)
    owner_name = tmux_owner_target_name(owner_tag)
    directory = run_dir(run_id)
    directory.mkdir(parents=True, mode=0o700)
    ok, reason, project_lock = acquire_initial_locks(run_id, session_id, needs_resolution, cwd)
    if not ok:
        shutil.rmtree(directory, ignore_errors=True)
        lifecycle.close()
        print("claude-auto: {}".format(reason), file=sys.stderr)
        return 73
    atomic_json(
        directory / "meta.json",
        {
            "version": VERSION,
            "run_id": run_id,
            "name": display_name,
            "mode": "interactive",
            "cwd": cwd,
            "session_id": session_id,
            "project_lock": project_lock,
            "state": "starting",
            "created_at": time.time(),
            "supervisor_pid": os.getpid(),
            "supervisor_identity": process_identity(os.getpid()),
        },
    )
    lifecycle.close()
    columns, lines = shutil.get_terminal_size((100, 24))
    inside_tmux = bool(os.environ.get("TMUX"))
    created_session = None
    created_window = None
    launch_server = None
    launch_done = None
    try:
        launch_server, launch_done = start_launch_server(run_id, args)
        pane_command = shlex.join([str(PYTHON), str(SCRIPT), "pane-run", run_id])
        if inside_tmux:
            host_session = tmux_run(["display-message", "-p", "#{session_name}"], check=True).stdout.strip()
            host_target = tmux_exact_session_target(host_session)
            if not host_target:
                raise OSError("invalid exact host tmux target")
            created = tmux_run(
                ["new-window", "-d", "-P", "-F", "#{pane_id} #{window_id}", "-t", host_target,
                 "-n", owner_name, "-c", cwd, pane_command],
                check=True,
            ).stdout.strip().split()
            main_pane, window_id = created[0], created[1]
            tmux_session = host_session
            created_window = tmux_exact_window_target(host_session, owner_name)
            if not created_window:
                raise OSError("invalid exact nested tmux target")
        else:
            created = tmux_run(
                ["new-session", "-d", "-P", "-F", "#{pane_id} #{window_id}", "-s", owner_name, "-n", owner_name,
                 "-x", str(columns), "-y", str(lines), "-c", cwd, pane_command],
                check=True,
            ).stdout.strip().split()
            main_pane, window_id = created[0], created[1]
            created_session = owner_name
            tmux_session = owner_name
        tmux_session_id = tmux_run(["display-message", "-p", "-t", main_pane, "#{session_id}"], check=True).stdout.strip()
        epoch = tmux_server_epoch()
        if not tmux_session_id or not epoch:
            raise OSError("tmux did not report stable ownership identity")
        if inside_tmux:
            tmux_run(["set-option", "-w", "-t", window_id, TMUX_OWNER_OPTION, owner_tag], check=True)
            tmux_run(["set-option", "-w", "-t", window_id, "remain-on-exit", "on"], check=True)
        else:
            tmux_run(["set-option", "-t", tmux_session_id, TMUX_OWNER_OPTION, owner_tag], check=True)
            tmux_run(["set-option", "-w", "-t", window_id, TMUX_OWNER_OPTION, owner_tag], check=True)
        binding = ensure_cancel_binding()
        update_meta(
            run_id,
            main_pane=main_pane,
            window_id=window_id,
            tmux_session=tmux_session,
            tmux_session_name=tmux_session,
            tmux_host_session_name=host_session if inside_tmux else None,
            tmux_window_name=owner_name,
            tmux_owner_tag=owner_tag,
            tmux_server_epoch=epoch,
            tmux_session_id=tmux_session_id,
            tmux_identity=tmux_target_identity(main_pane),
            owned_target="window" if inside_tmux else "session",
            owned_window_link=created_window if inside_tmux else None,
            cancel_binding=binding,
        )
        display_mode = prepare_watchdog_display(run_id, lines)
        update_meta(run_id, watchdog_display=display_mode)
        launch_watchdog(run_id, display_mode, main_pane, cwd)
        launch_owner_monitor(run_id)
        tmux_run(["select-pane", "-t", main_pane], capture=True)
        if not launch_done.wait(timeout=15):
            raise TimeoutError("launch specification was not consumed")
        if inside_tmux:
            tmux_run(["select-window", "-t", created_window], capture=True)
            outcome = {}
            observed = observe_owned_target_disappearance(run_id, wait=True, outcome=outcome)
            if not observed:
                return 1
            root_code = outcome.get("root_exit_code")
            return root_code if isinstance(root_code, int) else 0
        attach_code = attach_managed_session(run_id, tmux_session)
        outcome = {}
        observed = observe_owned_target_disappearance(run_id, wait=False, outcome=outcome)
        if observed is False:
            return 1
        if observed is True:
            root_code = outcome.get("root_exit_code")
            if isinstance(root_code, int):
                return root_code
        return attach_code
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, TimeoutError, OSError, IndexError) as exc:
        if launch_server is not None:
            try:
                launch_server.close()
            except OSError:
                pass
        try:
            ipc_socket_path(run_id, "launch").unlink()
        except FileNotFoundError:
            pass
        # Setup failure is not normal lifecycle teardown.  The only possible
        # targets here are the freshly generated exact owner names.
        if created_session:
            tmux_run(["kill-session", "-t", tmux_exact_session_target(created_session)], capture=True)
        elif created_window:
            unlink_owned_tmux_window({"tmux_owner_tag": owner_tag}, created_window)
        print("claude-auto: failed to create managed tmux session: {}".format(exc), file=sys.stderr)
        cleanup_run(run_id, False)
        return 1


def option_value(args, option):
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            break
        if token.startswith(option + "="):
            return token.split("=", 1)[1]
        if token == option and index + 1 < len(args):
            return args[index + 1]
        index += 1
    return None


def explicit_stream_output(args):
    return option_value(args, "--output-format") == "stream-json"


def has_option(args, names):
    for token in args:
        if token == "--":
            break
        name = token.split("=", 1)[0]
        if name in names:
            return True
    return False


def first_positional(args):
    index = 0
    while index < len(args):
        token = args[index]
        name = token.split("=", 1)[0]
        if token == "--":
            return None
        if token.startswith("-"):
            if "=" in token:
                index += 1
                continue
            if name in VALUE_OPTIONS or name in REPEAT_VALUE_OPTIONS:
                index += 2
                continue
            if name in VARIADIC_OPTIONS:
                index += 1
                while index < len(args) and not args[index].startswith("-"):
                    index += 1
                continue
            if name in OPTIONAL_VALUE_OPTIONS:
                index += 1
                if index < len(args) and not args[index].startswith("-"):
                    index += 1
                continue
            if name in {"-r", "--resume"}:
                index += 1
                if index < len(args) and not args[index].startswith("-"):
                    index += 1
                continue
            index += 1
            continue
        return token
    return None


def direct_reason(args):
    option_tokens = []
    for token in args:
        if token == "--":
            break
        option_tokens.append(token)
    if any(token in DIRECT_SIMPLE_FLAGS for token in option_tokens):
        return "help/version"
    if has_option(args, DIRECT_FLAGS):
        return "special mode"
    if has_option(args, {"--max-budget-usd"}):
        return "budget preservation"
    for index, token in enumerate(option_tokens):
        if token.startswith("--input-format=") and token.split("=", 1)[1] == "stream-json":
            return "stream-json input"
        if token == "--input-format" and index + 1 < len(option_tokens) and option_tokens[index + 1] == "stream-json":
            return "stream-json input"
    positional = first_positional(args)
    if positional in MANAGEMENT_COMMANDS and not has_option(args, {"-p", "--print"}):
        return "management command"
    return None


def raw_exec(args, disabled=False):
    env = os.environ.copy()
    env.pop("CLAUDE_AUTO_RUN_ID", None)
    env.pop("CLAUDE_AUTO_MANAGED", None)
    if disabled:
        env["CLAUDE_AUTO_DISABLED"] = "1"
    else:
        env.pop("CLAUDE_AUTO_DISABLED", None)
    os.execve(str(RAW_CLAUDE), [str(RAW_CLAUDE)] + list(args), env)


def build_recovery_args(original, session_id, message):
    preserved = []
    index = 0
    while index < len(original):
        token = original[index]
        name = token.split("=", 1)[0]
        if token == "--":
            break
        if name in {"-c", "--continue", "--fork-session", "--replay-user-messages"}:
            index += 1
            continue
        if name in {"-r", "--resume", "--session-id"}:
            if "=" not in token and index + 1 < len(original) and not original[index + 1].startswith("-"):
                index += 2
            else:
                index += 1
            continue
        if not token.startswith("-"):
            index += 1
            continue
        if name in VALUE_OPTIONS or name in REPEAT_VALUE_OPTIONS:
            if "=" in token:
                preserved.append(token)
                index += 1
                continue
            if index + 1 >= len(original):
                return None
            preserved.extend(original[index:index + 2])
            index += 2
            continue
        if name in VARIADIC_OPTIONS:
            if name == "--file":
                return None
            if "=" in token:
                preserved.append(token)
                index += 1
                continue
            values = []
            index += 1
            while index < len(original) and not original[index].startswith("-"):
                values.append(original[index])
                index += 1
            if len(values) != 1:
                return None
            preserved.extend((token, values[0]))
            continue
        if name in OPTIONAL_VALUE_OPTIONS:
            preserved.append(token)
            index += 1
            if "=" not in token and index < len(original) and not original[index].startswith("-"):
                preserved.append(original[index])
                index += 1
            continue
        if name in BOOL_OPTIONS:
            if name not in {"-p", "--print", "--replay-user-messages"}:
                preserved.append(token)
            index += 1
            continue
        return None
    return preserved + ["-p", "--resume", session_id, message]


def copy_stream(source, destination):
    while True:
        chunk = source.read(65536)
        if not chunk:
            break
        destination.write(chunk)
        destination.flush()


def drain_socket(server):
    events = []
    server.setblocking(False)
    while True:
        try:
            raw = server.recv(65535)
        except BlockingIOError:
            break
        try:
            events.append(json.loads(raw.decode("utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return events


def final_recoverable_event(events):
    final = None
    session_id = None
    blocked = False
    for event in events:
        if event.get("session_id"):
            session_id = event["session_id"]
        if event.get("kind") == "lock_blocked":
            blocked = True
            final = None
        elif event.get("kind") == "turn_success":
            final = None
        elif event.get("kind") == "recoverable_failure" and not event.get("subagent"):
            final = event
    return final, session_id, blocked


def run_headless_attempt(args, run_id, server, stream, first):
    env = os.environ.copy()
    env["CLAUDE_AUTO_RUN_ID"] = run_id
    env["CLAUDE_AUTO_MANAGED"] = "headless"
    env.pop("CLAUDE_AUTO_DISABLED", None)
    stdout_store = None
    stderr_store = None
    if stream:
        stdout_dest = sys.stdout.buffer
        stderr_dest = sys.stderr.buffer
    else:
        stdout_store = tempfile.SpooledTemporaryFile(max_size=TEMP_MAX_MEMORY, mode="w+b", prefix="claude-auto-output-")
        stderr_store = tempfile.SpooledTemporaryFile(max_size=TEMP_MAX_MEMORY, mode="w+b", prefix="claude-auto-output-")
        stdout_dest = stdout_store
        stderr_dest = stderr_store
    process = subprocess.Popen(
        [str(RAW_CLAUDE)] + list(args),
        stdin=None if first else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    update_meta(run_id, child_pid=process.pid, state="running")
    out_thread = threading.Thread(target=copy_stream, args=(process.stdout, stdout_dest), daemon=True)
    err_thread = threading.Thread(target=copy_stream, args=(process.stderr, stderr_dest), daemon=True)
    out_thread.start()
    err_thread.start()
    try:
        code = process.wait()
    except KeyboardInterrupt:
        process.send_signal(signal.SIGINT)
        try:
            code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            code = process.wait()
        raise
    out_thread.join()
    err_thread.join()
    time.sleep(0.05)
    events = drain_socket(server)
    return code, events, stdout_store, stderr_store


def emit_store(store, destination):
    if store is None:
        return
    store.seek(0)
    shutil.copyfileobj(store, destination)
    destination.flush()


def headless_main(args, lifecycle=None):
    lifecycle = lifecycle or lifecycle_lock()
    if not SCRIPT.exists():
        lifecycle.close()
        raw_exec(args, disabled=False)
    ensure_dirs()
    cleanup_stale()
    args, session_id, needs_resolution = add_managed_session_id(args)
    run_id = uuid.uuid4().hex
    cwd = canonical_project()
    directory = run_dir(run_id)
    directory.mkdir(parents=True, mode=0o700)
    ok, reason, project_lock = acquire_initial_locks(run_id, session_id, needs_resolution, cwd)
    if not ok:
        shutil.rmtree(directory, ignore_errors=True)
        lifecycle.close()
        print("claude-auto: {}".format(reason), file=sys.stderr)
        return 73
    atomic_json(
        directory / "meta.json",
        {
            "version": VERSION,
            "run_id": run_id,
            "name": "headless-" + run_id[:8],
            "mode": "headless",
            "cwd": cwd,
            "session_id": session_id,
            "project_lock": project_lock,
            "state": "starting",
            "created_at": time.time(),
            "supervisor_pid": os.getpid(),
            "supervisor_identity": process_identity(os.getpid()),
        },
    )
    lifecycle.close()
    socket_path = ipc_socket_path(run_id, "events")
    server = None
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
    except OSError as exc:
        if server is not None:
            server.close()
        print("claude-auto: failed to create recovery socket: {}".format(exc), file=sys.stderr)
        cleanup_run(run_id, False)
        return 1
    stream = explicit_stream_output(args)
    attempt_args = list(args)
    recovery_count = 0
    recovery_updated_at = time.time()
    first = True
    final_code = 1
    try:
        while True:
            code, events, stdout_store, stderr_store = run_headless_attempt(
                attempt_args, run_id, server, stream, first
            )
            first = False
            failure, observed_session, blocked = final_recoverable_event(events)
            if observed_session:
                session_id = observed_session
            if blocked:
                if not stream:
                    emit_store(stdout_store, sys.stdout.buffer)
                    emit_store(stderr_store, sys.stderr.buffer)
                final_code = code
                break
            if not failure:
                if not stream:
                    emit_store(stdout_store, sys.stdout.buffer)
                    emit_store(stderr_store, sys.stderr.buffer)
                final_code = code
                break
            if recovery_count and time.time() - recovery_updated_at > RETRY_STATE_EXPIRY:
                recovery_count = 0
            if is_paused(run_id) or consume_cancel(run_id) or recovery_count >= MAX_RECOVERIES or not session_id:
                if not stream:
                    emit_store(stdout_store, sys.stdout.buffer)
                    emit_store(stderr_store, sys.stderr.buffer)
                if recovery_count >= MAX_RECOVERIES:
                    print(
                        "claude-auto: API {} after 3 continuations".format(failure.get("category")),
                        file=sys.stderr,
                    )
                final_code = code
                break
            category = failure.get("category")
            delay = ERROR_POLICIES[category]["delays"][recovery_count]
            recovery_count += 1
            recovery_updated_at = time.time()
            log_event(run_id, "recovery_scheduled", category=category, retry_count=recovery_count, delay=delay)
            recovery_args = build_recovery_args(
                args, session_id, recovery_message(recovery_count, category)
            )
            if recovery_args is None:
                if not stream:
                    emit_store(stdout_store, sys.stdout.buffer)
                    emit_store(stderr_store, sys.stderr.buffer)
                print("claude-auto: cannot safely replay unknown CLI options; recovery disabled", file=sys.stderr)
                final_code = code
                break
            deadline = time.monotonic() + delay
            write_status(
                run_id,
                "countdown",
                retry_count=recovery_count,
                category=category,
                delay=delay,
            )
            cancelled = False
            while time.monotonic() < deadline:
                with recovery_control_lock(run_id):
                    paused = is_paused(run_id)
                    skipped = (directory / "cancel").exists()
                    if skipped:
                        consume_cancel(run_id)
                if paused or skipped:
                    if skipped:
                        log_event(
                            run_id,
                            "recovery_skipped",
                            retry_count=recovery_count,
                        )
                    if not stream:
                        emit_store(stdout_store, sys.stdout.buffer)
                        emit_store(stderr_store, sys.stderr.buffer)
                    final_code = code
                    cancelled = True
                    break
                time.sleep(min(0.25, deadline - time.monotonic()))
            if cancelled:
                break
            with recovery_control_lock(run_id):
                paused = is_paused(run_id)
                skipped = (directory / "cancel").exists()
                if skipped:
                    consume_cancel(run_id)
                if not paused and not skipped:
                    write_status(
                        run_id,
                        "submitting",
                        retry_count=recovery_count,
                    )
            if paused or skipped:
                if skipped:
                    log_event(
                        run_id,
                        "recovery_skipped",
                        retry_count=recovery_count,
                    )
                if not stream:
                    emit_store(stdout_store, sys.stdout.buffer)
                    emit_store(stderr_store, sys.stderr.buffer)
                final_code = code
                break
            if stdout_store:
                stdout_store.close()
            if stderr_store:
                stderr_store.close()
            attempt_args = recovery_args
            log_event(run_id, "recovery_started", category=category, retry_count=recovery_count)
    except KeyboardInterrupt:
        final_code = 130
    finally:
        server.close()
        cleanup_run(run_id, True)
    return final_code


def should_headless(args):
    return has_option(args, {"-p", "--print"}) or not sys.stdin.isatty() or not sys.stdout.isatty()


def raw_version():
    try:
        result = subprocess.run([str(RAW_CLAUDE), "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def compatibility_check(update_version=True):
    errors = []
    if not RAW_CLAUDE.exists():
        errors.append("raw Claude binary missing: {}".format(RAW_CLAUDE))
    version = raw_version()
    if not version:
        errors.append("cannot read Claude Code version")
    if not PYTHON.exists() or sys.version_info < (3, 9):
        errors.append("Python 3.9+ is required")
    if not TMUX.exists():
        errors.append("tmux missing: {}".format(TMUX))
    elif tmux_run(["-V"], capture=True).returncode != 0:
        errors.append("tmux failed to start")
    settings = read_json(SETTINGS_PATH, {})
    hooks = settings.get("hooks", {}) if isinstance(settings, dict) else {}
    for event in TARGET_EVENTS:
        entries = hooks.get(event, [])
        found = False
        for entry in entries if isinstance(entries, list) else []:
            for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
                if str(SCRIPT) in json.dumps(hook, ensure_ascii=False):
                    found = True
        if not found:
            errors.append("missing {} hook".format(event))
    state = read_json(STATE_DIR / "compatibility.json", {})
    previous = state.get("claude_version")
    changed = bool(previous and version and previous != version)
    compatible = not errors
    if update_version:
        atomic_json(
            STATE_DIR / "compatibility.json",
            {
                "claude_version": version,
                "checked_at": time.time(),
                "compatible": compatible,
                "errors": errors,
                "version_changed": changed,
            },
        )
    return compatible, version, changed, errors


def cleanup_stale(force=False):
    ensure_dirs()
    now = time.time()
    for parent, retention in ((RUNS_DIR, STALE_RETENTION), (UNMANAGED_DIR, STALE_RETENTION)):
        for path in list(parent.iterdir()):
            try:
                age = now - path.stat().st_mtime
            except FileNotFoundError:
                continue
            meta = read_json(path / "meta.json", {}) if path.is_dir() else {}
            if parent == RUNS_DIR and path.is_dir() and re.fullmatch(r"[0-9a-f]{32}", path.name):
                if meta.get("state") in {"ending", "end_cleanup_failed"}:
                    presence = owned_tmux_target_presence(meta)
                    if presence is False or (presence is None and successful_final_session_close(meta)):
                        cleanup_run(path.name, True)
                    else:
                        # Legacy, ambiguous, and failed tagged records stay
                        # visible for explicit public cleanup, even with clean.
                        continue
                    continue
                if run_is_live(path.name, meta):
                    continue
                release_named_lock("session", meta.get("session_id"), path.name)
                release_named_lock("project", meta.get("project_lock"), path.name)
                if meta.get("state") not in {"crashed", "exited", "blocked"}:
                    update_meta(path.name, state="crashed", ended_at=now)
            if force or age > retention:
                if parent == RUNS_DIR and path.is_dir() and re.fullmatch(r"[0-9a-f]{32}", path.name):
                    for socket_name in ("events", "launch"):
                        try:
                            ipc_socket_path(path.name, socket_name).unlink()
                        except FileNotFoundError:
                            pass
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
    temp_root = Path(tempfile.gettempdir())
    for path in temp_root.glob("claude-auto-output-*"):
        try:
            if now - path.stat().st_mtime > TEMP_RETENTION:
                path.unlink()
        except (FileNotFoundError, OSError):
            pass


def find_run(name):
    matches = []
    for path in RUNS_DIR.iterdir() if RUNS_DIR.exists() else []:
        if not path.is_dir():
            continue
        meta = read_json(path / "meta.json", {})
        if path.name == name or meta.get("name") == name or str(meta.get("name", "")).startswith(name):
            matches.append((path.name, meta))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit("claude-auto: no session matches {!r}".format(name))
    raise SystemExit("claude-auto: multiple sessions match {!r}".format(name))


SESSION_STATE_GROUPS = {
    "error": {
        "unconfirmed", "stale", "exhausted", "unsupported", "blocked",
        "incompatible", "target_lost", "crashed", "end_cleanup_failed",
    },
    "active": {"countdown", "submitting", "awaiting"},
    "update": {"update_available"},
    "paused": {"paused", "paused_global"},
}
SESSION_RISK = {"error": 0, "active": 1, "update": 2, "paused": 3, "ok": 4}
SESSION_TAGS = {
    "error": "ERROR",
    "active": "ACTIVE",
    "update": "UPDATE",
    "paused": "PAUSED",
    "ok": "OK",
}


CLI_SEVERITY_STYLE = {
    "error": ("!", "31"),
    "active": ("◔", "36"),
    "update": ("!", "33"),
    "paused": ("○", "2"),
    "ok": ("✓", "32"),
}


def cli_severity_tag(severity, stream=None):
    stream = stream or sys.stdout
    label = SESSION_TAGS[severity]
    color, unicode = terminal_capabilities(stream)
    if not color:
        return label
    symbol, code = CLI_SEVERITY_STYLE[severity]
    if not unicode:
        symbol = {"error": "!", "active": "~", "update": "!", "paused": "-", "ok": "+"}[severity]
    return "\033[{}m{} {}\033[0m".format(code, symbol, label)


def session_severity(state):
    for severity, states in SESSION_STATE_GROUPS.items():
        if state in states:
            return severity
    return "ok"


def session_diagnostics():
    diagnostics = []
    for path in RUNS_DIR.iterdir() if RUNS_DIR.exists() else []:
        if not path.is_dir():
            continue
        meta = read_json(path / "meta.json", {})
        if meta.get("state") != "end_cleanup_failed" and not run_is_live(path.name, meta):
            continue
        status = read_json(path / "status.json", {})
        state = status.get("state", meta.get("status", "running"))
        if (
            meta.get("version")
            and meta.get("version") != VERSION
            and session_severity(state) not in {"error", "active"}
        ):
            state = "update_available"
        severity = session_severity(state)
        diagnostics.append(
            {
                "run_id": path.name,
                "name": meta.get("name", path.name),
                "mode": meta.get("mode", "?"),
                "cwd": meta.get("cwd", ""),
                "state": state,
                "severity": severity,
                "version": meta.get("version"),
            }
        )
    diagnostics.sort(
        key=lambda item: (SESSION_RISK[item["severity"]], item["name"])
    )
    return diagnostics


def list_runs():
    cleanup_stale()
    rows = session_diagnostics()
    if not rows:
        print("No active claude-auto sessions.")
        return 0
    for row in rows:
        print(
            "{:<7} {:<36} {:<12} {:<18} {}".format(
                cli_severity_tag(row["severity"]),
                row["name"],
                row["mode"],
                row["state"],
                row["cwd"],
            )
        )
    return 0


def show_status():
    cleanup_stale()
    compatible, version, changed, errors = compatibility_check(update_version=True)
    print("claude-auto {} · Claude Code {}".format(VERSION, version or "unavailable"))
    print("automatic recovery: {}".format(
        "paused globally" if GLOBAL_PAUSE.exists() else ("enabled" if compatible else "observe-only")
    ))
    if changed:
        print("Claude Code version changed; the offline compatibility check was rerun.")
    if errors:
        for error in errors:
            print("- {}".format(error))
    return list_runs()


def attach_run(name):
    run_id, meta = find_run(name)
    if meta.get("mode") != "interactive":
        raise SystemExit("claude-auto: only interactive sessions can be attached")
    session_name = meta.get("tmux_session")
    window_id = meta.get("window_id")
    if os.environ.get("TMUX"):
        if window_id:
            return tmux_run(["select-window", "-t", "{}:{}".format(session_name, window_id)], capture=False).returncode
        return tmux_run(["switch-client", "-t", session_name], capture=False).returncode
    target = "{}:{}".format(session_name, window_id) if window_id else session_name
    return attach_managed_session(run_id, target)


def set_pause(name, paused):
    if name:
        run_id, _ = find_run(name)
        marker = run_dir(run_id) / "paused"
        lock = recovery_control_lock(run_id)
    else:
        marker = GLOBAL_PAUSE
        lock = recovery_control_lock(global_exclusive=True)
    with lock:
        if paused:
            atomic_json(marker, {"paused_at": time.time(), "scope": name or "global"})
        else:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
            if name:
                write_status(run_id, "ready", retry_count=0)
    print("{} {} recovery.".format("Paused" if paused else "Resumed", name or "global"))
    return 0


def skip_run(name):
    run_id, meta = find_run(name)
    with recovery_control_lock(run_id):
        state = read_json(run_dir(run_id) / "status.json", {}).get("state")
        if state != "countdown":
            if state in {"submitting", "awaiting"}:
                print(
                    "Recovery is already submitting; it cannot be safely skipped.",
                    file=sys.stderr,
                )
            else:
                print("No pending recovery to skip for {}.".format(meta.get("name", name)), file=sys.stderr)
            return 1
        (run_dir(run_id) / "cancel").touch(mode=0o600, exist_ok=True)
    print("Skipped pending recovery for {}.".format(meta.get("name", name)))
    return 0


def cancel_run(name):
    return skip_run(name)


def retry_owned_cleanup(name):
    """Retry only a retained tagged target through explicit user action."""
    run_id, meta = find_run(name)
    if meta.get("state") != "end_cleanup_failed":
        print("No retained end-cleanup failure for {}.".format(meta.get("name", name)), file=sys.stderr)
        return 1
    if finish_owned_tmux_target(run_id):
        print("Cleaned up {}.".format(meta.get("name", name)))
        return 0
    failure = get_meta(run_id).get("end_close_failure", "target_unverified")
    if failure == "legacy_metadata_refused":
        print("Cleanup for {} is refused: legacy metadata has no owner tag or tmux server generation; inspect and close it manually.".format(meta.get("name", name)), file=sys.stderr)
    else:
        print("Cleanup for {} was not retried safely ({}); retained for inspection.".format(meta.get("name", name), failure), file=sys.stderr)
    return 1


def cancel_target(session_name, window_id):
    for path in RUNS_DIR.iterdir() if RUNS_DIR.exists() else []:
        meta = read_json(path / "meta.json", {})
        if meta.get("tmux_session") == session_name and meta.get("window_id") == window_id:
            with recovery_control_lock(path.name):
                status = read_json(path / "status.json", {})
                if status.get("state") != "countdown":
                    return 1
                (path / "cancel").touch(mode=0o600, exist_ok=True)
            return 0
    return 1


def show_logs(name):
    run_id, _ = find_run(name)
    path = run_dir(run_id) / "events.jsonl"
    if not path.exists():
        print("No events for this session.")
        return 0
    with open(path, "r", encoding="utf-8") as handle:
        shutil.copyfileobj(handle, sys.stdout)
    return 0


SESSION_ACTIONS = {
    "unconfirmed": "inspect the session; press Enter only if recovery text remains",
    "stale": "inspect the session, then run claude-auto doctor",
    "exhausted": "inspect the session before continuing manually",
    "unsupported": "inspect the session before continuing manually",
    "blocked": "run claude-auto doctor",
    "incompatible": "run claude-auto doctor",
    "target_lost": "run claude-auto doctor",
    "update_available": "restart this managed session to load the installed update",
}


def session_action(row):
    if row["state"] == "paused_global":
        return "run claude-auto resume"
    if row["state"] == "paused":
        return "run claude-auto resume {}".format(row["name"])
    return SESSION_ACTIONS.get(row["state"], "inspect this managed session")


def doctor():
    cleanup_stale()
    compatible, version, changed, errors = compatibility_check(update_version=True)
    print("claude-auto {}".format(VERSION))
    print("Claude Code: {}".format(version or "unavailable"))
    print("tmux: {}".format(tmux_run(["-V"], capture=True).stdout.strip() if TMUX.exists() else "missing"))
    print("global pause: {}".format("on" if GLOBAL_PAUSE.exists() else "off"))
    print("compatibility: {}".format("ok" if compatible else "observe-only"))
    if changed:
        print("Claude Code version changed; local compatibility check was run.")
    for error in errors:
        print("- {}".format(error))
    diagnostics = session_diagnostics()
    if diagnostics:
        print("managed sessions:")
        for row in diagnostics:
            print(
                "{} {} · {} · cwd={}".format(
                    cli_severity_tag(row["severity"]),
                    row["name"],
                    row["state"],
                    row["cwd"],
                )
            )
            if row["severity"] != "ok":
                print("  action: {}".format(session_action(row)))
    has_session_error = any(row["severity"] == "error" for row in diagnostics)
    return 0 if compatible and not has_session_error else 1


RESUME_SESSION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


def install_report_active():
    live = []
    try:
        paths = list(RUNS_DIR.iterdir()) if RUNS_DIR.exists() else []
    except OSError:
        paths = []
    for path in paths:
        if not path.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", path.name):
            continue
        meta = read_json(path / "meta.json", {})
        try:
            if run_is_live(path.name, meta):
                live.append(meta)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    if not live:
        return 0
    count = len(live)
    noun = "session" if count == 1 else "sessions"
    verb = "is" if count == 1 else "are"
    print(
        "claude-auto: {} managed {} started before this installation {} still active.".format(
            count, noun, verb
        )
    )
    print(
        "Their already-running recovery process was not replaced; no session was stopped or changed."
        if count != 1
        else "Its already-running recovery process was not replaced; no session was stopped or changed."
    )
    for meta in live:
        mode = json.dumps(str(meta.get("mode") or "unknown"), ensure_ascii=True)
        cwd = json.dumps(str(meta.get("cwd") or "unknown"), ensure_ascii=True)
        session_id = meta.get("session_id")
        encoded_session = json.dumps(
            str(session_id) if session_id else "unavailable", ensure_ascii=True
        )
        print("  mode={} cwd={} session_id={}".format(mode, cwd, encoded_session))
        if session_id and RESUME_SESSION_ID.fullmatch(str(session_id)):
            print("  After it exits, start Claude from the reported cwd and use:")
            print("    claude --resume {}".format(session_id))
        else:
            print("  After it exits, use Claude Code's normal resume selection from the reported cwd.")
    print(
        "  Resume restores conversation history, not the original CLI invocation; reapply any launch policy or configuration options you still need."
    )
    return 0


def self_test():
    failures = []

    def expect(name, condition):
        if condition:
            print("ok - " + name)
        else:
            print("not ok - " + name)
            failures.append(name)

    expect(
        "timeout exact text",
        classify_failure({"last_assistant_message": "⏺ API Error: The operation timed out."}) == "timeout",
    )
    expect(
        "request timeout text",
        classify_failure({"error_details": "Request timed out"}) == "timeout",
    )
    expect(
        "CC Switch overloaded 422",
        classify_failure({
            "error": "unknown",
            "error_details": "422 格式转换错误: Responses upstream service_unavailable_error: Our servers are currently overloaded. Please try again later.",
        }) == "overloaded",
    )
    expect(
        "structured overloaded",
        classify_failure({"error": "overloaded"}) == "overloaded",
    )
    expect(
        "ordinary 422 ignored",
        classify_failure({"error": "unknown", "error_details": "API Error: 422 invalid request format"}) is None,
    )
    expect("recovery marker", recovery_message(2, "timeout").startswith("[claude-auto recovery 2/3][timeout]"))
    expect("timeout backoff", TIMEOUT_DELAYS == (5, 15, 30))
    expect("overloaded backoff", OVERLOADED_DELAYS == (15, 30, 60))
    expect("shared recovery cap", MAX_RECOVERIES == 3)
    preserved = build_recovery_args(
        ["-p", "--model", "opus", "--mcp-config", "a.json", "--output-format", "json", "original prompt"],
        "00000000-0000-4000-8000-000000000000",
        "continue safely",
    )
    expect("launch options replayed", preserved is not None and "original prompt" not in preserved and "--resume" in preserved)
    expect("unknown options fail closed", build_recovery_args(["-p", "--future-option", "value", "prompt"], "id", "msg") is None)
    expect("raw binary exists", RAW_CLAUDE.exists())
    expect("tmux exists", TMUX.exists())
    expect("double-dash stops wrapper parsing", direct_reason(["--", "install"]) is None)
    print("{} tests, {} failures".format(13, len(failures)))
    return 1 if failures else 0


def installed_hook_commands():
    commands = set()
    manifest = read_json(CONFIG_DIR / "install-manifest.json", {})
    command = manifest.get("hook_command") if isinstance(manifest, dict) else None
    if isinstance(command, str) and command:
        commands.add(command)
    return commands


def remove_our_hooks(settings):
    if not isinstance(settings, dict):
        return settings
    commands = installed_hook_commands()
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept_entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                kept_entries.append(entry)
                continue
            hook_list = entry.get("hooks")
            if not isinstance(hook_list, list):
                kept_entries.append(entry)
                continue
            kept_hooks = [
                hook
                for hook in hook_list
                if not (
                    isinstance(hook, dict)
                    and hook.get("type") == "command"
                    and hook.get("command") in commands
                )
            ]
            if kept_hooks:
                new_entry = dict(entry)
                new_entry["hooks"] = kept_hooks
                kept_entries.append(new_entry)
        if kept_entries:
            hooks[event] = kept_entries
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    return settings


def installed_shell_rc_paths():
    paths = list(SHELL_RC_PATHS)
    manifest = read_json(CONFIG_DIR / "install-manifest.json", {})
    shell_rc = manifest.get("shell_rc") if isinstance(manifest, dict) else None
    if isinstance(shell_rc, str) and shell_rc:
        path = Path(shell_rc).expanduser()
        if path not in paths:
            paths.append(path)
    return tuple(paths)


def remove_marker_block(path, start, end):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    pattern = re.compile(r"(?:^|\n)" + re.escape(start) + r"\n.*?\n" + re.escape(end) + r"\n?", re.DOTALL)
    new_text = pattern.sub("\n", text).lstrip("\n")
    Path(path).write_text(new_text, encoding="utf-8")


def load_json_strict(path):
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def uninstall_owned_paths():
    manifest = load_json_strict(CONFIG_DIR / "install-manifest.json")
    expected = {
        "app_dir": APP_DIR,
        "config_dir": CONFIG_DIR,
        "state_dir": STATE_DIR,
        "bin_dir": SHIM_DIR,
    }
    unsafe = {
        Path("/").resolve(),
        HOME.resolve(),
        (HOME / ".config").resolve(),
        (HOME / ".local").resolve(),
        (HOME / ".local" / "share").resolve(),
        (HOME / ".local" / "state").resolve(),
        Path("/tmp").resolve(),
        Path("/var").resolve(),
        Path("/etc").resolve(),
        Path("/usr").resolve(),
        Path("/opt").resolve(),
        Path("/Applications").resolve(),
    }
    resolved = {}
    for key, runtime_path in expected.items():
        recorded = manifest.get(key)
        if not isinstance(recorded, str) or not recorded:
            raise ValueError("install manifest is missing {}".format(key))
        path = Path(recorded).expanduser().resolve()
        if path != runtime_path.expanduser().resolve():
            raise ValueError("install manifest {} does not match runtime path".format(key))
        if key != "bin_dir" and path in unsafe:
            raise ValueError("unsafe managed directory: {}".format(path))
        resolved[key] = path
    recursive = [resolved[key] for key in ("app_dir", "config_dir", "state_dir")]
    for index, left in enumerate(recursive):
        for right in recursive[index + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise ValueError("managed directories overlap: {} and {}".format(left, right))
    for key in ("app_dir", "config_dir", "state_dir"):
        if not (resolved[key] / ".claude-auto-owned").is_file():
            raise ValueError("ownership marker is missing from {}".format(resolved[key]))
    if not (resolved["bin_dir"] / ".claude-auto-bin-owned").is_file():
        raise ValueError("bin ownership marker is missing from {}".format(resolved["bin_dir"]))
    return resolved


def uninstall(dry_run=False):
    lifecycle = lifecycle_lock(exclusive=True)
    try:
        owned = uninstall_owned_paths()
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        lifecycle.close()
        raise SystemExit("claude-auto: install ownership is invalid; refusing uninstall: {}".format(exc))
    active = []
    for path in RUNS_DIR.iterdir() if RUNS_DIR.exists() else []:
        if path.is_dir() and run_is_live(path.name, read_json(path / "meta.json", {})):
            active.append(path.name)
    if active:
        raise SystemExit("claude-auto: stop active managed sessions before uninstalling")
    if dry_run:
        try:
            load_json_strict(SETTINGS_PATH)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise SystemExit("claude-auto: settings are unreadable; refusing uninstall: {}".format(exc))
        print("Would remove: five claude-auto hook entries from {}".format(SETTINGS_PATH))
        print("Would remove: marked PATH blocks from {}".format(
            ", ".join(str(path) for path in installed_shell_rc_paths())
        ))
        print("Would remove: {}, {}, and {}".format(
            owned["bin_dir"] / "claude", owned["bin_dir"] / "claude-auto", owned["bin_dir"] / "claude-raw"
        ))
        print("Would remove: source, configuration, and runtime state")
        print("Would keep: Homebrew, tmux, and the official {}".format(RAW_CLAUDE))
        return 0
    try:
        settings = load_json_strict(SETTINGS_PATH)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit("claude-auto: settings are unreadable; refusing uninstall: {}".format(exc))
    atomic_json(SETTINGS_PATH, remove_our_hooks(settings))
    for shell_rc in installed_shell_rc_paths():
        remove_marker_block(shell_rc, "# >>> claude-auto >>>", "# <<< claude-auto <<<")
        try:
            if shell_rc.exists() and not shell_rc.read_text(encoding="utf-8").strip():
                shell_rc.unlink()
        except OSError:
            pass
    if our_binding_present():
        tmux_run(["unbind-key", "-T", "prefix", "X"], capture=True)
    try:
        BINDING_OWNER.unlink()
    except FileNotFoundError:
        pass
    for path in (
        owned["bin_dir"] / "claude",
        owned["bin_dir"] / "claude-auto",
        owned["bin_dir"] / "claude-raw",
        owned["bin_dir"] / ".claude-auto-bin-owned",
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    try:
        owned["bin_dir"].rmdir()
    except OSError:
        pass
    shutil.rmtree(owned["config_dir"])
    shutil.rmtree(owned["state_dir"])
    shutil.rmtree(owned["app_dir"])
    try:
        info = IPC_DIR.stat()
        if info.st_uid == os.getuid() and not (info.st_mode & 0o077):
            IPC_DIR.rmdir()
    except (FileNotFoundError, OSError):
        pass
    print("Removed claude-auto hooks, PATH entries, shims, source, configuration, and state. Homebrew and tmux were kept.")
    return 0


def management_main(args, lifecycle=None):
    parser = argparse.ArgumentParser(prog="claude-auto")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("list")
    attach = subparsers.add_parser("attach")
    attach.add_argument("name")
    pause = subparsers.add_parser("pause")
    pause.add_argument("name", nargs="?")
    resume = subparsers.add_parser("resume")
    resume.add_argument("name", nargs="?")
    skip = subparsers.add_parser("skip")
    skip.add_argument("name")
    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("name")
    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("name")
    logs = subparsers.add_parser("logs")
    logs.add_argument("name")
    subparsers.add_parser("status")
    subparsers.add_parser("doctor")
    subparsers.add_parser("self-test")
    subparsers.add_parser("clean")
    uninstall_parser = subparsers.add_parser("uninstall")
    uninstall_parser.add_argument("--dry-run", action="store_true")
    new = subparsers.add_parser("new")
    new.add_argument("--name")
    new.add_argument("claude_args", nargs=argparse.REMAINDER)
    ns = parser.parse_args(args)
    if ns.command in {None, "list"}:
        return list_runs()
    if ns.command == "status":
        return show_status()
    if ns.command == "attach":
        return attach_run(ns.name)
    if ns.command == "pause":
        return set_pause(ns.name, True)
    if ns.command == "resume":
        return set_pause(ns.name, False)
    if ns.command in {"skip", "cancel"}:
        return skip_run(ns.name)
    if ns.command == "cleanup":
        return retry_owned_cleanup(ns.name)
    if ns.command == "logs":
        return show_logs(ns.name)
    if ns.command == "doctor":
        return doctor()
    if ns.command == "self-test":
        return self_test()
    if ns.command == "clean":
        cleanup_stale(force=True)
        print("Removed inactive claude-auto state; active sessions were kept.")
        return 0
    if ns.command == "uninstall":
        return uninstall(ns.dry_run)
    if ns.command == "new":
        claude_args = list(ns.claude_args)
        if claude_args and claude_args[0] == "--":
            claude_args = claude_args[1:]
        if ns.name:
            os.environ["CLAUDE_AUTO_NAME"] = ns.name
        return interactive_main(claude_args, lifecycle=lifecycle)
    return 2


def clear_inherited_managed_environment():
    """A shim invocation is distinct from the root wrapper that spawned it."""
    for name in ("CLAUDE_AUTO_RUN_ID", "CLAUDE_AUTO_MANAGED"):
        os.environ.pop(name, None)


def shim_main(args):
    clear_inherited_managed_environment()
    lifecycle = lifecycle_lock()
    if not SCRIPT.exists():
        lifecycle.close()
        raw_exec(args, disabled=False)
    ensure_dirs()
    cleanup_stale()
    reason = direct_reason(args)
    if reason:
        lifecycle.close()
        raw_exec(args, disabled=has_option(args, {"--safe-mode", "--bare"}))
    compatibility = read_json(STATE_DIR / "compatibility.json", {})
    current_version = raw_version()
    if (
        compatibility.get("claude_version") != current_version
        or not compatibility.get("compatible")
    ):
        compatible, _, _, _ = compatibility_check(update_version=True)
        if not compatible:
            lifecycle.close()
            raw_exec(args, disabled=False)
    if should_headless(args):
        return headless_main(args, lifecycle=lifecycle)
    return interactive_main(args, lifecycle=lifecycle)


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "hook":
        lifecycle = lifecycle_lock()
        if not SCRIPT.exists():
            lifecycle.close()
            return 0
        try:
            return hook_main()
        finally:
            lifecycle.close()
    if len(sys.argv) >= 4 and sys.argv[1] == "watchdog":
        return watchdog_main(sys.argv[2], sys.argv[3])
    if len(sys.argv) >= 3 and sys.argv[1] == "pane-run":
        return pane_run_main(sys.argv[2])
    if len(sys.argv) >= 3 and sys.argv[1] == "owner-monitor":
        return owner_monitor_main(sys.argv[2])
    if len(sys.argv) >= 4 and sys.argv[1] == "cancel-target":
        return cancel_target(sys.argv[2], sys.argv[3])
    if len(sys.argv) >= 2 and sys.argv[1] == "install-report-active":
        return install_report_active()
    if len(sys.argv) >= 2 and sys.argv[1] == "install-self-test":
        return self_test()
    if len(sys.argv) >= 2 and sys.argv[1] == "manage":
        manage_args = sys.argv[2:]
        if manage_args and manage_args[0] == "uninstall":
            return management_main(manage_args)
        lifecycle = lifecycle_lock()
        if not SCRIPT.exists():
            lifecycle.close()
            print("claude-auto: installation was removed", file=sys.stderr)
            return 1
        try:
            return management_main(manage_args, lifecycle=lifecycle)
        finally:
            lifecycle.close()
    if len(sys.argv) >= 2 and sys.argv[1] == "shim":
        return shim_main(sys.argv[2:])
    print("claude-auto internal entry point", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
