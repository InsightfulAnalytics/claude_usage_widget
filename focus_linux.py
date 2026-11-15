"""Bring the terminal a Claude Code session runs in to the front, on Hyprland.

The session's window belongs to the nearest of its ancestors that owns one: the terminal
emulator, or VS Code for a session in one of its terminals (the window comes forward; the tab is
not chosen). A session inside tmux has the tmux server above it and no terminal, so its pane is
selected first and the window is that of a tmux client showing the pane's session. Nothing is
launched, and every command gets its arguments as a list, never through a shell.
"""

import json
import os
import re
import subprocess
from pathlib import Path

MAX_DEPTH = 32  # never walk further up the process tree than this
TIMEOUT = 3  # seconds any one command gets

# What focus_session() managed.
WINDOW = "window"  # the window the session runs in has the focus
DETACHED = "detached"  # the session is in a tmux session no terminal is attached to

PANE_ID = re.compile(r"%\d+\Z")
ADDRESS = re.compile(r"0x[0-9a-fA-F]+\Z")


def run(args):
    """The command's stdout, or None when it can't be run, fails or takes too long."""
    try:
        done = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def parent_of(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes()
        # Field 4, counted after the bracketed name, which may itself hold spaces and brackets.
        return int(stat.rpartition(b")")[2].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def environ_of(pid):
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    pairs = (item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
    return {k.decode(errors="replace"): v.decode(errors="replace") for k, v in pairs}


def ancestors(pid, parent_of=parent_of):
    """`pid`, then its parent, and so on up to (not including) PID 1."""
    seen = set()
    while isinstance(pid, int) and pid > 1 and pid not in seen and len(seen) < MAX_DEPTH:
        seen.add(pid)
        yield pid
        pid = parent_of(pid)


def windows(run=run):
    """{pid: window address} for every Hyprland window; a process with several windows gets the
    one focused most recently."""
    try:
        clients = json.loads(run(["hyprctl", "clients", "-j"]) or "[]")
    except ValueError:
        return {}
    found = {}
    for c in clients if isinstance(clients, list) else []:
        if not isinstance(c, dict):
            continue
        pid, address, order = c.get("pid"), c.get("address"), c.get("focusHistoryID")
        if not isinstance(pid, int) or not isinstance(address, str) or not ADDRESS.match(address):
            continue
        order = order if isinstance(order, int) and order >= 0 else float("inf")
        if pid not in found or order < found[pid][0]:
            found[pid] = (order, address)
    return {pid: address for pid, (_, address) in found.items()}


def window_of(pid, open_windows, parent_of=parent_of):
    return next((open_windows[p] for p in ancestors(pid, parent_of) if p in open_windows), None)


def tmux_pane(env):
    """(socket, pane id) when the environment is that of a process inside tmux, else None."""
    socket, pane = env.get("TMUX", "").split(",")[0], env.get("TMUX_PANE", "")
    return (socket, pane) if socket.startswith("/") and PANE_ID.match(pane) else None


def tmux_client(socket, pane, run=run):
    """Show `pane` in a tmux client and return (client pid, None), or (None, session name) when no
    client is attached to the server at all."""
    tmux = ["tmux", "-S", socket]
    where = (run(tmux + ["display-message", "-p", "-t", pane, "#{session_id}\t#{window_id}\t#{session_name}"]) or "")
    session, _, rest = where.strip("\n").partition("\t")
    window, _, name = rest.partition("\t")
    if not session or not window:
        return None, None
    clients = []
    for line in (run(tmux + ["list-clients", "-F", "#{client_activity}\t#{client_pid}\t#{session_id}\t#{client_name}"])
                 or "").splitlines():
        activity, pid, attached, client = (line.split("\t") + ["", "", ""])[:4]
        if pid.isdigit() and client:
            clients.append((attached == session, int(activity) if activity.isdigit() else 0, int(pid), client))
    if not clients:
        return None, name
    # A client already on the pane's session, else the one used most recently, moved over to it.
    on_session, _, pid, client = max(clients)
    if not on_session:
        run(tmux + ["switch-client", "-c", client, "-t", session])
    run(tmux + ["select-window", "-t", window])
    run(tmux + ["select-pane", "-t", pane])
    return pid, None


def focus_window(address, run=run):
    # Hyprland with a Lua config takes the dispatcher as Lua; older ones take the plain form.
    if (run(["hyprctl", "dispatch", f'hl.dsp.focus({{ window = "address:{address}" }})']) or "").strip() == "ok":
        return True
    return (run(["hyprctl", "dispatch", "focuswindow", f"address:{address}"]) or "").strip() == "ok"


def focus_session(pid, run=run, parent_of=parent_of, environ_of=environ_of):
    """Bring the session's terminal forward. Returns (WINDOW, None), (DETACHED, tmux session name),
    or (None, None) when there is no window to bring forward."""
    start = pid
    pane = tmux_pane(environ_of(pid))
    if pane:
        start, name = tmux_client(*pane, run=run)
        if start is None:
            return (DETACHED, name) if name else (None, None)
    address = window_of(start, windows(run), parent_of)
    if address and focus_window(address, run):
        return WINDOW, None
    return None, None


def copy(text):
    """Put `text` on the Wayland clipboard. wl-copy forks to keep serving it, so it gets no pipe
    to hold open."""
    try:
        subprocess.run(["wl-copy", "--", text], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=TIMEOUT, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def resume_command(session_id):
    return f"claude --resume {session_id}"


def attach_command(session_name):
    return "tmux attach -t " + "'" + session_name.replace("'", "'\\''") + "'"


def open_folder(path):
    if path and os.path.isdir(path):
        subprocess.Popen(["xdg-open", path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    return False
