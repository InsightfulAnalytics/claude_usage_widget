#!/usr/bin/env python3
"""Feeds the Omarchy bar plugin: fetches plan usage, watches the Claude Code sessions, raises
desktop notifications and prints the view.

Service.qml starts this and it lives as long as the shell does.
Protocol: stdout carries one JSON object per line. Most are the view from view.build(), printed
whenever what it shows changes; {"event": "focused"} says a session's terminal came forward.
stdin takes one command per line:
  refresh                    fetch the usage numbers now
  config <json>              {"notifyAttention": bool, "notifyFinished": bool, "stalledMinutes": n}
  focus | copy | folder <pid>   go to that session's terminal, copy its resume command, open its folder
When stdin closes (the shell went away), it exits.
"""

from __future__ import annotations

import html
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

# The shared modules sit in the repo root, one level above this plugin folder (resolve() follows
# the symlink in ~/.config/omarchy/plugins).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import focus_linux  # noqa: E402
import sessions  # noqa: E402
import usage  # noqa: E402
import view  # noqa: E402

STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
LOG_FILE = STATE_HOME / "claude_usage_widget" / "widget.log"
SESSION_SECONDS = 2
FLASH_SECONDS = 4
ACTIONS = ("focus", "copy", "folder")
log = logging.getLogger("claude_usage_widget")


def notify(title, message, run=subprocess.run):
    try:
        # Omarchy's notifications read the body as markup, so a "<" in a session name is escaped.
        run(["notify-send", "--app-name=Claude usage", "--icon=claude-desktop", title,
             html.escape(message, quote=False)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        log.exception("Notification failed")


class Helper:
    def __init__(self, out=sys.stdout, monitor=None, fetch=usage.fetch_usage, notify=notify, focus=focus_linux,
                 now=lambda: datetime.now(timezone.utc), clock=time.monotonic):
        self.out, self.fetch, self.notify, self.focus, self.now, self.clock = out, fetch, notify, focus, now, clock
        self.monitor = monitor or sessions.Monitor()
        self.alerts = sessions.Alerts()
        self.lock = threading.RLock()  # the poll, stdin and scan threads all change what is shown
        self.data = None  # the last good Usage
        self.error = None  # the UsageError of the last fetch, if it failed
        self.sessions = []
        self.flash = None  # (text, monotonic deadline) shown in the Sessions header
        self.settings = {"attention": True, "finished": True}
        self.printed = None
        self.wake = threading.Event()  # set it to fetch now instead of waiting
        self.changed = threading.Event()  # set it to scan and print now
        self.done = threading.Event()

    def write(self, line):
        try:
            self.out.write(line + "\n")
            self.out.flush()
        except (BrokenPipeError, ValueError):  # the shell is gone
            self.done.set()
            self.wake.set()
            self.changed.set()

    def emit(self):
        """Print the view, unless it is the one already printed."""
        with self.lock:
            flash = self.flash[0] if self.flash and self.clock() < self.flash[1] else ""
            line = json.dumps(view.build(self.data, self.error, self.sessions, self.now(), flash),
                              ensure_ascii=False)
            if line != self.printed:
                self.printed = line
                self.write(line)

    def record(self, result):
        """Take a fetch's result: new numbers, or the error that keeps the last good ones stale."""
        with self.lock:
            if isinstance(result, usage.Usage):
                self.data, self.error = result, None
            else:
                self.error = result
        self.emit()

    def poll(self):
        last = None
        while not self.done.is_set():
            try:
                result = self.fetch()
            except usage.UsageError as e:
                result = e
            except Exception as e:  # keep polling whatever happens
                log.exception("Fetch failed")
                result = usage.UsageError("error", f"Unexpected error ({e.__class__.__name__}).")
            problem = str(result) if isinstance(result, usage.UsageError) else None
            if problem != last:  # an outage is logged when it starts and when it ends, not every retry
                if problem:
                    log.warning("Fetch: %s", problem)
                else:
                    log.info("Fetch: OK")
                last = problem
            self.record(result)
            self.wake.wait(usage.next_poll_delay(result))
            self.wake.clear()

    def scan(self):
        found = self.monitor.scan()
        with self.lock:
            self.sessions = found
            changes = self.alerts.update(found, self.now())
            note = sessions.notification([c for c in changes if self.settings[c[0]]])
        if note:
            self.notify(*note)
        self.emit()

    def watch(self):
        """Session scans, until the shell goes away. The first one reads every transcript in full."""
        last_error = None
        while not self.done.is_set():
            self.changed.clear()
            try:
                self.scan()
                last_error = None
            except Exception as e:  # keep watching whatever happens; log each new failure once
                if repr(e) != last_error:
                    log.exception("Session scan failed")
                    last_error = repr(e)
            self.changed.wait(SESSION_SECONDS)

    def listen(self, lines=None):
        for line in sys.stdin if lines is None else lines:
            try:
                self.handle(line)
            except Exception:
                log.exception("Command failed: %r", line[:200])
        self.done.set()  # stdin closed: the shell is gone
        self.wake.set()
        self.changed.set()

    def handle(self, line):
        verb, _, rest = line.strip().partition(" ")
        if verb == "refresh":
            self.wake.set()
        elif verb == "config":
            self.configure(rest)
        elif verb in ACTIONS and rest.isdigit():
            with self.lock:
                session = next((s for s in self.sessions if s.pid == int(rest)), None)
            if session:
                self.act(verb, session)

    def configure(self, text):
        try:
            config = json.loads(text)
        except ValueError:
            return
        if not isinstance(config, dict):
            return
        with self.lock:
            before = (dict(self.settings), self.monitor.stalled_after)
            for key, name in (("notifyAttention", "attention"), ("notifyFinished", "finished")):
                if isinstance(config.get(key), bool):
                    self.settings[name] = config[key]
            minutes = config.get("stalledMinutes")
            if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
                self.monitor.stalled_after = timedelta(minutes=max(1.0, float(minutes)))
            if (self.settings, self.monitor.stalled_after) != before:
                log.info("Settings: notify when a session needs you %s, when a long turn finishes %s, "
                         "stalled after %s", self.settings["attention"], self.settings["finished"],
                         self.monitor.stalled_after)
        self.changed.set()

    def act(self, verb, session):
        if verb == "focus":
            where, tmux_session = self.focus.focus_session(session.pid)
            log.info("Go to PID %s: %s", session.pid, where or "no terminal found")
            if where == focus_linux.WINDOW:
                self.write(json.dumps({"event": "focused"}))
                return
            if where == focus_linux.DETACHED:
                self.copy(self.focus.attach_command(tmux_session), "tmux session detached: attach command copied")
            else:
                self.copy(self.focus.resume_command(session.session_id), "No terminal found: resume command copied")
        elif verb == "copy":
            self.copy(self.focus.resume_command(session.session_id), "Resume command copied")
        elif verb == "folder" and not self.focus.open_folder(session.cwd):
            self.say("Its folder is gone")

    def copy(self, text, note):
        self.say(note if self.focus.copy(text) else "Couldn't reach the clipboard")

    def say(self, text):
        with self.lock:
            self.flash = (text, self.clock() + FLASH_SECONDS)
        self.emit()
        # The scan loop prints again once the note has run out.


def main():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(LOG_FILE, maxBytes=256_000, backupCount=1, encoding="utf-8")])
    log.info("Started")
    helper = Helper()
    helper.emit()  # "Loading..." until the first answer
    threading.Thread(target=helper.listen, daemon=True).start()
    threading.Thread(target=helper.poll, daemon=True).start()
    helper.watch()
    log.info("Stopped")


if __name__ == "__main__":
    main()
