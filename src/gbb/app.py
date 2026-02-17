import os
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static
from textual.worker import Worker, get_current_worker

from gbb.cleanup import delete_branch, delete_worktree, list_non_ignored_entries
from gbb.config import Config, WorkspaceConfig
from gbb.git import BranchInfo, create_worktree, detect_main_branch, discover_repo, fetch_repo, is_dirty
from gbb.kitty import (
    KittyError,
    KittyWindow,
    clear_idle_panes,
    create_workspace_tab,
    focus_repo_tab,
    get_sibling_cwd,
    is_kitty,
    next_tab_title,
    restart_claude_pane,
    switch_all_panes,
    switch_pane,
)
from gbb.pins import load_pins, pin_key, save_pins

REPO_COLORS = [
    "#50fa7b",
    "#8be9fd",
    "#ff79c6",
    "#ffb86c",
    "#bd93f9",
    "#f1fa8c",
    "#ff5555",
    "#6272a4",
]


def format_age(timestamp: int) -> str:
    delta = int(time.time()) - timestamp
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    if delta < 604800:
        return f"{delta // 86400}d"
    return f"{delta // 604800}w"


def format_ahead_behind(ahead: int, behind: int) -> Text:
    if not ahead and not behind:
        return Text("")
    result = Text()
    if ahead:
        result.append(f"↑{ahead}", style="#50fa7b")
    if behind:
        result.append(f"↓{behind}", style="#ff5555")
    return result


def shorten_path(path: Path) -> str:
    home = Path.home()
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


class DeleteConfirmScreen(ModalScreen[bool]):
    CSS = """
    DeleteConfirmScreen {
        align: center middle;
    }
    #delete-dialog {
        width: 60;
        height: auto;
        max-height: 80%;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(
        self,
        branch_name: str,
        worktree_path: str,
        reasons: list[str],
        entries: list[str],
    ):
        super().__init__()
        self._branch_name = branch_name
        self._worktree_path = worktree_path
        self._reasons = reasons
        self._entries = entries

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-dialog"):
            yield Static(self._build_content())

    def _build_content(self) -> str:
        lines = [f"[bold]Delete '{self._branch_name}'?[/bold]", ""]
        lines.append(self._worktree_path)
        if self._reasons:
            lines.append("")
            for reason in self._reasons:
                lines.append(f"[yellow]  {reason}[/yellow]")
        if self._entries:
            lines.append("")
            for entry in self._entries[:20]:
                lines.append(f"  {entry}")
            remaining = len(self._entries) - 20
            if remaining > 0:
                lines.append(f"  [dim]... and {remaining} more[/dim]")
        lines.append("")
        lines.append("[dim]y[/dim] delete  [dim]n[/dim] cancel")
        return "\n".join(lines)

    def on_key(self, event: events.Key) -> None:
        if event.key == "y":
            self.dismiss(True)
        elif event.key in ("n", "escape"):
            self.dismiss(False)
        event.prevent_default()
        event.stop()


class ClaudeConfirmScreen(ModalScreen[str]):
    """Returns 'continue', 'resume', or '' (cancel)."""

    CSS = """
    ClaudeConfirmScreen {
        align: center middle;
    }
    #claude-dialog {
        width: 50;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, count: int):
        super().__init__()
        self._count = count

    def compose(self) -> ComposeResult:
        with Vertical(id="claude-dialog"):
            s = "s" if self._count != 1 else ""
            yield Static(
                f"[bold]Claude running in {self._count} pane{s}[/bold]\n\n"
                f"Kill + restart?\n\n"
                f"[dim]c[/dim] --continue  [dim]r[/dim] --resume  [dim]k[/dim] kill  [dim]n[/dim] skip"
            )

    def on_key(self, event: events.Key) -> None:
        if event.key == "c":
            self.dismiss("continue")
        elif event.key == "r":
            self.dismiss("resume")
        elif event.key == "k":
            self.dismiss("kill")
        elif event.key in ("n", "escape"):
            self.dismiss("")
        event.prevent_default()
        event.stop()


