import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

KNOWN_SHELLS = {"zsh", "bash", "fish", "nu", "sh", "dash", "ksh", "tcsh", "csh"}


@dataclass
class KittyWindow:
    id: int
    foreground_command: str
    window_type: str  # "shell", "claude", "busy"
    pids: list[int] = field(default_factory=list)


@dataclass
class SwitchResult:
    switched: int = 0
    skipped: list[str] = field(default_factory=list)
    claude_windows: list[KittyWindow] = field(default_factory=list)


class KittyError(Exception):
    pass


def is_kitty() -> bool:
    """Check if running in kitty with a working remote control socket."""
    if "KITTY_WINDOW_ID" not in os.environ or "KITTY_LISTEN_ON" not in os.environ:
        return False
    try:
        result = subprocess.run(
            _kitten_cmd("ls"),
            capture_output=True, text=True, timeout=3,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def self_window_id() -> int:
    return int(os.environ.get("KITTY_WINDOW_ID", "0"))


def _kitten_cmd(*args: str) -> list[str]:
    """Build a kitten @ command, using the socket if available."""
    cmd = ["kitten", "@"]
    listen_on = os.environ.get("KITTY_LISTEN_ON", "")
    if listen_on:
        cmd.extend(["--to", listen_on])
    cmd.extend(args)
    return cmd


def _is_claude_process(foreground_processes: list[dict]) -> bool:
    for proc in foreground_processes:
        cmdline = proc.get("cmdline", [])
        for arg in cmdline:
            if Path(arg).name == "claude":
                return True
    return False


def classify_window(foreground_processes: list[dict]) -> tuple[str, str]:
    """Returns (window_type, command_name)."""
    if not foreground_processes:
        return ("shell", "unknown")
    proc = foreground_processes[-1]
    cmdline = proc.get("cmdline", [])
    if not cmdline:
        return ("shell", "unknown")
    binary = Path(cmdline[0]).name
    if binary in KNOWN_SHELLS:
        return ("shell", binary)
    if _is_claude_process(foreground_processes):
        return ("claude", "claude")
    return ("busy", binary)


def _kitty_ls() -> list[dict]:
    try:
        result = subprocess.run(
            _kitten_cmd("ls"),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        raise KittyError("kitten command not found")
    except subprocess.TimeoutExpired:
        raise KittyError("kitten @ ls timed out")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "remote control" in stderr.lower() or "allow_remote_control" in stderr.lower():
            raise KittyError(
                "Kitty remote control not enabled. "
                "Add 'allow_remote_control yes' to kitty.conf"
            )
        raise KittyError(f"kitten @ ls failed: {stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise KittyError("Failed to parse kitten @ ls output")


def get_sibling_windows() -> list[KittyWindow]:
    """Get all windows in the same tab, excluding gbb's own window."""
    my_id = self_window_id()
    data = _kitty_ls()

    for os_window in data:
        for tab in os_window.get("tabs", []):
            window_ids = [w["id"] for w in tab.get("windows", [])]
            if my_id in window_ids:
                windows = []
                for w in tab["windows"]:
                    if w["id"] == my_id:
                        continue
                    fg = w.get("foreground_processes", [])
                    wtype, cmd = classify_window(fg)
                    pids = [p["pid"] for p in fg if "pid" in p]
                    windows.append(KittyWindow(id=w["id"], foreground_command=cmd, window_type=wtype, pids=pids))
                return windows

    return []


def get_sibling_cwd() -> Path | None:
    """Get the most common cwd among idle shell panes in the same tab."""
    my_id = self_window_id()
    data = _kitty_ls()

    for os_window in data:
        for tab in os_window.get("tabs", []):
            window_ids = [w["id"] for w in tab.get("windows", [])]
            if my_id not in window_ids:
                continue
            cwds: list[str] = []
            for w in tab["windows"]:
                if w["id"] == my_id:
                    continue
                fg = w.get("foreground_processes", [])
                wtype, _ = classify_window(fg)
                if wtype == "shell":
                    cwd = w.get("cwd", "")
                    if cwd:
                        cwds.append(cwd)
            if not cwds:
                return None
            # Most common cwd wins
            from collections import Counter
            most_common = Counter(cwds).most_common(1)[0][0]
            return Path(most_common)

    return None


def send_text(window_id: int, text: str) -> bool:
    try:
        result = subprocess.run(
            _kitten_cmd("send-text", "--stdin", "--match", f"id:{window_id}"),
            input=text,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _quote_path(path: Path) -> str:
    path_str = str(path)
    if " " in path_str or "'" in path_str:
        path_str = "'" + path_str.replace("'", "'\\''") + "'"
    return path_str


def switch_pane(window_id: int, target_path: Path, checkout_branch: str | None = None) -> bool:
    path_str = _quote_path(target_path)
    if checkout_branch:
        return send_text(window_id, f"cd {path_str} && git checkout {checkout_branch}\n")
    return send_text(window_id, f"cd {path_str}\n")


def restart_claude_pane(
    window: KittyWindow,
    target_path: Path,
    checkout_branch: str | None = None,
    claude_flag: str = "continue",
) -> bool:
    # Kill claude process by PID (SIGTERM, then SIGKILL if needed)
    for pid in reversed(window.pids):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # Wait for process to die
    for _ in range(20):
        time.sleep(0.1)
        alive = False
        for pid in window.pids:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                pass
        if not alive:
            break
    else:
        # Force kill if still alive
        for pid in reversed(window.pids):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.3)

    path_str = _quote_path(target_path)
    if checkout_branch:
        return send_text(window.id, f"cd {path_str} && git checkout {checkout_branch} && claude --{claude_flag}\n")
    return send_text(window.id, f"cd {path_str} && claude --{claude_flag}\n")


def switch_all_panes(target_path: Path, checkout_branch: str | None = None) -> SwitchResult:
    """Switch all sibling shell panes. Only the first shell pane does checkout to avoid lock races."""
    result = SwitchResult()
    windows = get_sibling_windows()
    checked_out = False

    for w in windows:
        if w.window_type == "shell":
            branch = checkout_branch if not checked_out else None
            if switch_pane(w.id, target_path, branch):
                result.switched += 1
                if branch:
                    checked_out = True
            else:
                result.skipped.append(f"{w.foreground_command} (pane {w.id})")
        elif w.window_type == "claude":
            result.claude_windows.append(w)
        else:
            result.skipped.append(f"{w.foreground_command} (pane {w.id})")

    return result


def focus_repo_tab(repo_name: str) -> bool:
    """Focus an existing tab whose title matches repo_name. Skips our own tab."""
    my_id = self_window_id()
    data = _kitty_ls()
    for os_window in data:
        for tab in os_window.get("tabs", []):
            window_ids = [w["id"] for w in tab.get("windows", [])]
            if my_id in window_ids:
                continue
            title = tab.get("title", "")
            if title == repo_name or title.startswith(f"{repo_name} ("):
                windows = tab.get("windows", [])
                if windows:
                    wid = windows[0]["id"]
                    try:
                        subprocess.run(
                            _kitten_cmd("focus-window", f"--match=id:{wid}"),
                            capture_output=True, text=True, timeout=5,
                        )
                        return True
                    except (subprocess.TimeoutExpired, FileNotFoundError):
                        pass
    return False


def next_tab_title(repo_name: str) -> str:
    """Return a unique tab title like 'repo (2)', 'repo (3)', etc."""
    data = _kitty_ls()
    existing: set[str] = set()
    for os_window in data:
        for tab in os_window.get("tabs", []):
            existing.add(tab.get("title", ""))
    if repo_name not in existing:
        return repo_name
    n = 2
    while f"{repo_name} ({n})" in existing:
        n += 1
    return f"{repo_name} ({n})"


def create_workspace_tab(
    repo_name: str,
    repo_path: Path,
    selected_dir: Path,
    checkout_branch: str | None = None,
    tab_title: str | None = None,
    start_claude: bool = True,
    claude_flag: str | None = "continue",
) -> None:
    """Create a new kitty tab with claude, gbb, and shell panes."""
    title = tab_title or repo_name

    def run(args: list[str]) -> str:
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            raise KittyError(f"kitten command failed: {result.stderr.strip()}")
        return result.stdout.strip()

    # Step 1: new tab with left pane (claude / shell)
    left_id = run(_kitten_cmd(
        "launch", "--type=tab", f"--tab-title={title}",
        f"--cwd={selected_dir}",
    ))

    # Step 2: right-top pane (gbb) via vsplit
    gbb_id = run(_kitten_cmd(
        "launch", "--location=vsplit", f"--cwd={repo_path}",
    ))

    # Step 3: right-bottom pane (shell) via hsplit
    shell_id = run(_kitten_cmd(
        "launch", "--location=hsplit", f"--cwd={selected_dir}",
    ))

    # Step 4: make shell pane taller (~75% of right column)
    run(_kitten_cmd(
        "resize-window", "--axis=vertical", "--increment=15",
        f"--match=id:{shell_id}",
    ))

    # Step 5: send commands to panes (only one pane does checkout to avoid lock races)
    claude_cmd = f"claude --{claude_flag}" if claude_flag else "claude"
    if checkout_branch:
        if start_claude:
            send_text(int(left_id), f"git checkout {checkout_branch} && {claude_cmd}\n")
        else:
            send_text(int(left_id), f"git checkout {checkout_branch}\n")
    elif start_claude:
        send_text(int(left_id), f"{claude_cmd}\n")
    send_text(int(gbb_id), "gbb\n")

    # Step 6: focus left pane
    run(_kitten_cmd("focus-window", f"--match=id:{left_id}"))


def clear_idle_panes() -> int:
    """Send clear (Cmd+K equivalent) to all idle shell panes. Returns count cleared."""
    windows = get_sibling_windows()
    cleared = 0
    for w in windows:
        if w.window_type == "shell":
            if send_text(w.id, "clear\n"):
                cleared += 1
    return cleared
