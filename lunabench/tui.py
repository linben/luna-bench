"""A bounded, main-thread curses dashboard for foreground daemon runs."""

from __future__ import annotations

import curses
import math


def _plain(value: object, width: int) -> str:
    """Escape non-ASCII text so both terminal controls and cell widths are safe."""
    if width <= 0:
        return ""
    parts: list[str] = []
    length = 0
    for char in str(value):
        part = char if " " <= char <= "~" else char.encode("unicode_escape").decode("ascii")
        parts.append(part)
        length += len(part)
        if length > width:
            return "".join(parts)[:max(0, width - 3)] + "." * min(3, width)
    return "".join(parts)


def _duration(value: object, *, countdown: bool = False) -> str:
    if value is None:
        return "--"
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return "--"
    if not math.isfinite(seconds):
        return "--"
    total = max(0, math.ceil(seconds) if countdown else int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02}:{minutes:02}:{seconds:02}"
    if hours:
        return f"{hours:02}:{minutes:02}:{seconds:02}"
    return f"{minutes:02}:{seconds:02}"


class Dashboard:
    """Own terminal setup and restoration; all methods run in the main thread."""

    def __init__(self) -> None:
        self._screen: curses.window | None = None
        self._active = False
        self._cursor: int | None = None
        self._escape_delay: int | None = None
        self._colors: dict[str, int] = {}

    def __enter__(self) -> Dashboard:
        if self._active:
            raise RuntimeError("dashboard is already active")
        self._active = True
        try:
            self._screen = curses.initscr()
            curses.noecho()
            curses.cbreak()
            self._screen.keypad(True)
            self._screen.nodelay(True)
            if hasattr(curses, "get_escdelay") and hasattr(curses, "set_escdelay"):
                self._escape_delay = curses.get_escdelay()
                curses.set_escdelay(25)
            try:
                self._cursor = curses.curs_set(0)
            except curses.error:
                # Some terminals cannot change cursor visibility.
                pass
            self._init_colors()
            return self
        except BaseException:
            self._restore()
            raise

    def _init_colors(self) -> None:
        self._colors.clear()
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            background = curses.COLOR_BLACK
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                pass
            colors = (
                ("heading", curses.COLOR_CYAN),
                ("running", curses.COLOR_GREEN),
                ("waiting", curses.COLOR_CYAN),
                ("stopping", curses.COLOR_YELLOW),
                ("error", curses.COLOR_RED),
            )
            for pair, (name, color) in enumerate(colors, 1):
                if pair >= curses.COLOR_PAIRS or color >= curses.COLORS:
                    continue
                curses.init_pair(pair, color, background)
                self._colors[name] = curses.color_pair(pair)
        except curses.error:
            # Color support is optional, including on misconfigured terminals.
            self._colors.clear()

    def _restore(self) -> None:
        if not self._active:
            return
        self._active = False
        screen, self._screen = self._screen, None
        actions = []
        if screen is not None:
            actions.extend(((screen.nodelay, (False,)), (screen.keypad, (False,))))
        if self._cursor is not None:
            actions.append((curses.curs_set, (self._cursor,)))
            self._cursor = None
        actions.extend(((curses.nocbreak, ()), (curses.echo, ()), (curses.endwin, ())))
        if self._escape_delay is not None:
            actions.append((curses.set_escdelay, (self._escape_delay,)))
            self._escape_delay = None
        for action, args in actions:
            try:
                action(*args)
            except (curses.error, OSError):
                # Attempt every restoration step even after partial setup or a
                # disconnected terminal; never mask an exception from the body.
                pass

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._restore()

    def poll(self) -> str | None:
        screen = self._screen
        if screen is None:
            return None
        # Bound input draining so held keys cannot starve rendering or shutdown.
        for _ in range(32):
            key = screen.getch()
            if key == -1:
                break
            if key in (ord("q"), ord("Q"), 27):
                return "quit"
            if key == curses.KEY_RESIZE:
                curses.update_lines_cols()
                screen.clearok(True)
        return None

    def render(self, snapshot: dict) -> None:
        screen = self._screen
        if screen is None:
            return
        rows, columns = screen.getmaxyx()
        screen.erase()
        if rows <= 0 or columns <= 1:
            screen.refresh()
            return

        # A spare column avoids curses' bottom-right write/scroll behavior.
        width = columns - 1

        def put(row: int, text: object, style: int = 0) -> None:
            if not 0 <= row < rows:
                return
            safe = _plain(text, width)
            if not safe:
                return
            try:
                screen.addstr(row, 0, safe, style)
            except curses.error:
                # Only tolerate a boundary invalidated by a concurrent resize;
                # genuine screen errors must reach the daemon's error handling.
                current_rows, current_columns = screen.getmaxyx()
                if row < current_rows and len(safe) < current_columns:
                    raise

        state = str(snapshot.get("state") or "starting")
        bold = curses.A_BOLD
        state_style = self._colors.get(state, self._colors.get("heading", 0)) | bold
        heading_style = self._colors.get("heading", 0) | bold
        if rows == 1:
            put(0, f"{state.upper()} | q: quit", state_style)
            screen.refresh()
            return

        next_in = snapshot.get("next_in_s")
        next_run = "--" if next_in is None else f"in {_duration(next_in, countdown=True)}"
        if state == "stopping":
            next_run = "disabled; draining requests"
        interval = _duration(snapshot.get("interval_s"))
        target = snapshot.get("target") or "--"
        api = snapshot.get("api") or "--"
        last_status = snapshot.get("last_status") or "--"
        error_style = self._colors.get("error", 0) if snapshot.get("errors") else 0
        lines = (
            (f"LUNABENCH DAEMON | {state.upper()}", state_style),
            (f"Cadence: {interval} | Next run: {next_run}", 0),
            (f"Suite: {snapshot.get('cycle', 0)} | Completed: {snapshot.get('completed', 0)}"
             f" | Failed: {snapshot.get('failed', 0)}", 0),
            (f"Session: {_duration(snapshot.get('elapsed_s'))}"
             f" | Run: {_duration(snapshot.get('run_elapsed_s'))}", 0),
            (f"Target: {target}", 0),
            (f"API: {api}", 0),
            (f"Samples: {snapshot.get('samples', 0)} | Errors: {snapshot.get('errors', 0)}"
             f" | Dropped: {snapshot.get('dropped', 0)}", error_style),
            (f"Last status: {last_status}", 0),
            (f"Started (UTC): {snapshot.get('started_at') or '--'}", 0),
            (f"Log directory: {snapshot.get('log_dir') or '--'}", 0),
            (f"Samples JSONL: {snapshot.get('samples_path') or '--'}", 0),
            (f"Report: {snapshot.get('report_path') or '--'}", 0),
            ("RECENT EVENTS", heading_style),
        )
        content_rows = rows - 1
        for row, (text, style) in enumerate(lines[:content_rows]):
            put(row, text, style)
        recent_rows = content_rows - len(lines)
        recent = snapshot.get("recent") or []
        if recent_rows > 0:
            # The core supplies a bounded list. Select only the visible tail,
            # anchoring newest events above the footer for a stable log area.
            visible = recent[-recent_rows:]
            first_row = content_rows - len(visible)
            for row, message in enumerate(visible, first_row):
                put(row, message)
        help_text = "q / Esc: stop after active requests drain | Ctrl-C: stop"
        if width < len(help_text):
            help_text = "q / Esc: stop (drain requests)"
        if width < len(help_text):
            help_text = "q: quit"
        put(rows - 1, help_text, curses.A_DIM)
        screen.refresh()
