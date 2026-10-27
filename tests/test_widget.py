import copy
import json
import sys
import tempfile
import unittest
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


class Render(unittest.TestCase):
    """The panel must update in place: rebuilding its widgets on every tick made it flicker."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "settings.json").write_text(json.dumps({"visible": False}), encoding="utf-8")
        for name, value in (("DATA_DIR", tmp), ("SETTINGS_FILE", tmp / "settings.json")):
            patcher = mock.patch.object(widget, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = widget.App()
        self.addCleanup(self.app.root.destroy)
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
        with mock.patch.object(widget.tk.Label, "configure", wraps=None) as configure:
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


if __name__ == "__main__":
    unittest.main()
