import copy
import json
import sys
import tempfile
import tkinter as tk
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import usage  # noqa: E402
import widget  # noqa: E402
from test_usage import NOW, SAMPLE  # noqa: E402


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
