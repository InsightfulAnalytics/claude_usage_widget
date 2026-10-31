"""Bring the terminal a Claude Code session runs in to the front.

VS Code terminals: the widget raises the VS Code window that is already open, then asks the
claude-context-status extension (0.3.0 or later) over its named pipe to show the terminal whose
shell has the session as a child. The shell is the session's nearest ancestor whose parent is
Code.exe. Nothing is launched, so no second VS Code window can open: a `vscode://` URI would start
whichever VS Code the protocol is registered to, which can be another install than the one
running. Other sessions run in a console window: attach to the session's console and raise the
window that owns it (Windows Terminal owns the pseudo-console window of each tab it hosts).
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import subprocess
import threading

PIPE_DIR = "\\\\.\\pipe\\"
PIPE_PREFIX = "claude-context-status-focus-"  # one pipe per VS Code window, named for its extension host
PIPE_TIMEOUT = 1.0  # seconds a window gets to answer
CODE_EXES = ("code.exe", "code - insiders.exe")
MAX_DEPTH = 16  # parent PIDs can be stale and reused; never walk further than this

# What focus_session() managed.
TAB = "tab"  # the session's VS Code terminal is showing
NO_EXTENSION = "no-extension"  # VS Code is raised, but no window answered on the pipe
NO_TAB = "no-tab"  # VS Code is raised, but no window has the session's terminal
CONSOLE = "console"  # the console window it runs in is raised

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetConsoleWindow.restype = wintypes.HWND
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def processes():
    """{pid: (parent pid, exe name)} for every running process."""
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return {}
    try:
        entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        out = {}
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        return out
    finally:
        kernel32.CloseHandle(snap)


def is_code(name):
    return (name or "").lower() in CODE_EXES


def vscode_host(pid, procs):
    """(terminal shell pid, Code.exe pid) when the session runs in a VS Code terminal, else None."""
    current, seen = pid, set()
    for _ in range(MAX_DEPTH):
        if current in seen or current not in procs:
            return None
        seen.add(current)
        parent = procs[current][0]
        if parent in procs and is_code(procs[parent][1]):
            return current, parent
        current = parent
    return None


def top_code(pid, procs):
    """The main Code.exe above a helper one (the terminal host): it owns the windows."""
    for _ in range(MAX_DEPTH):
        parent = procs.get(pid, (None, None))[0]
        if parent not in procs or not is_code(procs[parent][1]) or parent == pid:
            return pid
        pid = parent
    return pid


def windows_of(pid):
    """Visible, titled top-level windows of a process."""
    found = []

    def collect(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(collect), 0)
    return found


def window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def focus_pipes():
    try:
        return [name for name in os.listdir(PIPE_DIR) if name.startswith(PIPE_PREFIX)]
    except OSError:
        return []


def ask_pipe(name, shell_pid, timeout=PIPE_TIMEOUT):
    """The extension's reply ({"found": bool, "workspace": str}), or None when the window doesn't
    answer in time. The exchange runs on a thread so a busy window can't hang the panel."""
    result = {}

    def talk():
        try:
            with open(PIPE_DIR + name, "r+b", buffering=0) as pipe:
                pipe.write(json.dumps({"shellPid": shell_pid}).encode() + b"\n")
                reply = b""
                while not reply.endswith(b"\n"):
                    chunk = pipe.read(4096)
                    if not chunk:
                        break
                    reply += chunk
            result["reply"] = json.loads(reply)
        except (OSError, ValueError):
            pass

    thread = threading.Thread(target=talk, daemon=True)
    thread.start()
    thread.join(timeout)
    reply = result.get("reply")
    return reply if isinstance(reply, dict) else None


def pick_window(windows, workspace):
    """The VS Code window showing `workspace` (its name is in the title), else the first one."""
    if workspace:
        for hwnd in windows:
            if f" - {workspace} - " in f" - {window_title(hwnd)} - ":
                return hwnd
    return windows[0] if windows else None


def console_window(pid):
    """The window showing `pid`'s console, or None. Needs a process with no console of its own
    (pythonw), since attaching to another console means leaving ours."""
    if kernel32.GetConsoleWindow() or not kernel32.AttachConsole(pid):
        return None
    try:
        hwnd = kernel32.GetConsoleWindow()
    finally:
        kernel32.FreeConsole()
    if not hwnd:
        return None
    owner = user32.GetAncestor(hwnd, 3)  # GA_ROOTOWNER: the Windows Terminal window for one of its tabs
    for candidate in (owner, hwnd):
        if candidate and user32.IsWindowVisible(candidate):
            return candidate
    return None


def raise_window(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    return bool(user32.SetForegroundWindow(hwnd))


def focus_session(pid):
    """Bring the session's terminal forward. Returns TAB, NO_EXTENSION, NO_TAB, CONSOLE, or None
    when there is no terminal to bring forward.

    The widget is the foreground process while it handles the click, so Windows lets it raise
    another window; the extension only switches the terminal inside its own window.
    """
    procs = processes()
    host = vscode_host(pid, procs)
    if host:
        shell, code = host
        replies = []
        for name in focus_pipes():
            reply = ask_pipe(name, shell)
            if reply is not None:
                replies.append(reply)
                if reply.get("found"):
                    break
        found = next((r for r in replies if r.get("found")), None)
        hwnd = pick_window(windows_of(top_code(code, procs)), found and found.get("workspace"))
        if hwnd:
            raise_window(hwnd)
        return TAB if found else NO_TAB if replies else NO_EXTENSION
    hwnd = console_window(pid)
    if hwnd and raise_window(hwnd):
        return CONSOLE
    return None


def reveal(path):
    """Select a file in Explorer (transcripts are too big to open in an editor by default)."""
    subprocess.Popen(f'explorer /select,"{path}"')


def open_folder(path):
    os.startfile(path)


def resume_command(session_id):
    return f"claude --resume {session_id}"
