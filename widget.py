"""Claude usage on the Windows desktop: a tray icon showing the session % and a small panel.

Run with pythonw.exe so no console window opens. Left-click the tray icon to show or hide the
panel; drag the panel anywhere; right-click either for the menu.
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
import winreg
from datetime import datetime, timezone
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont, ImageTk

import usage

APP = "claude_usage_widget"
POLL_SECONDS = 180
BACKOFF_SECONDS = 600
TICK_MS = 30_000  # re-render countdowns between fetches
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP
SETTINGS_FILE = DATA_DIR / "settings.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClaudeUsageWidget"

# Dark-mode tokens from the dataviz reference palette. Meter fills carry severity; the track
# is a dim step of the same fill so the state reads across the whole bar.
FONT = "Segoe UI"
SURFACE = "#1a1a19"
BORDER = "#383835"
INK = "#f0efec"
INK_2 = "#c3c2b7"
MUTED = "#898781"
FILL = {"normal": "#3987e5", "warning": "#fab219", "critical": "#d03b3b", "stale": "#898781"}
TRACK_MIX = 0.28
PANEL_WIDTH = 280
BAR_HEIGHT = 8
ERROR_SHORT = {"login": "Login expired", "rate_limited": "Rate limited", "network": "Offline"}

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


def tray_image(text, severity):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=12, fill=FILL[severity])
    ink = "#0b0b0b" if severity == "warning" else "#ffffff"
    draw.text((size / 2, size / 2), text, fill=ink, font=load_font(46 if len(text) <= 2 else 32), anchor="mm")
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


def load_settings():
    settings = {"x": None, "y": None, "visible": True, "topmost": True}
    try:
        settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return settings


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
        self.images = []
        self.drag = None

        self.root = tk.Tk()
        self.root.title("Claude usage")
        self.root.overrideredirect(True)
        self.root.configure(bg=BORDER)
        self.scale = self.root.winfo_fpixels("1i") / 96
        self.body = tk.Frame(self.root, bg=SURFACE, padx=self.px(14), pady=self.px(10))
        self.body.pack(padx=1, pady=1, fill="both", expand=True)

        self.topmost = tk.BooleanVar(value=self.settings["topmost"])
        self.context = tk.Menu(self.root, tearoff=False)
        self.context.add_command(label="Refresh now", command=self.wake.set)
        self.context.add_checkbutton(label="Keep on top", variable=self.topmost, command=self.apply_topmost)
        self.context.add_command(label="Hide panel", command=lambda: self.set_visible(False))
        self.context.add_separator()
        self.context.add_command(label="Quit", command=self.quit)

        self.root.bind_all("<ButtonPress-1>", self.drag_start)
        self.root.bind_all("<B1-Motion>", self.drag_move)
        self.root.bind_all("<ButtonRelease-1>", lambda e: self.save_settings())
        self.root.bind_all("<Button-3>", lambda e: self.context.tk_popup(e.x_root, e.y_root))

        self.icon = pystray.Icon(APP, tray_image("?", "stale"), "Claude usage: loading", menu=pystray.Menu(
            pystray.MenuItem("Show panel", lambda: self.post("toggle"), default=True,
                             checked=lambda item: self.settings["visible"]),
            pystray.MenuItem("Refresh now", lambda: self.wake.set()),
            pystray.MenuItem("Keep panel on top", lambda: self.post("topmost"),
                             checked=lambda item: self.settings["topmost"]),
            pystray.MenuItem("Start with Windows", lambda: self.post("startup"),
                             checked=lambda item: starts_with_windows()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.post("quit")),
        ))

        x, y = self.settings["x"], self.settings["y"]
        self.pos = (x, y) if x is not None and y is not None else None
        self.apply_topmost()
        self.render()
        if not self.settings["visible"]:
            self.root.withdraw()

    def px(self, value):
        return round(value * self.scale)

    def post(self, *event):
        # pystray callbacks run on the tray thread; Tk is only touched from the main thread.
        self.events.put(event)

    def run(self):
        threading.Thread(target=self.icon.run, daemon=True).start()
        threading.Thread(target=self.poll, daemon=True).start()
        self.root.after(250, self.drain)
        self.root.after(TICK_MS, self.tick)
        self.root.mainloop()

    def poll(self):
        while True:
            delay = POLL_SECONDS
            try:
                result = usage.fetch_usage()
            except usage.UsageError as e:
                log.warning("fetch: %s", e)
                result = e
                if e.kind == "rate_limited":
                    delay = BACKOFF_SECONDS
            except Exception as e:  # keep polling whatever happens
                log.exception("fetch failed")
                result = usage.UsageError("network", f"Unexpected error ({e.__class__.__name__}).")
            self.post("data", result)
            self.wake.wait(delay)
            self.wake.clear()

    def drain(self):
        while not self.events.empty():
            name, *args = self.events.get_nowait()
            if name == "data":
                if isinstance(args[0], usage.Usage):
                    self.data, self.error = args[0], None
                else:
                    self.error = args[0]
                self.render()
                self.update_tray()
            elif name == "toggle":
                self.set_visible(not self.settings["visible"])
            elif name == "topmost":
                self.topmost.set(not self.topmost.get())
                self.apply_topmost()
            elif name == "startup":
                set_starts_with_windows(not starts_with_windows())
                self.icon.update_menu()
            elif name == "quit":
                self.quit()
                return
        self.root.after(250, self.drain)

    def tick(self):
        self.render()
        self.root.after(TICK_MS, self.tick)

    def update_tray(self):
        session = self.data.session if self.data else None
        if session:
            text, severity = str(round(session.percent)), session.severity
        else:
            text, severity = "?", "stale"
        if self.error:
            severity = "stale"
        self.icon.icon = tray_image(text, severity)

        lines = []
        for limit in (self.data.limits[:2] if self.data else []):
            reset = limit.resets_at
            when = ""
            if reset:
                left = (reset - datetime.now(timezone.utc)).total_seconds()
                when = f", resets {reset.astimezone():%a} " if left >= 86400 else ", resets "
                when += usage.format_clock(reset)
            lines.append(f"{limit.label.split(' (')[0]} {round(limit.percent)}%{when}")
        if self.error:
            lines.append(ERROR_SHORT.get(self.error.kind, "Error"))
        self.icon.title = "\n".join(lines or ["Claude usage: loading"])[:127]
        self.icon.update_menu()

    def render(self):
        self.build()
        self.fit()

    def build(self):
        for child in self.body.winfo_children():
            child.destroy()
        self.images = []
        width = self.px(PANEL_WIDTH)
        tk.Frame(self.body, bg=SURFACE, width=width, height=0).pack()  # fixed width in every state

        header = tk.Frame(self.body, bg=SURFACE)
        header.pack(fill="x", pady=(0, self.px(6)))
        tk.Label(header, text="Claude usage", font=(FONT, 9, "bold"), fg=INK_2, bg=SURFACE).pack(side="left")
        close = tk.Label(header, text="✕", font=(FONT, 9), fg=MUTED, bg=SURFACE, cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.set_visible(False))
        if self.error:
            note = tk.Frame(header, bg=SURFACE)
            note.pack(side="right", padx=(0, self.px(8)))
            tk.Label(note, text="⚠", font=(FONT, 8), fg=FILL["warning"], bg=SURFACE).pack(side="left")
            tk.Label(note, text=ERROR_SHORT.get(self.error.kind, "Error"), font=(FONT, 8),
                     fg=INK_2, bg=SURFACE).pack(side="left")
        elif self.data:
            tk.Label(header, text=f"updated {usage.format_age(self.data.fetched_at)}", font=(FONT, 8),
                     fg=MUTED, bg=SURFACE).pack(side="right", padx=(0, self.px(8)))

        if self.error and (self.error.kind == "login" or not self.data):
            tk.Label(self.body, text=str(self.error), font=(FONT, 9), fg=INK_2, bg=SURFACE, justify="left",
                     wraplength=width, anchor="w").pack(fill="x", pady=(0, self.px(6)))
        if not self.data:
            if not self.error:
                tk.Label(self.body, text="Loading…", font=(FONT, 9), fg=MUTED, bg=SURFACE,
                         anchor="w").pack(fill="x")
            return

        for limit in self.data.limits:
            severity = "stale" if self.error else limit.severity
            block = tk.Frame(self.body, bg=SURFACE)
            block.pack(fill="x", pady=(self.px(4), self.px(6)))
            top = tk.Frame(block, bg=SURFACE)
            top.pack(fill="x")
            tk.Label(top, text=limit.label, font=(FONT, 10), fg=INK, bg=SURFACE).pack(side="left")
            tk.Label(top, text=f"{round(limit.percent)}%", font=(FONT, 10, "bold"), fg=INK,
                     bg=SURFACE).pack(side="right")

            image = ImageTk.PhotoImage(bar_image(width, self.px(BAR_HEIGHT), limit.percent, severity))
            self.images.append(image)
            tk.Label(block, image=image, bg=SURFACE, borderwidth=0).pack(fill="x", pady=(self.px(3), self.px(3)))

            bottom = tk.Frame(block, bg=SURFACE)
            bottom.pack(fill="x")
            tk.Label(bottom, text=usage.format_reset(limit.resets_at), font=(FONT, 8), fg=MUTED,
                     bg=SURFACE).pack(side="left")
            label = status_text(limit)
            if label and not self.error:
                tk.Label(bottom, text=label, font=(FONT, 8), fg=INK_2, bg=SURFACE).pack(side="right")
                tk.Label(bottom, text="⚠", font=(FONT, 8), fg=FILL[limit.severity],
                         bg=SURFACE).pack(side="right")

        if self.data.breakdown:
            parts = ", ".join(f"{name} {round(pct)}%" for name, pct in self.data.breakdown)
            tk.Label(self.body, text=f"This week: {parts}", font=(FONT, 8), fg=MUTED, bg=SURFACE,
                     wraplength=width, justify="left", anchor="w").pack(fill="x", pady=(self.px(2), 0))

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
        self.root.geometry(f"+{x}+{y}")

    def drag_start(self, event):
        if not isinstance(event.widget, tk.Widget) or isinstance(event.widget, tk.Menu):
            self.drag = None
            return
        self.drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag_move(self, event):
        if self.drag:
            self.pos = (event.x_root - self.drag[0], event.y_root - self.drag[1])
            self.root.geometry(f"+{self.pos[0]}+{self.pos[1]}")

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
        self.root.destroy()


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
    App().run()
    del mutex


if __name__ == "__main__":
    main()
