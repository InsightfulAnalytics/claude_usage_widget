import importlib.util
import io
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sessions  # noqa: E402
import usage  # noqa: E402
from test_usage import NOW, SAMPLE  # noqa: E402
from test_view import session  # noqa: E402

HELPER = Path(__file__).parents[1] / "omarchy" / "claude_usage_status.py"
spec = importlib.util.spec_from_file_location("claude_usage_status", HELPER)
helper = importlib.util.module_from_spec(spec)
# No __pycache__ in omarchy/: nothing may be written into the plugin folder the shell loads.
written, sys.dont_write_bytecode = sys.dont_write_bytecode, True
try:
    spec.loader.exec_module(helper)
finally:
    sys.dont_write_bytecode = written


class FakeMonitor:
    def __init__(self):
        self.found = []
        self.stalled_after = sessions.STALLED_AFTER

    def scan(self):
        return self.found


class FakeFocus:
    """Stands in for focus_linux: records what was asked and answers as told."""

    def __init__(self, where=(helper.focus_linux.WINDOW, None), clipboard=True, folder=True):
        self.where, self.clipboard, self.folder = where, clipboard, folder
        self.focused, self.copied, self.opened = [], [], []

    def focus_session(self, pid):
        self.focused.append(pid)
        return self.where

    def copy(self, text):
        self.copied.append(text)
        return self.clipboard

    def open_folder(self, path):
        self.opened.append(path)
        return self.folder

    resume_command = staticmethod(helper.focus_linux.resume_command)
    attach_command = staticmethod(helper.focus_linux.attach_command)


class HelperTest(unittest.TestCase):
    def make(self, **focus):
        self.out, self.sent, self.time = io.StringIO(), [], [100.0]
        self.monitor, self.focus = FakeMonitor(), FakeFocus(**focus)
        return helper.Helper(out=self.out, monitor=self.monitor, notify=lambda *note: self.sent.append(note),
                             focus=self.focus, now=lambda: NOW, clock=lambda: self.time[0])

    def lines(self):
        return [json.loads(line) for line in self.out.getvalue().splitlines()]


class Emit(HelperTest):
    def test_an_unchanged_view_is_printed_once(self):
        h = self.make()
        h.emit()
        h.emit()
        h.scan()
        self.assertEqual([line["state"] for line in self.lines()], ["starting"])

    def test_numbers_then_an_error_that_keeps_them(self):
        h = self.make()
        h.record(usage.parse_usage(SAMPLE, now=NOW))
        h.record(usage.UsageError("network", "Offline or unreachable (URLError)."))
        first, second = self.lines()
        self.assertEqual((first["bar"]["text"], first["note"]), ("46%", "updated just now"))
        self.assertEqual((second["bar"]["text"], second["note"], second["bar"]["severity"]), ("46%", "Offline", "stale"))

    def test_a_closed_pipe_ends_the_helper(self):
        h = self.make()
        self.out.close()
        h.emit()
        self.assertTrue(h.done.is_set())


