import copy
import json
import sys
import tempfile
import tkinter as tk
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from pystray._util import win32

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sessions  # noqa: E402
import usage  # noqa: E402
import widget  # noqa: E402
from test_usage import NOW, SAMPLE  # noqa: E402


APPS = []


def descendants(w):
    out = []
    for child in w.winfo_children():
        out += [str(child)] + descendants(child)
    return out


class WidgetTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = Path(tmp.name) / "settings.json"
        settings.write_text(json.dumps({"visible": False}), encoding="utf-8")
        for name, value in (("DATA_DIR", Path(tmp.name)), ("SETTINGS_FILE", settings)):
            patcher = mock.patch.object(widget, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_app(self):
        app = widget.App()
        # pystray names each icon's window class after id(icon) and never unregisters it, so a
        # collected app's id reused by a later one fails with "Class already exists".
        APPS.append(app)

        def destroy():
            try:
                app.root.destroy()
            except tk.TclError:  # already destroyed by quit()
                pass
        self.addCleanup(destroy)
        return app


class Render(WidgetTest):
    """The panel must update in place: rebuilding its widgets on every tick made it flicker."""

    def setUp(self):
        super().setUp()
        self.app = self.make_app()
        self.app.now = lambda: NOW + timedelta(minutes=5)  # fixed clock: no minute-boundary flakes
        self.app.data = usage.parse_usage(copy.deepcopy(SAMPLE), now=NOW)
        self.app.render()

    def test_new_numbers_keep_the_same_widgets(self):
        before = descendants(self.app.body)
        self.app.data = usage.parse_usage({**SAMPLE, "limits": [dict(SAMPLE["limits"][0], percent=47)]
                                           + SAMPLE["limits"][1:]}, now=NOW)
        self.app.render()
        self.assertEqual(descendants(self.app.body), before)
        self.assertEqual(self.app.rows[0]["percent"].cget("text"), "47%")

    def test_tick_without_changes_redraws_nothing(self):
        with mock.patch.object(widget.tk.Label, "configure") as configure:
            self.app.render()
        configure.assert_not_called()

    def test_offline_greys_bars_in_place(self):
        before = descendants(self.app.body)
        self.app.error = usage.UsageError("network", "Offline")
        self.app.render()
        self.assertEqual(descendants(self.app.body), before)
        self.assertEqual(self.app.note.cget("text"), "Offline")
        self.assertEqual(self.app.rows[0]["bar_key"][1], "stale")

    def test_login_error_adds_the_message_line(self):
        before = descendants(self.app.body)
        self.app.error = usage.UsageError("login", "Login expired. Run Claude Code once to refresh it.")
        self.app.render()
        self.assertNotEqual(descendants(self.app.body), before)
        self.assertIn("Login expired", self.app.message.cget("text"))


def session(pid, state, minutes_ago=3, name=None, context=100_000, waiting_for=None):
    since = NOW - timedelta(minutes=minutes_ago)
    return sessions.Session(
        pid=pid, session_id=f"s{pid}", name=name or f"site-{pid}", title="A title", cwd="B:\\work\\site",
        kind="interactive", entrypoint="cli", version=None, state=state, waiting_for=waiting_for, since=since,
        status_since=since, started_at=since, model="claude-opus-5-5", context=context, window=1_000_000,
        window_source="default", compacted=False, main=sessions.Tokens(10, 20, 3_000_000, 40))


class Sessions(WidgetTest):
    """Session rows are slots filled in place: a session changing state or order must not
    rebuild them, or the panel flickers every 2 seconds."""

    def setUp(self):
        super().setUp()
        self.app = self.make_app()
        self.app.now = lambda: NOW
        self.app.sessions = [session(1, sessions.RUNNING), session(2, sessions.YOUR_TURN)]
        self.app.render()

    def test_rows_show_state_context_and_total(self):
        row = self.app.session_rows[0]
        self.assertEqual(row["name"].cget("text"), "site-1")
        self.assertEqual(row["state"].cget("text"), "Running 3m")
        self.assertEqual(row["percent"].cget("text"), "10%")
        self.assertEqual(row["total"].cget("text"), "3.0M tokens")
        self.assertEqual(self.app.sessions_note.cget("text"), "2 sessions")

    def test_state_and_order_changes_keep_the_same_widgets(self):
        before = descendants(self.app.body)
        self.app.sessions = [session(2, sessions.NEEDS_YOU, waiting_for="permission"), session(1, sessions.RUNNING)]
        self.app.render()
        self.assertEqual(descendants(self.app.body), before)
        self.assertEqual(self.app.session_rows[0]["state"].cget("text"), "Needs you 3m · permission")
        self.assertEqual(self.app.sessions_note.cget("text"), "1 needs you")

    def test_unchanged_sessions_redraw_nothing(self):
        with mock.patch.object(widget.tk.Label, "configure") as configure:
            self.app.render()
        configure.assert_not_called()

    def test_a_new_session_adds_a_row(self):
        self.app.sessions = self.app.sessions + [session(3, sessions.RUNNING)]
        self.app.render()
        self.assertEqual(len(self.app.session_rows), 3)

    def test_long_names_are_cut_to_fit(self):
        self.app.sessions = [session(1, sessions.RUNNING, name="dataset-challenge-42-ai-agent-customer-support-52")]
        self.app.render()
        text = self.app.session_rows[0]["name"].cget("text")
        self.assertTrue(text.endswith("…"))
        self.assertLessEqual(self.app.fonts[9].measure(text), self.app.session_rows[0]["name_width"])

    def test_more_rows_than_fit_scroll(self):
        self.app.sessions = [session(i, sessions.RUNNING) for i in range(widget.MAX_ROWS + 3)]
        self.app.render()
        self.assertEqual(len(self.app.session_rows), widget.MAX_ROWS)
        self.assertEqual(self.app.more.cget("text"), "3 below: scroll to see them")
        for _ in range(5):
            self.app.scroll(mock.Mock(delta=-120))
        self.assertEqual(self.app.offset, 3)  # stops at the end
        self.assertEqual(self.app.session_rows[0]["name"].cget("text"), "site-3")
        self.assertEqual(self.app.more.cget("text"), "3 above: scroll to see them")

    def test_tray_dot_while_a_session_needs_you(self):
        with mock.patch.object(widget, "tray_image", wraps=widget.tray_image) as tray_image:
            self.app.update_tray()
            self.app.sessions = [session(1, sessions.NEEDS_YOU)]
            self.app.update_tray()
            self.app.update_tray()  # nothing changed: the icon is left alone
        self.assertEqual([c.args[2] for c in tray_image.call_args_list], [False, True])
        self.assertTrue(self.app.icon.title.startswith("1 session needs you"))

    def test_notifications_follow_the_tray_ticks(self):
        changes = [("attention", session(1, sessions.NEEDS_YOU), "permission"),
                   ("finished", session(2, sessions.YOUR_TURN), "Finished after 3m")]
        with mock.patch.object(self.app.icon, "notify") as notify:
            self.app.settings["notify_finished"] = False
            self.app.alert(changes)
            self.app.settings["notify_attention"] = False
            self.app.alert(changes)
        notify.assert_called_once_with("permission\nsite", "Needs you: site-1")

    def click(self, widget_, dx=0):
        x, y = 50, 50
        self.app.drag_start(mock.Mock(widget=widget_, x_root=x, y_root=y))
        self.app.release(mock.Mock(widget=widget_, x_root=x + dx, y_root=y))

    def test_click_goes_to_the_session(self):
        with mock.patch.object(widget.focus, "focus_session", return_value=widget.focus.TAB) as focus_session:
            self.click(self.app.session_rows[1]["state"])
        focus_session.assert_called_once_with(2)
        self.assertEqual(self.app.sessions_note.cget("text"), "2 sessions")

    def test_dragging_a_row_moves_the_panel_only(self):
        with mock.patch.object(widget.focus, "focus_session") as focus_session:
            self.click(self.app.session_rows[0]["name"], dx=30)
        focus_session.assert_not_called()

    def test_no_terminal_copies_the_resume_command(self):
        with mock.patch.object(widget.focus, "focus_session", return_value=None), \
                mock.patch.object(self.app.root, "clipboard_clear"), \
                mock.patch.object(self.app.root, "clipboard_append") as clipboard_append:  # keep the real clipboard
            self.click(self.app.session_rows[0]["name"])
        clipboard_append.assert_called_once_with("claude --resume s1")
        self.assertEqual(self.app.sessions_note.cget("text"), "No terminal found: resume command copied")

    def test_vs_code_without_the_extension_says_to_reload(self):
        with mock.patch.object(widget.focus, "focus_session", return_value=widget.focus.NO_EXTENSION):
            self.click(self.app.session_rows[0]["name"])
        self.assertEqual(self.app.sessions_note.cget("text"), "Reload VS Code to jump to the tab")

    def test_tip_describes_the_session(self):
        tip = widget.session_tip(session(1, sessions.NEEDS_YOU, waiting_for="permission"), NOW)
        self.assertIn("Needs you: permission", tip)
        self.assertIn("Context: 100,000 of 1,000,000 tokens (10.0%)", tip)
        self.assertIn("Session total: 3,000,070 tokens", tip)


class Tray(WidgetTest):
    def setUp(self):
        super().setUp()
        self.app = self.make_app()
        self.app.now = lambda: NOW

    def test_no_numbers_yet_shows_dots(self):
        self.app.update_tray()
        self.assertEqual(self.app.tray_state[:2], (None, "stale"))
        self.assertNotEqual(widget.tray_image(None, "stale").tobytes(), widget.tray_image("0", "stale").tobytes())

    def test_plan_without_a_session_limit_shows_the_weekly_percent(self):
        self.app.data = usage.parse_usage({**SAMPLE, "five_hour": None, "limits": SAMPLE["limits"][1:]}, now=NOW)
        self.app.update_tray()
        self.assertEqual(self.app.tray_state[:2], ("91", "critical"))
        self.assertTrue(self.app.icon.title.startswith("Weekly 91%, resets "))
        self.assertIn("\nWeekly (Fable) 0%, resets", self.app.icon.title)

    def test_icon_is_drawn_again_once_shown(self):
        # pystray can drop an image set while it is still showing the icon.
        self.app.data = usage.parse_usage(copy.deepcopy(SAMPLE), now=NOW)
        self.app.update_tray()
        with mock.patch.object(widget, "tray_image", wraps=widget.tray_image) as tray_image, \
                mock.patch.object(self.app.root, "after"):
            self.app.post("tray_ready")
            self.app.drain()
        tray_image.assert_called_once_with("46", "normal", False)

    def test_icon_goes_out_with_its_own_uid(self):
        """Windows keeps the icon's taskbar setting against this uID (see TrayIcon)."""
        sent = []
        with mock.patch.object(win32, "Shell_NotifyIcon", lambda code, data: sent.append(data)):
            self.app.icon._message(win32.NIM_MODIFY, win32.NIF_TIP, szTip="hello")
        self.assertEqual(sent[0].uID, widget.TrayIcon.UID)
        self.assertEqual(sent[0].szTip, "hello")


class Robustness(WidgetTest):
    def setUp(self):
        super().setUp()
        self.app = self.make_app()

    def test_failing_event_does_not_stop_the_event_loop(self):
        # A Tk callback that raises is never rescheduled unless it rescheduled itself first.
        with mock.patch.object(self.app.root, "after") as after, \
                mock.patch.object(self.app, "set_visible", side_effect=OSError("registry")):
            self.app.post("toggle")
            with self.assertRaises(OSError):
                self.app.drain()
        after.assert_called_once_with(250, self.app.drain)

    def test_tk_callback_errors_go_to_the_log(self):
        with self.assertLogs(widget.log, "ERROR") as logs:
            self.app.root.report_callback_exception(RuntimeError, RuntimeError("boom"), None)
        self.assertIn("Tk callback failed", logs.output[0])

    def test_data_refresh_leaves_the_tray_menu_alone(self):
        self.app.data = usage.parse_usage(copy.deepcopy(SAMPLE), now=NOW)
        with mock.patch.object(self.app.icon, "update_menu") as update_menu:
            self.app.update_tray()
        update_menu.assert_not_called()

    def test_quit_waits_for_the_tray_thread(self):
        self.app.tray_thread = mock.Mock()
        with mock.patch.object(self.app.icon, "stop"):
            self.app.quit()
        self.app.tray_thread.join.assert_called_once_with(timeout=2)

    def test_hidden_panel_is_withdrawn_before_the_first_render(self):
        states = []
        with mock.patch.object(widget.App, "render", autospec=True,
                               side_effect=lambda app: states.append(app.root.state())):
            self.make_app()
        self.assertEqual(states, ["withdrawn"])

    def test_network_failures_retry_sooner(self):
        self.assertEqual(widget.next_poll_delay(usage.UsageError("network", "x")), widget.RETRY_SECONDS)
        self.assertEqual(widget.next_poll_delay(usage.UsageError("rate_limited", "x")), widget.BACKOFF_SECONDS)
        self.assertEqual(widget.next_poll_delay(usage.UsageError("login", "x")), widget.POLL_SECONDS)
        self.assertEqual(widget.next_poll_delay(usage.parse_usage(SAMPLE, now=NOW)), widget.POLL_SECONDS)


if __name__ == "__main__":
    unittest.main()
