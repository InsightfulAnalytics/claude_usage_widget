import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import focus  # noqa: E402

# pid: (parent pid, exe), shaped like a real tree: VS Code main -> terminal host -> shell -> claude.
PROCS = {
    100: (1, "explorer.exe"),
    200: (100, "Code.exe"),
    210: (200, "Code.exe"),
    300: (210, "powershell.exe"),
    301: (300, "claude.exe"),
    400: (210, "cmd.exe"),
    401: (400, "pwsh.exe"),
    402: (401, "claude.exe"),
    500: (100, "powershell.exe"),
    501: (500, "claude.exe"),
}


class VSCodeHost(unittest.TestCase):
    def test_shell_and_terminal_host(self):
        self.assertEqual(focus.vscode_host(301, PROCS), (300, 210))

    def test_nested_shells_use_the_one_vs_code_started(self):
        self.assertEqual(focus.vscode_host(402, PROCS), (400, 210))

    def test_console_session_is_not_vs_code(self):
        self.assertIsNone(focus.vscode_host(501, PROCS))

    def test_unknown_pid(self):
        self.assertIsNone(focus.vscode_host(999, PROCS))

    def test_parent_loop_ends(self):
        # A stale parent PID can point back down the tree once PIDs are reused.
        self.assertIsNone(focus.vscode_host(1, {1: (2, "a.exe"), 2: (1, "b.exe")}))

    def test_main_code_process_owns_the_windows(self):
        self.assertEqual(focus.top_code(210, PROCS), 200)
        self.assertEqual(focus.top_code(200, PROCS), 200)


class FocusSession(unittest.TestCase):
    def vs_code(self, replies, windows=(11, 22), titles=None):
        """focus_session(301) with the given pipe replies ({pipe name: reply or None})."""
        titles = titles or {11: "a.md - other - Visual Studio Code", 22: "b.md - my-repo - Visual Studio Code"}
        asked = []

        def ask(name, shell_pid):
            asked.append((name, shell_pid))
            return replies[name]

        with mock.patch.object(focus, "processes", return_value=PROCS), \
                mock.patch.object(focus, "focus_pipes", return_value=list(replies)), \
                mock.patch.object(focus, "ask_pipe", side_effect=ask), \
                mock.patch.object(focus, "windows_of", return_value=list(windows)) as windows_of, \
                mock.patch.object(focus, "window_title", side_effect=titles.get), \
                mock.patch.object(focus, "raise_window") as raise_window, \
                mock.patch.object(focus.os, "startfile") as startfile:
            result = focus.focus_session(301)
        windows_of.assert_called_once_with(200)  # the open VS Code; nothing is launched
        startfile.assert_not_called()
        return result, asked, raise_window

    def test_vs_code_session_shows_its_tab_and_raises_that_window(self):
        result, asked, raise_window = self.vs_code({"p1": {"found": False, "workspace": "other"},
                                                     "p2": {"found": True, "workspace": "my-repo"},
                                                     "p3": {"found": False}})
        self.assertEqual(result, focus.TAB)
        self.assertEqual(asked, [("p1", 300), ("p2", 300)])  # stops at the window that has it
        raise_window.assert_called_once_with(22)

    def test_no_extension_still_raises_vs_code(self):
        result, _, raise_window = self.vs_code({"p1": None})
        self.assertEqual(result, focus.NO_EXTENSION)
        raise_window.assert_called_once_with(11)

    def test_tab_in_no_window(self):
        result, _, _ = self.vs_code({"p1": {"found": False}})
        self.assertEqual(result, focus.NO_TAB)

    def test_console_session_raises_its_window(self):
        with mock.patch.object(focus, "processes", return_value=PROCS), \
                mock.patch.object(focus, "console_window", return_value=1234), \
                mock.patch.object(focus, "raise_window", return_value=True) as raise_window:
            self.assertEqual(focus.focus_session(501), focus.CONSOLE)
        raise_window.assert_called_once_with(1234)

    def test_nothing_found(self):
        with mock.patch.object(focus, "processes", return_value=PROCS), \
                mock.patch.object(focus, "console_window", return_value=None):
            self.assertIsNone(focus.focus_session(501))

    def test_resume_command(self):
        self.assertEqual(focus.resume_command("abc-123"), "claude --resume abc-123")


if __name__ == "__main__":
    unittest.main()