class Notifications(HelperTest):
    def test_sessions_already_waiting_at_the_start_stay_silent(self):
        h = self.make()
        self.monitor.found = [session(1, sessions.NEEDS_YOU, waiting_for="permission")]
        h.scan()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.lines()[-1]["bar"]["attention"], 1)

    def test_a_session_that_starts_waiting_notifies_once(self):
        h = self.make()
        self.monitor.found = [session(1, sessions.RUNNING)]
        h.scan()
        self.monitor.found = [session(1, sessions.NEEDS_YOU, waiting_for="permission")]
        h.scan()
        h.scan()
        self.assertEqual(self.sent, [("Needs you: site-1", "permission\nsite")])

    def test_notifications_follow_the_settings(self):
        h = self.make()
        h.handle('config {"notifyAttention": false, "notifyFinished": "yes", "stalledMinutes": 12}')
        self.assertEqual(h.settings, {"attention": False, "finished": True})
        self.assertEqual(self.monitor.stalled_after, timedelta(minutes=12))
        self.monitor.found = [session(1, sessions.RUNNING)]
        h.scan()
        self.monitor.found = [session(1, sessions.NEEDS_YOU)]
        h.scan()
        self.assertEqual(self.sent, [])

    def test_settings_that_make_no_sense_change_nothing(self):
        h = self.make()
        for text in ("config", "config {half", "config [1]", 'config {"stalledMinutes": true}',
                     'config {"stalledMinutes": "5"}'):
            h.handle(text)
        self.assertEqual(h.settings, {"attention": True, "finished": True})
        self.assertEqual(self.monitor.stalled_after, sessions.STALLED_AFTER)
        h.handle('config {"stalledMinutes": 0}')
        self.assertEqual(self.monitor.stalled_after, timedelta(minutes=1))

    def test_the_body_is_escaped_for_the_notification_markup(self):
        calls = []
        helper.notify("Needs you: <b>x</b>", "<img src=x> & y", run=lambda args, **kw: calls.append(args))
        self.assertEqual(calls[0][-2:], ["Needs you: <b>x</b>", "&lt;img src=x&gt; &amp; y"])


class Commands(HelperTest):
    def ready(self, **focus):
        h = self.make(**focus)
        self.monitor.found = [session(7, sessions.YOUR_TURN)]
        h.scan()
        return h

    def test_refresh_wakes_the_poll(self):
        h = self.make()
        h.handle("refresh\n")
        self.assertTrue(h.wake.is_set())

    def test_going_to_a_session_reports_it_came_forward(self):
        h = self.ready()
        h.handle("focus 7")
        self.assertEqual(self.focus.focused, [7])
        self.assertEqual(self.lines()[-1], {"event": "focused"})
        self.assertEqual(self.focus.copied, [])

    def test_no_terminal_copies_the_resume_command(self):
        h = self.ready(where=(None, None))
        h.handle("focus 7")
        self.assertEqual(self.focus.copied, ["claude --resume s7"])
        self.assertEqual(self.lines()[-1]["flash"], "No terminal found: resume command copied")

    def test_a_detached_tmux_session_copies_how_to_attach(self):
        h = self.ready(where=(helper.focus_linux.DETACHED, "Work"))
        h.handle("focus 7")
        self.assertEqual(self.focus.copied, ["tmux attach -t 'Work'"])
        self.assertEqual(self.lines()[-1]["flash"], "tmux session detached: attach command copied")

    def test_copy_and_the_note_runs_out(self):
        h = self.ready()
        h.handle("copy 7")
        self.assertEqual(self.focus.copied, ["claude --resume s7"])
        self.assertEqual(self.lines()[-1]["flash"], "Resume command copied")
        self.time[0] += helper.FLASH_SECONDS + 1
        h.scan()
        self.assertEqual(self.lines()[-1]["flash"], "")

    def test_a_clipboard_that_cannot_be_reached_says_so(self):
        h = self.ready(clipboard=False)
        h.handle("copy 7")
        self.assertEqual(self.lines()[-1]["flash"], "Couldn't reach the clipboard")

    def test_folder(self):
        h = self.ready()
        h.handle("folder 7")
        self.assertEqual(self.focus.opened, ["/work/site"])
        self.focus.folder = False
        h.handle("folder 7")
        self.assertEqual(self.lines()[-1]["flash"], "Its folder is gone")

    def test_only_live_sessions_and_known_commands_are_acted_on(self):
        h = self.ready()
        for text in ("focus 8", "focus", "focus 7; rm -rf /", "focus -7", "delete 7", "", "copy seven"):
            h.handle(text)
        self.assertEqual((self.focus.focused, self.focus.copied, self.focus.opened), ([], [], []))

    def test_stdin_closing_ends_the_helper(self):
        h = self.ready()
        h.listen(["copy 7\n", "refresh\n"])
        self.assertEqual(self.focus.copied, ["claude --resume s7"])
        self.assertTrue(h.done.is_set())


if __name__ == "__main__":
    unittest.main()