class WorkspaceOptionsScreen(ModalScreen[WorkspaceConfig | None]):
    """Returns WorkspaceConfig on confirm, None on cancel."""

    CSS = """
    WorkspaceOptionsScreen {
        align: center middle;
    }
    #workspace-dialog {
        width: 40;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, defaults: WorkspaceConfig):
        super().__init__()
        self._start_claude = defaults.start_claude

    def compose(self) -> ComposeResult:
        with Vertical(id="workspace-dialog"):
            yield Static(id="ws-content")

    def on_mount(self) -> None:
        self._refresh_content()

    def _refresh_content(self) -> None:
        check = "×" if self._start_claude else " "
        self.query_one("#ws-content", Static).update(
            f"[bold]New workspace[/bold]\n\n"
            f"  [dim]space[/dim]  [{check}] Start claude\n\n"
            f"[dim]enter[/dim] create  [dim]esc[/dim] cancel"
        )

    def on_key(self, event: events.Key) -> None:
        if event.key == "space":
            self._start_claude = not self._start_claude
            self._refresh_content()
        elif event.key == "enter":
            self.dismiss(WorkspaceConfig(start_claude=self._start_claude))
        elif event.key == "escape":
            self.dismiss(None)
        event.prevent_default()
        event.stop()


class CreateWorktreeScreen(ModalScreen[tuple[str, str] | None]):
    """Returns (branch_name, base_branch) on confirm, None on cancel."""

    CSS = """
    CreateWorktreeScreen {
        align: center middle;
    }
    #create-wt-dialog {
        width: 50;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #wt-branch-input {
        margin: 0 0;
    }
    """

    def __init__(self, default_branch: str, selected_branch: str):
        super().__init__()
        self._default_branch = default_branch
        self._selected_branch = selected_branch
        self._use_default = True

    def compose(self) -> ComposeResult:
        with Vertical(id="create-wt-dialog"):
            yield Static(id="wt-header")
            yield Input(placeholder="branch-name", id="wt-branch-input")
            yield Static(id="wt-footer")

    def on_mount(self) -> None:
        self._refresh_content()
        self.query_one("#wt-branch-input", Input).focus()

    def _refresh_content(self) -> None:
        if self._use_default:
            base_text = f"  [bold]{self._default_branch}[/bold]  /  [dim]{self._selected_branch}[/dim]"
        else:
            base_text = f"  [dim]{self._default_branch}[/dim]  /  [bold]{self._selected_branch}[/bold]"
        self.query_one("#wt-header", Static).update(
            f"[bold]Create worktree[/bold]\n"
        )
        self.query_one("#wt-footer", Static).update(
            f"\n  [dim]tab[/dim]  base: {base_text}\n\n"
            f"[dim]enter[/dim] create  [dim]esc[/dim] cancel"
        )

    def on_key(self, event: events.Key) -> None:
        if event.key == "tab":
            self._use_default = not self._use_default
            self._refresh_content()
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            self.dismiss(None)
            event.prevent_default()
            event.stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "wt-branch-input":
            name = event.value.strip()
            if not name:
                return
            base = self._default_branch if self._use_default else self._selected_branch
            self.dismiss((name, base))


class HelpScreen(ModalScreen[None]):
    CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-dialog {
        width: 50;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    KEYS = [
        ("enter", "Select branch"),
        ("/", "Filter branches"),
        ("a", "Toggle all repos / this repo"),
        ("x", "Pin / unpin branch"),
        ("p", "Git pull"),
        ("c", "Create worktree"),
        ("d", "Diff against default branch"),
        ("D", "Diff local changes"),
        ("ctrl+d", "Delete branch"),
        ("o", "Open in editor"),
        ("T", "Open / switch to workspace tab"),
        ("ctrl+t", "New workspace tab"),
        ("P", "Git push"),
        ("R", "Fetch all + refresh"),
        ("K", "Clear idle panes"),
        ("j/k", "Move cursor"),
        ("alt+↑/↓", "Jump to prev/next repo"),
        ("q/esc", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        lines = ["[bold]Keybindings[/bold]", ""]
        for key, desc in self.KEYS:
            lines.append(f"  [bold]{key:<12}[/bold] {desc}")
        lines.append("")
        lines.append("[dim]press any key to close[/dim]")
        with Vertical(id="help-dialog"):
            yield Static("\n".join(lines))

    def on_key(self, event: events.Key) -> None:
        self.dismiss(None)
        event.prevent_default()
        event.stop()


class GbbApp(App):
    TITLE = "gbb"

    BINDINGS = [
        Binding("question_mark", "show_help", "Help", show=True, key_display="?"),
        Binding("q", "quit_app", "Quit", show=True, key_display="q"),
        Binding("slash", "start_filter", "Filter", show=True),
        Binding("a", "toggle_scope", "All repos", show=True),
        Binding("T", "workspace", "Workspace", show=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("alt+up", "prev_group", "Prev repo", show=False),
        Binding("alt+down", "next_group", "Next repo", show=False),
        Binding("escape", "cancel", "", show=False),
        Binding("ctrl+d", "delete_branch", "Delete", show=False),
        Binding("d", "diff_main", "Diff main", show=False),
        Binding("D", "diff_local", "Diff local", show=False),
        Binding("o", "open_root", "Open", show=False),
        Binding("x", "toggle_pin", "Pin", show=False),
        Binding("p", "pull_branch", "Pull", show=False),
        Binding("K", "clear_panes", "Clear", show=False),
        Binding("ctrl+t", "new_workspace", "New ws", show=False),
        Binding("c", "create_worktree", "Create wt", show=False),
        Binding("R", "fetch_refresh", "Refresh", show=False),
        Binding("P", "push_branch", "Push", show=False),
    ]

    CSS = """
    DataTable {
        height: 1fr;
        scrollbar-size: 0 0;
    }

    DataTable > .datatable--header {
        text-style: bold;
    }

    #filter-bar {
        dock: bottom;
        height: auto;
        min-height: 1;
        display: none;
    }

    #filter-bar.visible {
        display: block;
    }

    #confirm-bar {
        dock: bottom;
        height: auto;
        min-height: 1;
        display: none;
        background: $surface;
    }

    #confirm-bar.visible {
        display: block;
    }
    """

    def __init__(
        self,
        config: Config,
        cwd: Path,
        show_all: bool = False,
    ):
        super().__init__()
        self._config = config
        self._cwd = cwd
        self._force_show_all = show_all
        self._pending_delete: tuple[str, Path, BranchInfo] | None = None
        self.filtering: bool = False
        self._repo_colors: dict[str, str] = {}
        self._all_rows: list[tuple[str, Path, BranchInfo]] = []
        self._group_indices: list[int] = []
        self.repo_data: list[tuple[str, Path, list[BranchInfo]]] = []
        self._current_repo: str | None = None
        self._show_all = show_all
        self._loading_others = False
        self._footer_timer = None
        self._pins = load_pins()
        self._kitty_mode = is_kitty()
        self.sub_title = "kitty" if self._kitty_mode else ""
        self._effective_cwd = cwd
        self._active_branch_key: str | None = None
        self._last_switch_path: Path | None = None
        self._pending_claude_windows: list[KittyWindow] = []
        self._pending_switch_path: Path | None = None
        self._pending_checkout_branch: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(cursor_type="row")
        yield Input(placeholder="/filter branches...", id="filter-bar")
        yield Static("", id="confirm-bar")
        yield Footer()

    def _update_effective_cwd(self) -> None:
        """Query sibling panes for consensus cwd. Call from background threads."""
        if not self._kitty_mode:
            return
        try:
            sibling_cwd = get_sibling_cwd()
            if sibling_cwd:
                self._effective_cwd = sibling_cwd
        except KittyError:
            pass

    def _rebuild_rows(self) -> None:
        self._all_rows = []
        self._repo_colors = {}
        for i, (name, path, branches) in enumerate(self.repo_data):
            self._repo_colors[name] = REPO_COLORS[i % len(REPO_COLORS)]
            for b in branches:
                self._all_rows.append((name, path, b))

    def _pinned_branches(self, repo_name: str) -> set[str]:
        prefix = f"{repo_name}:"
        return {key[len(prefix):] for key in self._pins if key.startswith(prefix)}

    def _is_active_row(self, repo_name: str, branch: BranchInfo) -> bool:
        key = f"{repo_name}:{branch.name}"
        return self._active_branch_key == key or branch.is_current

    def _scoped_rows(self) -> list[tuple[str, Path, BranchInfo]]:
        if not self._show_all and self._current_repo:
            rows = [r for r in self._all_rows if r[0] == self._current_repo]
        else:
            rows = list(self._all_rows)
        active = [r for r in rows if self._is_active_row(r[0], r[2])]
        rest = [r for r in rows if not self._is_active_row(r[0], r[2])]
        pinned = [r for r in rest if pin_key(r[0], r[2].name) in self._pins]
        unpinned = [r for r in rest if pin_key(r[0], r[2].name) not in self._pins]
        return active + pinned + unpinned

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(
            "", "Repo", "Branch", "Age", "Status", "HEAD±", "main±", "Path",
            "Cleanup",
        )

        self._update_effective_cwd()

        valid_repos = [p for p in self._config.repos if p.exists()]

        current_repo_path: Path | None = None
        for rp in valid_repos:
            try:
                self._cwd.relative_to(rp)
                current_repo_path = rp
                self._current_repo = rp.name
                break
            except ValueError:
                continue

        if not self._force_show_all:
            self._show_all = self._current_repo is None

        if current_repo_path and not self._force_show_all:
            branches = discover_repo(
                current_repo_path, self._config.recent_days, self._effective_cwd,
                pinned=self._pinned_branches(current_repo_path.name),
            )
            if branches:
                self.repo_data = [(current_repo_path.name, current_repo_path, branches)]
                self._rebuild_rows()
            self._populate(self._scoped_rows())

            other_repos = [rp for rp in valid_repos if rp != current_repo_path]
            if other_repos:
                self._loading_others = True
                self._discover_repos_background(other_repos)
        else:
            self._loading_others = True
            self._discover_repos_background(valid_repos)

        self._update_scope_label()
        self.set_interval(5, self._refresh_tick)
        self._footer_timer = self.set_timer(3, self._hide_footer)
        self._fetch_repos_background(valid_repos)
        table.focus()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.error:
            self.notify(
                f"Error ({event.worker.group}): {event.worker.error}",
                timeout=10,
                severity="error",
            )

    @work(thread=True, exclusive=True, group="discovery")
    def _discover_repos_background(self, repos: list[Path]) -> None:
        worker = get_current_worker()
        self._update_effective_cwd()
        cwd = self._effective_cwd
        with ThreadPoolExecutor() as pool:
            results = list(pool.map(
                lambda rp: (rp.name, rp, discover_repo(
                    rp, self._config.recent_days, cwd,
                    pinned=self._pinned_branches(rp.name),
                )),
                repos,
            ))
        if worker.is_cancelled:
            return
        new_data = [(name, path, branches) for name, path, branches in results if branches]
        self.call_from_thread(self._merge_discovered, new_data)

    def _merge_discovered(self, new_data: list[tuple[str, Path, list[BranchInfo]]]) -> None:
        existing_names = {name for name, _, _ in self.repo_data}
        for name, path, branches in new_data:
            if name not in existing_names:
                self.repo_data.append((name, path, branches))
        self._rebuild_rows()
        self._loading_others = False
        if self._show_all or self._current_repo is None:
            self._populate(self._scoped_rows())

    @work(thread=True, exclusive=True, group="fetch")
    def _fetch_repos_background(self, repos: list[Path]) -> None:
        worker = get_current_worker()
        with ThreadPoolExecutor() as pool:
            list(pool.map(fetch_repo, repos))
        if not worker.is_cancelled:
            self.call_from_thread(self._post_fetch_refresh)

    def _post_fetch_refresh(self) -> None:
        if self._pending_delete is not None or self._loading_others:
            return
        self._refresh_repos()

    def action_fetch_refresh(self) -> None:
        repos = [path for _, path, _ in self.repo_data]
        if repos:
            self.notify("Fetching...", timeout=2)
            self._fetch_repos_background(repos)

    def _refresh_tick(self) -> None:
        if self._pending_delete is not None:
            return
        if self._loading_others:
            return
        if not self.repo_data:
            return
        self._refresh_repos()

    @work(thread=True, exclusive=True, group="refresh")
    def _refresh_repos(self) -> None:
        worker = get_current_worker()
        self._update_effective_cwd()
        cwd = self._effective_cwd
        repo_paths = [(name, path) for name, path, _ in self.repo_data]
        with ThreadPoolExecutor() as pool:
            results = list(pool.map(
                lambda item: (item[0], item[1], discover_repo(
                    item[1], self._config.recent_days, cwd,
                    pinned=self._pinned_branches(item[0]),
                )),
                repo_paths,
            ))
        if worker.is_cancelled:
            return
        new_data = [(name, path, branches) for name, path, branches in results if branches]
        self.call_from_thread(self._apply_refresh, new_data)

    @staticmethod
    def _data_fingerprint(data: list[tuple[str, Path, list["BranchInfo"]]]) -> tuple:
        rows = []
        for name, _, branches in data:
            for b in branches:
                rows.append((
                    name, b.name, b.timestamp, b.dirty,
                    b.ahead_upstream, b.behind_upstream,
                    b.ahead_main, b.behind_main,
                    b.is_default, b.deletable, b.delete_reason,
                    str(b.worktree.path) if b.worktree else "",
                    b.commit,
                ))
        return tuple(rows)

    def _apply_refresh(self, new_data: list[tuple[str, Path, list[BranchInfo]]]) -> None:
        if self._pending_delete is not None:
            return

        if self._data_fingerprint(new_data) == self._data_fingerprint(self.repo_data):

            return

        table = self.query_one(DataTable)
        cursor_key: str | None = None
        cursor_row_idx = table.cursor_row
        if table.row_count > 0:
            row_keys = list(table.rows.keys())
            if cursor_row_idx < len(row_keys):
                cursor_key = str(row_keys[cursor_row_idx].value)

        self.repo_data = new_data
        self._rebuild_rows()

        for name, path, branches in self.repo_data:
            for b in branches:
                if b.is_current:
                    self._current_repo = name
                    break

        if self.filtering:
            query = self.query_one("#filter-bar", Input).value
            self._apply_filter(query)
        else:
            self._populate(self._scoped_rows())

        if cursor_key and table.row_count > 0:
            row_keys = list(table.rows.keys())
            for i, rk in enumerate(row_keys):
                if str(rk.value) == cursor_key:
                    table.move_cursor(row=i)
                    return
        if table.row_count > 0:
            table.move_cursor(row=min(cursor_row_idx, table.row_count - 1))

    def _populate(self, rows: list[tuple[str, Path, BranchInfo]]) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self._group_indices = []
        last_repo = None

        for i, (repo_name, repo_path, b) in enumerate(rows):
            if repo_name != last_repo:
                self._group_indices.append(i)
                last_repo = repo_name

            color = self._repo_colors[repo_name]

            is_pinned = pin_key(repo_name, b.name) in self._pins
            is_active = (
                self._kitty_mode
                and self._active_branch_key == f"{repo_name}:{b.name}"
            )

            # First column: state icon (single purpose)
            is_linked_worktree = b.worktree and b.worktree.path != repo_path
            if b.is_current:
                icon = Text("◉", style="#50fa7b")
            elif is_active:
                icon = Text("▸", style="#50fa7b")
            elif is_pinned:
                icon = Text("⚑", style="#f1fa8c")
            elif is_linked_worktree:
                icon = Text("⎇", style="#8be9fd")
            else:
                icon = Text(" ")

            # Branch name with optional default icon prefix
            branch_prefix = "◆ " if b.is_default else ""

            if b.worktree:
                status = Text("*", style="#ffb86c") if b.dirty else Text(" ")
            else:
                status = Text("—", style="dim")

            path_str = shorten_path(b.worktree.path) if b.worktree else ""
            wt_path = str(b.worktree.path) if b.worktree else ""

            if b.deletable:
                repo_cell = Text(repo_name, style=f"dim {color}")
                branch_cell = Text(f"{branch_prefix}{b.name}", style="dim")
                age_cell = Text(format_age(b.timestamp), style="dim")
                status_cell = Text(status.plain, style="dim")
                head_cell = Text(format_ahead_behind(b.ahead_upstream, b.behind_upstream).plain, style="dim")
                main_cell = Text(format_ahead_behind(b.ahead_main, b.behind_main).plain, style="dim")
                path_cell = Text(path_str, style="dim")
                cleanup_cell = Text(f"✕ {b.delete_reason}", style="#ff5555")
            else:
                repo_cell = Text(repo_name, style=f"bold {color}")
                branch_cell = f"{branch_prefix}{b.name}"
                age_cell = Text(format_age(b.timestamp), style="dim")
                status_cell = status
                head_cell = format_ahead_behind(b.ahead_upstream, b.behind_upstream)
                main_cell = format_ahead_behind(b.ahead_main, b.behind_main)
                path_cell = path_str
                cleanup_cell = Text("")

            table.add_row(
                icon,
                repo_cell,
                branch_cell,
                age_cell,
                status_cell,
                head_cell,
                main_cell,
                path_cell,
                cleanup_cell,
                key=f"{repo_name}:{b.name}:{wt_path}",
            )

    def _update_scope_label(self) -> None:
        label = "This repo" if self._show_all else "All repos"
        self._bindings.key_to_bindings["a"] = [
            Binding("a", "toggle_scope", label, show=True)
        ]
        self.refresh_bindings()

    def action_toggle_scope(self) -> None:
        if self._current_repo is None:
            return
        self._show_all = not self._show_all
        self._update_scope_label()
        if self._show_all and self._loading_others:
            self.notify("Loading other repos...", timeout=2)
        if self.filtering:
            query = self.query_one("#filter-bar", Input).value
            self._apply_filter(query)
        else:
            self._populate(self._scoped_rows())

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_quit_app(self) -> None:
        self.workers.cancel_all()
        self.exit()

    def action_cancel(self) -> None:
        if self._pending_delete is not None:
            self._dismiss_confirm()
        elif self.filtering:
            self._close_filter()
        else:
            self.workers.cancel_all()
            self.exit()

    def _get_cursor_row_data(self) -> tuple[str, Path, BranchInfo] | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_keys = list(table.rows.keys())
        key = str(row_keys[table.cursor_row].value)
        parts = key.split(":", 2)
        repo_name = parts[0]
        branch_name = parts[1]
        for rn, rp, b in self._all_rows:
            if rn == repo_name and b.name == branch_name:
                return (rn, rp, b)
        return None

    def action_delete_branch(self) -> None:
        if self.filtering or self._pending_delete is not None:
            return

        data = self._get_cursor_row_data()
        if not data:
            return

        repo_name, repo_path, branch = data

        if branch.is_current:
            self.notify("Cannot delete current branch", timeout=3)
            return

        for rn, rp, branches in self.repo_data:
            if rn == repo_name:
                for br in branches:
                    if br.name in ("main", "master") and branch.name == br.name:
                        self.notify("Cannot delete main branch", timeout=3)
                        return
                break

        if branch.worktree:
            self._pending_delete = (repo_name, repo_path, branch)
            self._prepare_worktree_delete(repo_name, repo_path, branch)
        elif branch.deletable:
            self._execute_delete(repo_name, repo_path, branch)
        else:
            self._pending_delete = (repo_name, repo_path, branch)
            self._show_confirm(
                f"'{branch.name}' not detected as merged. Force delete? [y/n]"
            )

    def action_clear_panes(self) -> None:
        if not self._kitty_mode:
            return
        self._do_clear_panes()

    @work(thread=True, exclusive=True, group="kitty-clear")
    def _do_clear_panes(self) -> None:
        try:
            cleared = clear_idle_panes()
        except KittyError:
            return
        if cleared:
            self.call_from_thread(
                self.notify,
                f"Cleared {cleared} pane{'s' if cleared != 1 else ''}",
                timeout=2,
            )

    def _workspace_params(self) -> tuple[str, Path, Path, str | None] | None:
        if not self._kitty_mode:
            return None
        data = self._get_cursor_row_data()
        if not data:
            return None
        repo_name, repo_path, branch = data
        if branch.worktree:
            return repo_name, repo_path, branch.worktree.path, None
        return repo_name, repo_path, repo_path, branch.name

    def action_workspace(self) -> None:
        params = self._workspace_params()
        if not params:
            return
        self._do_try_focus_or_prompt(*params)

    def action_new_workspace(self) -> None:
        params = self._workspace_params()
        if not params:
            return
        self._show_workspace_options(params, force_new=True)

    def action_create_worktree(self) -> None:
        if self.filtering or self._pending_delete is not None:
            return
        data = self._get_cursor_row_data()
        if not data:
            return
        repo_name, repo_path, branch = data
        main_branch = detect_main_branch(repo_path)
        if not main_branch:
            self.notify("Could not detect main branch", timeout=3)
            return

        def on_result(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            branch_name, base = result
            self._do_create_worktree(repo_name, repo_path, branch_name, base)

        self.push_screen(
            CreateWorktreeScreen(main_branch, branch.name),
            on_result,
        )

    @work(thread=True, exclusive=True, group="create-worktree")
    def _do_create_worktree(
        self, repo_name: str, repo_path: Path, branch_name: str, base: str,
    ) -> None:
        sanitized = branch_name.replace("/", "-")
        worktree_path = repo_path.parent / f"{repo_path.name}.{sanitized}"
        if worktree_path.exists():
            self.call_from_thread(
                self.notify, f"Directory already exists: {worktree_path.name}", timeout=5,
            )
            return
        err = create_worktree(repo_path, branch_name, base, worktree_path)
        if err:
            self.call_from_thread(self.notify, f"Error: {err}", timeout=5)
            return
        self.call_from_thread(
            self.notify, f"Created worktree: {worktree_path.name}", timeout=3,
        )
        # Refresh table then open workspace tab
        self._update_effective_cwd()
        self.call_from_thread(self._post_create_worktree, repo_name, repo_path, worktree_path)

    def _post_create_worktree(
        self, repo_name: str, repo_path: Path, worktree_path: Path,
    ) -> None:
        self._refresh_repos()
        if self._kitty_mode:
            self._show_workspace_options(
                (repo_name, repo_path, worktree_path, None),
                force_new=False,
                claude_flag=None,
            )

    @work(thread=True, exclusive=True, group="kitty-workspace")
    def _do_try_focus_or_prompt(
        self, repo_name: str, repo_path: Path, selected_dir: Path, checkout_branch: str | None,
    ) -> None:
        try:
            if focus_repo_tab(repo_name):
                self.call_from_thread(
                    self.notify, f"Switched to {repo_name}", timeout=2,
                )
                return
        except KittyError as e:
            self.call_from_thread(self.notify, f"Workspace failed: {e}", timeout=5)
            return
        self.call_from_thread(
            self._show_workspace_options,
            (repo_name, repo_path, selected_dir, checkout_branch),
            False,
        )

    def _show_workspace_options(
        self,
        params: tuple[str, Path, Path, str | None],
        force_new: bool,
        claude_flag: str | None = "continue",
    ) -> None:
        def on_result(ws_config: WorkspaceConfig | None) -> None:
            if ws_config is None:
                return
            self._config.workspace = ws_config
            self._config.save_workspace()
            self._do_create_workspace(
                *params, ws_config=ws_config,
                use_numbered_title=force_new, claude_flag=claude_flag,
            )

        self.push_screen(
            WorkspaceOptionsScreen(self._config.workspace),
            on_result,
        )

    @work(thread=True, exclusive=True, group="kitty-workspace")
    def _do_create_workspace(
        self,
        repo_name: str,
        repo_path: Path,
        selected_dir: Path,
        checkout_branch: str | None,
        ws_config: WorkspaceConfig | None = None,
        use_numbered_title: bool = False,
        claude_flag: str | None = "continue",
    ) -> None:
        ws = ws_config or self._config.workspace
        try:
            title = next_tab_title(repo_name) if use_numbered_title else repo_name
            create_workspace_tab(
                repo_name, repo_path, selected_dir, checkout_branch,
                tab_title=title, start_claude=ws.start_claude,
                claude_flag=claude_flag,
            )
        except KittyError as e:
            self.call_from_thread(self.notify, f"Workspace failed: {e}", timeout=5)
            return
        self.call_from_thread(
            self.notify, f"Workspace opened: {title}", timeout=3,
        )

    def action_open_root(self) -> None:
        data = self._get_cursor_row_data()
        if not data:
            return
        repo_name, repo_path, branch = data
        path = branch.worktree.path if branch.worktree else repo_path
        subprocess.Popen(
            ["subl", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _run_in_pager(self, cmd: list[str], cwd: Path) -> None:
        with self.suspend():
            diff = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
            if not diff.stdout.strip():
                print("(no changes)")
                input("Press enter to continue...")
            else:
                pager = subprocess.Popen(
                    ["less", "-R"],
                    stdin=subprocess.PIPE,
                )
                pager.communicate(input=diff.stdout.encode())

    def action_diff_main(self) -> None:
        data = self._get_cursor_row_data()
        if not data:
            return
        repo_name, repo_path, branch = data
        main_branch = detect_main_branch(repo_path)
        if not main_branch:
            self.notify("No default branch found", timeout=3)
            return
        self._run_in_pager(
            ["git", "-c", "color.diff=always", "diff", f"{main_branch}...{branch.name}"],
            cwd=repo_path,
        )

    def action_diff_local(self) -> None:
        data = self._get_cursor_row_data()
        if not data:
            return
        repo_name, repo_path, branch = data
        if not branch.worktree:
            self.notify("No worktree — no local changes", timeout=3)
            return
        self._run_in_pager(
            ["git", "-c", "color.diff=always", "diff"],
            cwd=branch.worktree.path,
        )

    def action_pull_branch(self) -> None:
        data = self._get_cursor_row_data()
        if not data:
            return
        repo_name, repo_path, branch = data
        if not branch.worktree:
            self.notify("No worktree — cannot pull", timeout=3)
            return
        self.notify(f"Pulling {branch.name}...", timeout=2)
        self._do_pull(repo_name, repo_path, branch)

    @work(thread=True, exclusive=True, group="pull")
    def _do_pull(self, repo_name: str, repo_path: Path, branch: BranchInfo) -> None:
        result = subprocess.run(
            ["git", "-C", str(branch.worktree.path), "pull"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            self.call_from_thread(
                self.notify, f"Pull failed: {result.stderr.strip()}", timeout=5,
            )
            return
        self.call_from_thread(
            self.notify, f"Pulled {branch.name}", timeout=3,
        )

    def action_push_branch(self) -> None:
        data = self._get_cursor_row_data()
        if not data:
            return
        repo_name, repo_path, branch = data
        if not branch.worktree:
            self.notify("No worktree — cannot push", timeout=3)
            return
        self.notify(f"Pushing {branch.name}...", timeout=2)
        self._do_push(repo_name, repo_path, branch)

    @work(thread=True, exclusive=True, group="push")
    def _do_push(self, repo_name: str, repo_path: Path, branch: BranchInfo) -> None:
        result = subprocess.run(
            ["git", "-C", str(branch.worktree.path), "push"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            # Try push with --set-upstream for new branches
            result = subprocess.run(
                ["git", "-C", str(branch.worktree.path), "push", "--set-upstream", "origin", branch.name],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                self.call_from_thread(
                    self.notify, f"Push failed: {result.stderr.strip()}", timeout=5,
                )
                return
        self.call_from_thread(
            self.notify, f"Pushed {branch.name}", timeout=3,
        )

    def action_toggle_pin(self) -> None:
        if self.filtering or self._pending_delete is not None:
            return
        data = self._get_cursor_row_data()
        if not data:
            return
        repo_name, repo_path, branch = data
        key = pin_key(repo_name, branch.name)
        if key in self._pins:
            self._pins.discard(key)
        else:
            self._pins.add(key)
        save_pins(self._pins)
        if self.filtering:
            query = self.query_one("#filter-bar", Input).value
            self._apply_filter(query)
        else:
            self._populate(self._scoped_rows())

    @work(thread=True, exclusive=True, group="delete-check")
    def _prepare_worktree_delete(
        self, repo_name: str, repo_path: Path, branch: BranchInfo
    ) -> None:
        dirty = is_dirty(branch.worktree.path)
        entries = list_non_ignored_entries(
            branch.worktree.path, self._config.worktree_ignore
        )
        self.call_from_thread(
            self._show_delete_dialog, repo_name, repo_path, branch, dirty, entries
        )

    def _show_delete_dialog(
        self,
        repo_name: str,
        repo_path: Path,
        branch: BranchInfo,
        dirty: bool,
        entries: list[str],
    ) -> None:
        reasons: list[str] = []
        if dirty:
            reasons.append("uncommitted changes")
        if entries:
            reasons.append("untracked files")
        if not branch.deletable:
            reasons.append("not detected as merged")

        if not reasons:
            self._execute_delete(repo_name, repo_path, branch)
            return

        def on_result(confirmed: bool) -> None:
            self._pending_delete = None
            if confirmed:
                self._execute_delete(repo_name, repo_path, branch)

        self.push_screen(
            DeleteConfirmScreen(
                branch.name,
                shorten_path(branch.worktree.path),
                reasons,
                entries,
            ),
            on_result,
        )

    def _execute_delete(
        self, repo_name: str, repo_path: Path, branch: BranchInfo
    ) -> None:
        self._pending_delete = None
        self.notify(f"Deleting {branch.name}...", timeout=2)
        self._do_delete(repo_name, repo_path, branch)

    @work(thread=True, exclusive=True, group="delete")
    def _do_delete(
        self, repo_name: str, repo_path: Path, branch: BranchInfo
    ) -> None:
        if branch.worktree:
            err = delete_worktree(repo_path, branch.worktree.path)
            if err:
                self.call_from_thread(self.notify, f"Error: {err}", timeout=5)
                return
        err = delete_branch(repo_path, branch.name, force=True)
        if err:
            self.call_from_thread(self.notify, f"Error: {err}", timeout=5)
            return
        self.call_from_thread(self._remove_row, repo_name, branch.name)
        self.call_from_thread(self.notify, f"Deleted {branch.name}", timeout=3)

    def _show_confirm(self, message: str) -> None:
        bar = self.query_one("#confirm-bar", Static)
        bar.update(message)
        bar.add_class("visible")

    def _dismiss_confirm(self) -> None:
        self._pending_delete = None
        bar = self.query_one("#confirm-bar", Static)
        bar.remove_class("visible")
        bar.update("")

    def _remove_row(self, repo_name: str, branch_name: str) -> None:
        self._all_rows = [
            (rn, rp, b) for rn, rp, b in self._all_rows
            if not (rn == repo_name and b.name == branch_name)
        ]
        for i, (rn, rp, branches) in enumerate(self.repo_data):
            if rn == repo_name:
                self.repo_data[i] = (
                    rn, rp, [b for b in branches if b.name != branch_name]
                )
                break
        if self.filtering:
            query = self.query_one("#filter-bar", Input).value
            self._apply_filter(query)
        else:
            self._populate(self._scoped_rows())

    def _show_footer_briefly(self) -> None:
        footer = self.query_one(Footer)
        footer.display = True
        if self._footer_timer is not None:
            self._footer_timer.stop()
        self._footer_timer = self.set_timer(3, self._hide_footer)

    def _hide_footer(self) -> None:
        self.query_one(Footer).display = False
        self._footer_timer = None

    def on_key(self, event: events.Key) -> None:
        self._show_footer_briefly()
        if self._pending_delete is not None:
            bar = self.query_one("#confirm-bar", Static)
            if not bar.has_class("visible"):
                return
            if event.key == "y":
                repo_name, repo_path, branch = self._pending_delete
                self._dismiss_confirm()
                self._execute_delete(repo_name, repo_path, branch)
            elif event.key in ("n", "escape"):
                self._dismiss_confirm()
            event.prevent_default()
            event.stop()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        parts = key.split(":", 2)
        repo_name = parts[0] if len(parts) > 0 else ""
        branch_name = parts[1] if len(parts) > 1 else ""
        wt_path = parts[2] if len(parts) > 2 else ""

        # Skip if already on this branch
        for rn, _, b in self._all_rows:
            if rn == repo_name and b.name == branch_name and self._is_active_row(rn, b):
                return

        has_worktree = bool(wt_path)

        if self._kitty_mode:
            self._active_branch_key = f"{repo_name}:{branch_name}"
            self._repopulate(follow_key=key)
            if has_worktree:
                self._do_kitty_switch(Path(wt_path))
            else:
                repo_path = None
                for name, rp, _ in self.repo_data:
                    if name == repo_name:
                        repo_path = rp
                        break
                if repo_path:
                    self._do_kitty_switch(repo_path, checkout_branch=branch_name)
            return

        if wt_path:
            path = wt_path
        else:
            path = ""
            for name, repo_path, _ in self.repo_data:
                if name == repo_name:
                    path = str(repo_path)
                    break

        self.exit((path, branch_name, has_worktree))

    def _repopulate(self, follow_key: str | None = None) -> None:
        table = self.query_one(DataTable)
        cursor_row = table.cursor_row
        if follow_key is None and table.row_count > 0:
            row_keys = list(table.rows.keys())
            if cursor_row < len(row_keys):
                follow_key = str(row_keys[cursor_row].value)
        if self.filtering:
            query = self.query_one("#filter-bar", Input).value
            self._apply_filter(query)
        else:
            self._populate(self._scoped_rows())
        if follow_key and table.row_count > 0:
            for i, rk in enumerate(table.rows.keys()):
                if str(rk.value) == follow_key:
                    table.move_cursor(row=i)
                    return
        if table.row_count > 0:
            table.move_cursor(row=min(cursor_row, table.row_count - 1))

    @work(thread=True, exclusive=True, group="kitty-switch")
    def _do_kitty_switch(self, target_path: Path, checkout_branch: str | None = None) -> None:
        try:
            result = switch_all_panes(target_path, checkout_branch)
        except KittyError as e:
            self.call_from_thread(self.notify, str(e), timeout=5)
            return
        self.call_from_thread(self._handle_switch_result, result, target_path, checkout_branch)

    def _handle_switch_result(self, result, target_path: Path, checkout_branch: str | None = None) -> None:
        dir_changed = self._last_switch_path != target_path
        self._last_switch_path = target_path

        parts = []
        if result.switched:
            n = result.switched
            parts.append(f"Switched {n} pane{'s' if n != 1 else ''}")
        if result.skipped:
            parts.append(f"Skipped: {', '.join(result.skipped)}")
        if parts:
            self.notify(". ".join(parts), timeout=3)

        if result.claude_windows and dir_changed:
            self._pending_claude_windows = result.claude_windows
            self._pending_switch_path = target_path
            self._pending_checkout_branch = checkout_branch

            def on_claude_confirm(choice: str) -> None:
                if choice == "kill":
                    self._kill_claude_panes()
                elif choice:
                    self._restart_claude_panes(claude_flag=choice)
                else:
                    self._pending_claude_windows = []
                    self._pending_switch_path = None
                    self._pending_checkout_branch = None

            self.push_screen(
                ClaudeConfirmScreen(len(result.claude_windows)),
                on_claude_confirm,
            )

    @work(thread=True, exclusive=True, group="kitty-claude")
    def _restart_claude_panes(self, claude_flag: str = "continue") -> None:
        windows = list(self._pending_claude_windows)
        path = self._pending_switch_path
        checkout = self._pending_checkout_branch
        self._pending_claude_windows = []
        self._pending_switch_path = None
        self._pending_checkout_branch = None
        restarted = 0
        for w in windows:
            if restart_claude_pane(w, path, checkout, claude_flag=claude_flag):
                restarted += 1
        if restarted:
            self.call_from_thread(
                self.notify,
                f"Restarted claude --{claude_flag} in {restarted} pane{'s' if restarted != 1 else ''}",
                timeout=3,
            )

    @work(thread=True, exclusive=True, group="kitty-claude")
    def _kill_claude_panes(self) -> None:
        windows = list(self._pending_claude_windows)
        path = self._pending_switch_path
        checkout = self._pending_checkout_branch
        self._pending_claude_windows = []
        self._pending_switch_path = None
        self._pending_checkout_branch = None
        killed = 0
        for w in windows:
            # Kill claude then cd to target (no restart)
            for pid in reversed(w.pids):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            killed += 1
        if killed:
            # Wait briefly for processes to exit
            time.sleep(0.5)
            for w in windows:
                if path:
                    switch_pane(w.id, path, checkout)
            self.call_from_thread(
                self.notify,
                f"Killed claude in {killed} pane{'s' if killed != 1 else ''}",
                timeout=3,
            )

    def action_cursor_down(self) -> None:
        self._show_footer_briefly()
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        if table.cursor_row >= table.row_count - 1:
            table.move_cursor(row=0)
        else:
            table.action_cursor_down()

    def action_cursor_up(self) -> None:
        self._show_footer_briefly()
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        if table.cursor_row <= 0:
            table.move_cursor(row=table.row_count - 1)
        else:
            table.action_cursor_up()

    def action_prev_group(self) -> None:
        self._show_footer_briefly()
        table = self.query_one(DataTable)
        current = table.cursor_row
        for idx in reversed(self._group_indices):
            if idx < current:
                table.move_cursor(row=idx)
                return
        if self._group_indices:
            table.move_cursor(row=self._group_indices[-1])

    def action_next_group(self) -> None:
        self._show_footer_briefly()
        table = self.query_one(DataTable)
        current = table.cursor_row
        for idx in self._group_indices:
            if idx > current:
                table.move_cursor(row=idx)
                return
        if self._group_indices:
            table.move_cursor(row=self._group_indices[0])

    def action_start_filter(self) -> None:
        if self.filtering:
            return
        self.filtering = True
        filter_bar = self.query_one("#filter-bar", Input)
        filter_bar.add_class("visible")
        filter_bar.value = ""
        filter_bar.focus()

    def _close_filter(self) -> None:
        self.filtering = False
        filter_bar = self.query_one("#filter-bar", Input)
        filter_bar.remove_class("visible")
        filter_bar.value = ""
        self._populate(self._scoped_rows())
        table = self.query_one(DataTable)
        table.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-bar":
            self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter-bar":
            self.filtering = False
            filter_bar = self.query_one("#filter-bar", Input)
            filter_bar.remove_class("visible")
            table = self.query_one(DataTable)
            table.focus()

    def _apply_filter(self, query: str) -> None:
        rows = self._scoped_rows()
        query = query.lower()
        if query:
            filtered = [
                row for row in rows
                if query in row[2].name.lower() or query in row[0].lower()
            ]
        else:
            filtered = rows
        self._populate(filtered)
