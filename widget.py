"""Claude on the Windows desktop: a tray icon showing the session % and a small panel with plan
usage and every running Claude Code session.

Run with pythonw.exe so no console window opens. Left-click the tray icon to show or hide the
panel; drag the panel anywhere; right-click either for the menu. Click a session to go to its
terminal; right-click it for more.
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import winreg
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont, ImageTk

import focus
import sessions
import usage

APP = "claude_usage_widget"
POLL_SECONDS = 180
RETRY_SECONDS = 30  # after a network failure, e.g. at login or resume before the network is up
BACKOFF_SECONDS = 600
TICK_MS = 30_000  # re-render countdowns between fetches
SESSION_SECONDS = 2
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP
SETTINGS_FILE = DATA_DIR / "settings.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClaudeUsageWidget"

# Dark-mode tokens from the dataviz reference palette. Meter fills carry severity; the track
# is a dim step of the same fill so the state reads across the whole bar.
FONT = "Segoe UI"
SURFACE = "#1a1a19"
BORDER = "#383835"
TIP_SURFACE = "#262624"
INK = "#f0efec"
INK_2 = "#c3c2b7"
MUTED = "#898781"
FILL = {"normal": "#3987e5", "warning": "#fab219", "critical": "#d03b3b", "stale": "#898781"}
TRACK_MIX = 0.28
PANEL_WIDTH = 340
BAR_HEIGHT = 8
ERROR_SHORT = {"login": "Login expired", "rate_limited": "Rate limited", "network": "Offline",
               "format": "Unexpected data"}

# Sessions: the dot and state text carry the state; the small bar is context fill.
STATE_COLOURS = {sessions.NEEDS_YOU: FILL["critical"], sessions.STALLED: FILL["critical"],
                 sessions.YOUR_TURN: FILL["warning"], sessions.RUNNING: "#3fae6a", sessions.UNKNOWN: MUTED}
STATE_DOTS = {sessions.STALLED: "○"}  # hollow; every other state is a solid ●
MAX_ROWS = 8  # more scroll with the mouse wheel
MINI_BAR_WIDTH = 44
MINI_BAR_HEIGHT = 6
CONTEXT_WARNING_AT = 60  # same thresholds as claude-context-status
CONTEXT_CRITICAL_AT = 80
CLICK_SLOP = 4  # pixels a press may move and still count as a click, not a drag
FLASH_SECONDS = 4
TIP_DELAY_MS = 500

log = logging.getLogger(APP)


def blend(base, top, t):
    a = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(top[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def status_text(limit):
    if limit.percent >= 100:
        return "Limit reached"
    return {"warning": "Near limit", "critical": "Almost out"}.get(limit.severity)


def load_font(size):
    try:
        return ImageFont.truetype("segoeuib.ttf", size)
    except OSError:
        return ImageFont.load_default(size)


def tray_image(text, severity, dot=False):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=12, fill=FILL[severity])
    ink = "#0b0b0b" if severity == "warning" else "#ffffff"
    draw.text((size / 2, size / 2), text, fill=ink, font=load_font(46 if len(text) <= 2 else 32), anchor="mm")
    if dot:
        # A session needs you. The white ring keeps the dot visible on the red critical square.
        draw.ellipse((size - 25, 0, size - 1, 24), fill=FILL["critical"], outline="#ffffff", width=3)
    return img


def bar_image(width, height, percent, severity):
    s = 4  # draw at 4x and downsample for smooth rounded ends
    w, h = width * s, height * s
    img = Image.new("RGB", (w, h), SURFACE)
    draw = ImageDraw.Draw(img)
    fill = FILL[severity]
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=blend(SURFACE, fill, TRACK_MIX))
    filled = round(w * min(max(percent, 0), 100) / 100)
    if filled > 0:
        draw.rounded_rectangle((0, 0, max(filled, h) - 1, h - 1), radius=h // 2, fill=fill)
    return img.resize((width, height), Image.LANCZOS)


def context_severity(percent):
    if percent is None:
        return "stale"
    return "critical" if percent >= CONTEXT_CRITICAL_AT else "warning" if percent >= CONTEXT_WARNING_AT else "normal"


def fit_text(font, text, width):
    """`text`, cut with an ellipsis to fit `width` pixels. Tk labels never elide by themselves."""
    if not text or font.measure(text) <= width:
        return text or ""
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…" if text else ""


def state_text(s, now):
    text = sessions.STATE_LABELS[s.state]
    if s.since:
        text += " " + sessions.format_elapsed((now - s.since).total_seconds())
    if s.waiting_for:
        text += f" · {s.waiting_for}"
    if s.compacted:
        text += " · compacted"
    if s.background:
        text += " · bg"
    return text


def session_tip(s, now):
    """Hover text for a session row."""
    lines = [s.title or s.name, s.cwd]
    lines.append(f"{sessions.STATE_LABELS[s.state]}" + (f": {s.waiting_for}" if s.waiting_for else "")
                 + (f", last transcript write {sessions.format_elapsed((now - s.last_write).total_seconds())} ago"
                    if s.last_write else ""))
    if s.compacted:
        lines.append("Context: compacted since the last reply")
    elif s.context is None:
        lines.append("Context: no reply yet")
    else:
        lines.append(f"Context: {s.context:,} of {s.window:,} tokens ({s.context_percent:.1f}%)")
    lines.append(f"Window from {s.window_source}")
    t = s.main + s.subagents
    lines.append(f"Session total: {t.total:,} tokens")
    lines.append(f"  input {t.input:,}, output {t.output:,}, cache read {t.cache_read:,}, cache write {t.cache_write:,}")
    if s.subagents.total:
        lines.append(f"  of which subagents {s.subagents.total:,}")
    details = [s.model, s.entrypoint, f"PID {s.pid}"]
    if s.started_at:
        details.insert(0, f"started {s.started_at.astimezone():%a} {usage.format_clock(s.started_at)}")
    lines.append(", ".join(d for d in details if d))
    lines.append(s.session_id)
    lines.append("Click: go to its terminal. Right-click: more.")
    return "\n".join(lines)


def next_poll_delay(result):
    if isinstance(result, usage.UsageError):
        return {"rate_limited": BACKOFF_SECONDS, "network": RETRY_SECONDS}.get(result.kind, POLL_SECONDS)
    return POLL_SECONDS


def update(widget, **options):
    """configure() only the options that changed, so an unchanged widget is never redrawn."""
    changed = {k: v for k, v in options.items() if str(widget.cget(k)) != str(v)}
    if changed:
        widget.configure(**changed)


def load_settings():
    settings = {"x": None, "y": None, "visible": True, "topmost": True,
                "notify_attention": True, "notify_finished": True, "stalled_minutes": 5}
    try:
        settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return settings


def stalled_after(settings):
    try:
        return timedelta(minutes=max(1.0, float(settings.get("stalled_minutes"))))
    except (TypeError, ValueError):
        return sessions.STALLED_AFTER


def startup_command():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{exe}" "{Path(__file__).resolve()}"'


def starts_with_windows():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def set_starts_with_windows(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def work_area(x, y):
    """(left, top, right, bottom) of the work area (no taskbar) of the monitor nearest x, y."""
    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromPoint(wintypes.POINT(x, y), 2), ctypes.byref(info))
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


class App:
    def __init__(self):
        self.settings = load_settings()
        self.events = queue.Queue()
        self.wake = threading.Event()
        self.data = None
        self.error = None
        self.sessions = []
        self.alerts = sessions.Alerts()
        self.offset = 0  # first session row shown when there are more than MAX_ROWS
        self.flash = None  # (text, monotonic deadline) shown in the Sessions header
        self.layout = None
        self.placed = None
        self.drag = None
        self.press = None  # (x_root, y_root, row slot or None) of the last left press
        self.tray_state = None
        self.tray_thread = None
        self.now = lambda: datetime.now(timezone.utc)

        self.root = tk.Tk()
        # Tk's default prints the traceback to stderr, which pythonw throws away.
        self.root.report_callback_exception = lambda *exc: log.error("Tk callback failed", exc_info=exc)
        self.root.title("Claude usage")
        self.root.overrideredirect(True)
        self.root.configure(bg=BORDER)
        self.scale = self.root.winfo_fpixels("1i") / 96
        self.body = tk.Frame(self.root, bg=SURFACE, padx=self.px(14), pady=self.px(10))
        self.body.pack(padx=1, pady=1, fill="both", expand=True)
        self.fonts = {size: tkfont.Font(root=self.root, family=FONT, size=size) for size in (8, 9, 10)}

        self.topmost = tk.BooleanVar(value=self.settings["topmost"])
        self.context = tk.Menu(self.root, tearoff=False)
        self.context.add_command(label="Refresh now", command=self.wake.set)
        self.context.add_checkbutton(label="Keep on top", variable=self.topmost, command=self.apply_topmost)
        self.context.add_command(label="Hide panel", command=lambda: self.set_visible(False))
        self.context.add_separator()
        self.context.add_command(label="Quit", command=self.quit)
        self.row_menu = tk.Menu(self.root, tearoff=False)
        self.tip = Tip(self)

        self.root.bind_all("<ButtonPress-1>", self.drag_start)
        self.root.bind_all("<B1-Motion>", self.drag_move)
        self.root.bind_all("<ButtonRelease-1>", self.release)
        self.root.bind_all("<Button-3>", self.popup)
        self.root.bind_all("<MouseWheel>", self.scroll)

        self.icon = pystray.Icon(APP, tray_image("?", "stale"), "Claude usage: loading", menu=pystray.Menu(
            pystray.MenuItem("Show panel", lambda: self.post("toggle"), default=True,
                             checked=lambda item: self.settings["visible"]),
            pystray.MenuItem("Refresh now", lambda: self.wake.set()),
            pystray.MenuItem("Keep panel on top", lambda: self.post("topmost"),
                             checked=lambda item: self.settings["topmost"]),
            pystray.MenuItem("Notify when a session needs you", lambda: self.post("notify", "notify_attention"),
                             checked=lambda item: self.settings["notify_attention"]),
            pystray.MenuItem("Notify when a long turn finishes", lambda: self.post("notify", "notify_finished"),
                             checked=lambda item: self.settings["notify_finished"]),
            pystray.MenuItem("Start with Windows", lambda: self.post("startup"),
                             checked=lambda item: starts_with_windows()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.post("quit")),
        ))

        x, y = self.settings["x"], self.settings["y"]
        self.pos = (x, y) if x is not None and y is not None else None
        if not self.settings["visible"]:
            self.root.withdraw()  # before the first render, which would otherwise map it briefly
        self.apply_topmost()
        self.render()

    def px(self, value):
        return round(value * self.scale)

    def post(self, *event):
        # pystray callbacks run on the tray thread; Tk is only touched from the main thread.
        self.events.put(event)

    def run(self):
        self.tray_thread = threading.Thread(target=self.icon.run, daemon=True)
        self.tray_thread.start()
        threading.Thread(target=self.poll, daemon=True).start()
        threading.Thread(target=self.watch, daemon=True).start()
        self.root.after(250, self.drain)
        self.root.after(TICK_MS, self.tick)
        self.root.mainloop()

    def poll(self):
        while True:
            try:
                result = usage.fetch_usage()
            except usage.UsageError as e:
                log.warning("fetch: %s", e)
                result = e
            except Exception as e:  # keep polling whatever happens
                log.exception("fetch failed")
                result = usage.UsageError("error", f"Unexpected error ({e.__class__.__name__}).")
            self.post("data", result)
            self.wake.wait(next_poll_delay(result))
            self.wake.clear()

    def watch(self):
        """Session scans run off the Tk thread: the first one reads every transcript in full."""
        monitor = sessions.Monitor(stalled_after=stalled_after(self.settings))
        last_error = None
        while True:
            try:
                self.post("sessions", monitor.scan())
                last_error = None
            except Exception as e:  # keep watching whatever happens; log each new failure once
                if repr(e) != last_error:
                    log.exception("session scan failed")
                    last_error = repr(e)
            time.sleep(SESSION_SECONDS)

    def drain(self):
        # Reschedule first: an exception below must not stop the loop for good.
        self.root.after(250, self.drain)
        while not self.events.empty():
            name, *args = self.events.get_nowait()
            if name == "data":
                if isinstance(args[0], usage.Usage):
                    self.data, self.error = args[0], None
                else:
                    self.error = args[0]
                self.render()
                self.update_tray()
            elif name == "sessions":
                self.sessions = args[0]
                self.alert(self.alerts.update(self.sessions, self.now()))
                self.render()
                self.update_tray()
            elif name == "toggle":
                self.set_visible(not self.settings["visible"])
            elif name == "topmost":
                self.topmost.set(not self.topmost.get())
                self.apply_topmost()
            elif name == "notify":
                self.settings[args[0]] = not self.settings[args[0]]
                self.save_settings()
                self.icon.update_menu()
            elif name == "startup":
                set_starts_with_windows(not starts_with_windows())
                self.icon.update_menu()
            elif name == "quit":
                self.quit()
                return

    def tick(self):
        self.root.after(TICK_MS, self.tick)
        self.render()

    def alert(self, changes):
        wanted = {"attention": self.settings["notify_attention"], "finished": self.settings["notify_finished"]}
        note = sessions.notification([c for c in changes if wanted[c[0]]])
        if note:
            try:
                self.icon.notify(note[1], note[0])
            except Exception:  # the tray icon may not be up yet
                log.exception("notification failed")

    def update_tray(self):
        session = self.data.session if self.data else None
        if session:
            text, severity = str(round(session.percent)), session.severity
        else:
            text, severity = "?", "stale"
        if self.error:
            severity = "stale"
        attention = [s for s in self.sessions if s.state in sessions.ATTENTION]

        lines = []
        if attention:
            n = len(attention)
            lines.append(f"{n} session{'s' if n > 1 else ''} need{'' if n > 1 else 's'} you")
        for limit in (self.data.limits[:2] if self.data else []):
            reset = limit.resets_at
            when = ""
            if reset:
                left = (reset - self.now()).total_seconds()
                when = f", resets {reset.astimezone():%a} " if left >= 86400 else ", resets "
                when += usage.format_clock(reset)
            lines.append(f"{limit.label.split(' (')[0]} {round(limit.percent)}%{when}")
        if self.error:
            lines.append(ERROR_SHORT.get(self.error.kind, "Error"))
        title = "\n".join(lines or ["Claude usage: loading"])[:127]

        # Sessions refresh every 2 s; only touch the icon when something it shows changed.
        state = (text, severity, bool(attention), title)
        if state == self.tray_state:
            return
        if state[:3] != (self.tray_state or ())[:3]:
            self.icon.icon = tray_image(text, severity, bool(attention))
        # No update_menu() here: data never changes the menu, and rebuilding it from this thread
        # can destroy the menu while it is open on the tray thread.
        self.icon.title = title
        self.tray_state = state

    def render(self):
        # Rebuilding every Tk widget on each 30 s tick made the panel flicker, so widgets are only
        # rebuilt when the layout changes; text, colours and bars are updated in place.
        layout = self.layout_key()
        if layout != self.layout:
            self.layout = layout
            self.build()
        self.fill()
        self.fit()

    def layout_key(self):
        """What decides which widgets exist: the limit rows, the message line, the footer, and
        how many session rows show. Session rows are slots refilled in place, so a session
        changing state or order never rebuilds them."""
        limits = tuple(l.label for l in self.data.limits) if self.data else ()
        message = not self.data or bool(self.error and self.error.kind == "login")
        slots = min(len(self.sessions), MAX_ROWS)
        return limits, message, bool(self.data and self.data.breakdown), slots, len(self.sessions) > MAX_ROWS

    def label(self, parent, size, fg, bold=False, **options):
        return tk.Label(parent, font=(FONT, size, "bold") if bold else (FONT, size), fg=fg, bg=SURFACE, **options)

    def build(self):
        self.tip.hide()
        for child in self.body.winfo_children():
            child.destroy()
        limits, message, footer, slots, more = self.layout
        width = self.px(PANEL_WIDTH)
        tk.Frame(self.body, bg=SURFACE, width=width, height=0).pack()  # fixed width in every state

        header = tk.Frame(self.body, bg=SURFACE)
        header.pack(fill="x", pady=(0, self.px(6)))
        self.label(header, 9, INK_2, bold=True, text="Claude usage").pack(side="left")
        close = self.label(header, 9, MUTED, text="✕", cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.set_visible(False))
        self.note = self.label(header, 8, MUTED)
        self.note.pack(side="right", padx=(0, self.px(8)))
        self.note_icon = self.label(header, 8, FILL["warning"])
        self.note_icon.pack(side="right")

        self.message = None
        if message:
            self.message = self.label(self.body, 9, MUTED, justify="left", wraplength=width, anchor="w")
            self.message.pack(fill="x", pady=(0, self.px(6)))

        self.rows = []
        for name in limits:
            block = tk.Frame(self.body, bg=SURFACE)
            block.pack(fill="x", pady=(self.px(4), self.px(6)))
            top = tk.Frame(block, bg=SURFACE)
            top.pack(fill="x")
            self.label(top, 10, INK, text=name).pack(side="left")
            row = {"percent": self.label(top, 10, INK, bold=True), "bar_key": None}
            row["percent"].pack(side="right")
            row["bar"] = tk.Label(block, bg=SURFACE, borderwidth=0)
            row["bar"].pack(fill="x", pady=(self.px(3), self.px(3)))
            bottom = tk.Frame(block, bg=SURFACE)
            bottom.pack(fill="x")
            row["reset"] = self.label(bottom, 8, MUTED)
            row["reset"].pack(side="left")
            row["status"] = self.label(bottom, 8, INK_2)
            row["status"].pack(side="right")
            row["status_icon"] = self.label(bottom, 8, MUTED)
            row["status_icon"].pack(side="right")
            self.rows.append(row)

        self.footer = None
        if footer:
            self.footer = self.label(self.body, 8, MUTED, wraplength=width, justify="left", anchor="w")
            self.footer.pack(fill="x", pady=(self.px(2), 0))

        self.build_sessions(slots, more)

    def build_sessions(self, slots, more):
        tk.Frame(self.body, bg=BORDER, height=1).pack(fill="x", pady=(self.px(10), self.px(8)))
        header = tk.Frame(self.body, bg=SURFACE)
        header.pack(fill="x")
        self.label(header, 9, INK_2, bold=True, text="Sessions").pack(side="left")
        self.sessions_note = self.label(header, 8, MUTED)
        self.sessions_note.pack(side="right")

        self.slot_of = {}  # Tk widget path -> session row slot, for clicks, menus and hover
        self.session_rows = []
        self.empty = None
        if not slots:
            self.empty = self.label(self.body, 8, MUTED, anchor="w", text="No Claude Code sessions running")
            self.empty.pack(fill="x", pady=(self.px(4), 0))
        # Labels sized in characters are as wide as that many "0"s, plus Tk's padding.
        dot_w = self.fonts[10].measure("00") + self.px(4)
        right_w = self.px(MINI_BAR_WIDTH) + self.px(6) + self.fonts[8].measure("0000") + self.px(4)
        for slot in range(slots):
            frame = tk.Frame(self.body, bg=SURFACE, cursor="hand2")
            frame.pack(fill="x", pady=(self.px(5), 0))
            top = tk.Frame(frame, bg=SURFACE)
            top.pack(fill="x")
            row = {"bar_key": None, "dot": self.label(top, 10, MUTED, width=2, anchor="w"),
                   "percent": self.label(top, 8, INK_2, width=4, anchor="e"),
                   "bar": tk.Label(top, bg=SURFACE, borderwidth=0)}
            row["dot"].pack(side="left")
            row["percent"].pack(side="right")
            row["bar"].pack(side="right", padx=(self.px(6), 0))
            # width=1 keeps a long name from widening the panel; fit_text() shortens it to fit.
            row["name"] = self.label(top, 9, INK, anchor="w", width=1)
            row["name"].pack(side="left", fill="x", expand=True)
            bottom = tk.Frame(frame, bg=SURFACE)
            bottom.pack(fill="x", padx=(dot_w, 0))
            row["total"] = self.label(bottom, 8, MUTED)
            row["total"].pack(side="right")
            row["state"] = self.label(bottom, 8, MUTED, anchor="w", width=1)
            row["state"].pack(side="left", fill="x", expand=True)
            row["name_width"] = self.px(PANEL_WIDTH) - dot_w - right_w - self.px(8)
            for w in (frame, top, bottom, *(v for v in row.values() if isinstance(v, tk.Widget))):
                self.slot_of[str(w)] = slot
                w.bind("<Enter>", lambda e, s=slot: self.tip.schedule(s), add="+")
                w.bind("<Leave>", lambda e: self.tip.leave(), add="+")
            self.session_rows.append(row)

        self.more = None
        if more:
            self.more = self.label(self.body, 8, MUTED, anchor="w")
            self.more.pack(fill="x", pady=(self.px(4), 0))

    def fill(self):
        now = self.now()
        if self.error:
            update(self.note_icon, text="⚠")
            update(self.note, text=ERROR_SHORT.get(self.error.kind, "Error"), fg=INK_2)
        else:
            update(self.note_icon, text="")
            age = usage.format_age(self.data.fetched_at, now) if self.data else None
            update(self.note, text=f"updated {age}" if age else "", fg=MUTED)
        if self.message:
            update(self.message, text=str(self.error) if self.error else "Loading…", fg=INK_2 if self.error else MUTED)

        for limit, row in zip(self.data.limits if self.data else [], self.rows):
            severity = "stale" if self.error else limit.severity
            update(row["percent"], text=f"{round(limit.percent)}%")
            bar_key = (limit.percent, severity)
            if bar_key != row["bar_key"]:
                row["image"] = ImageTk.PhotoImage(bar_image(self.px(PANEL_WIDTH), self.px(BAR_HEIGHT), *bar_key))
                row["bar"].configure(image=row["image"])
                row["bar_key"] = bar_key
            update(row["reset"], text=usage.format_reset(limit.resets_at, now))
            status = None if self.error else status_text(limit)
            update(row["status"], text=status or "")
            update(row["status_icon"], text="⚠" if status else "", fg=FILL[limit.severity])

        if self.footer:
            parts = ", ".join(f"{name} {round(pct)}%" for name, pct in self.data.breakdown)
            update(self.footer, text=f"This week: {parts}")

        self.fill_sessions(now)

    def visible_sessions(self):
        self.offset = max(0, min(self.offset, len(self.sessions) - MAX_ROWS))
        return self.sessions[self.offset:self.offset + MAX_ROWS]

    def fill_sessions(self, now):
        attention = sum(s.state in sessions.ATTENTION for s in self.sessions)
        if self.flash and time.monotonic() < self.flash[1]:
            update(self.sessions_note, text=self.flash[0], fg=INK_2)
        elif attention:
            update(self.sessions_note, text=f"{attention} need{'s' if attention == 1 else ''} you",
                   fg=FILL["critical"])
        else:
            n = len(self.sessions)
            update(self.sessions_note, text=f"{n} session{'s' if n != 1 else ''}" if n else "", fg=MUTED)

        for s, row in zip(self.visible_sessions(), self.session_rows):
            colour = STATE_COLOURS[s.state]
            update(row["dot"], text=STATE_DOTS.get(s.state, "●"), fg=colour)
            update(row["name"], text=fit_text(self.fonts[9], s.name, row["name_width"]))
            update(row["state"], text=state_text(s, now), fg=colour)
            update(row["total"], text=f"{sessions.format_tokens(s.total)} tokens" if s.total else "")
            percent = None if s.compacted else s.context_percent
            update(row["percent"], text="" if percent is None else f"{round(percent)}%")
            bar_key = (round(percent or 0), context_severity(percent))
            if bar_key != row["bar_key"]:
                row["image"] = ImageTk.PhotoImage(bar_image(self.px(MINI_BAR_WIDTH), self.px(MINI_BAR_HEIGHT), *bar_key))
                row["bar"].configure(image=row["image"])
                row["bar_key"] = bar_key

        if self.more:
            above, below = self.offset, len(self.sessions) - self.offset - len(self.session_rows)
            parts = [f"{above} above" if above else "", f"{below} below" if below else ""]
            update(self.more, text=f"{', '.join(p for p in parts if p)}: scroll to see them")

    def session_at(self, slot):
        visible = self.visible_sessions()
        return visible[slot] if slot is not None and slot < len(visible) else None

    def fit(self):
        """Keep the whole panel inside one monitor's work area as its height changes.

        First run (no saved position) puts it in the bottom-right corner of the primary monitor.
        """
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        if self.pos is None:
            left, top, right, bottom = work_area(0, 0)
            x, y = right - w - self.px(12), bottom - h - self.px(12)
        else:
            x, y = self.pos
            left, top, right, bottom = work_area(x + w // 2, y + h // 2)
            x = min(max(x, left), right - w)
            y = min(max(y, top), bottom - h)
        self.pos = (x, y)
        if (x, y, w, h) != self.placed:
            self.placed = (x, y, w, h)
            self.root.geometry(f"+{x}+{y}")

    def drag_start(self, event):
        self.tip.hide()
        if not isinstance(event.widget, tk.Widget) or isinstance(event.widget, tk.Menu):
            self.drag = self.press = None
            return
        self.press = (event.x_root, event.y_root, self.slot_of.get(str(event.widget)))
        self.drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag_move(self, event):
        if self.drag:
            self.pos = (event.x_root - self.drag[0], event.y_root - self.drag[1])
            self.root.geometry(f"+{self.pos[0]}+{self.pos[1]}")

    def release(self, event):
        press, self.press = self.press, None
        self.save_settings()
        if press and press[2] is not None and abs(event.x_root - press[0]) <= CLICK_SLOP \
                and abs(event.y_root - press[1]) <= CLICK_SLOP:
            self.go_to(self.session_at(press[2]))

    def go_to(self, s):
        if not s:
            return
        try:
            where = focus.focus_session(s.pid)
        except OSError:
            log.exception("focus failed")
            where = None
        if not where:
            self.flash = ("Couldn't find its window", time.monotonic() + FLASH_SECONDS)
            self.fill_sessions(self.now())

    def popup(self, event):
        self.tip.hide()
        s = self.session_at(self.slot_of.get(str(event.widget)))
        if not s:
            self.context.tk_popup(event.x_root, event.y_root)
            return
        menu = self.row_menu
        menu.delete(0, "end")
        menu.add_command(label="Go to terminal", command=lambda: self.go_to(s))
        menu.add_command(label="Copy resume command", command=lambda: self.copy(focus.resume_command(s.session_id)))
        menu.add_command(label="Open transcript", command=lambda: focus.reveal(s.transcript),
                         state="normal" if s.transcript else "disabled")
        menu.add_command(label="Open folder", command=lambda: focus.open_folder(s.cwd),
                         state="normal" if s.cwd and Path(s.cwd).is_dir() else "disabled")
        menu.tk_popup(event.x_root, event.y_root)

    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.flash = ("Copied", time.monotonic() + FLASH_SECONDS)
        self.fill_sessions(self.now())

    def scroll(self, event):
        if len(self.sessions) > MAX_ROWS:
            self.offset -= 1 if event.delta > 0 else -1
            self.tip.hide()
            self.fill_sessions(self.now())

    def apply_topmost(self):
        self.settings["topmost"] = self.topmost.get()
        self.root.attributes("-topmost", self.settings["topmost"])
        self.save_settings()
        self.icon.update_menu()

    def set_visible(self, visible):
        self.settings["visible"] = visible
        if visible:
            self.root.deiconify()
            self.root.attributes("-topmost", self.settings["topmost"])
        else:
            self.tip.hide()
            self.root.withdraw()
        self.save_settings()
        self.icon.update_menu()

    def save_settings(self):
        if self.pos:
            self.settings["x"], self.settings["y"] = self.pos
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        except OSError:
            log.exception("could not save settings")

    def quit(self):
        self.save_settings()
        self.icon.stop()
        if self.tray_thread:
            # The tray thread removes the icon on its way out; exiting first leaves a ghost icon.
            self.tray_thread.join(timeout=2)
        self.root.destroy()


class Tip:
    """Hover details for a session row. Moving between the widgets of one row fires Leave then
    Enter, so hiding waits a moment and an Enter on the same row cancels it."""

    def __init__(self, app):
        self.app = app
        self.window = None
        self.pending = None
        self.slot = None

    def schedule(self, slot):
        self.cancel()
        if self.window and self.slot == slot:
            return
        self.hide()
        self.slot = slot
        self.pending = self.app.root.after(TIP_DELAY_MS, self.show)

    def leave(self):
        self.cancel()
        self.pending = self.app.root.after(80, self.hide)

    def cancel(self):
        if self.pending:
            self.app.root.after_cancel(self.pending)
            self.pending = None

    def show(self):
        self.pending = None
        s = self.app.session_at(self.slot)
        if not s:
            return
        root = self.app.root
        self.window = tk.Toplevel(root, bg=BORDER)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        tk.Label(self.window, text=session_tip(s, self.app.now()), justify="left", font=(FONT, 8), fg=INK,
                 bg=TIP_SURFACE, padx=self.app.px(8), pady=self.app.px(6),
                 wraplength=self.app.px(420)).pack(padx=1, pady=1)
        self.window.update_idletasks()
        w, h = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        x, y = root.winfo_pointerx() + self.app.px(14), root.winfo_pointery() + self.app.px(14)
        left, top, right, bottom = work_area(x, y)
        x = min(max(x, left), right - w)
        y = y if y + h <= bottom else root.winfo_pointery() - h - self.app.px(8)
        self.window.geometry(f"+{x}+{max(y, top)}")

    def hide(self):
        self.cancel()
        if self.window:
            self.window.destroy()
            self.window = None


def main():
    # One instance only: the Run key and a manual launch would otherwise stack two icons.
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\{APP}")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=DATA_DIR / "widget.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        App().run()
    except Exception:
        log.exception("widget stopped")  # pythonw has no console to show it
        raise
    del mutex


if __name__ == "__main__":
    main()
