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
    def test_vs_code_session_opens_the_focus_uri(self):
        with mock.patch.object(focus, "processes", return_value=PROCS), \
                mock.patch.object(focus, "windows_of", return_value=[]) as windows_of, \
                mock.patch.object(focus.os, "startfile") as startfile:
            self.assertEqual(focus.focus_session(301), "vscode")
        windows_of.assert_called_once_with(200)
        startfile.assert_called_once_with("vscode://timosborn.claude-context-status/focus?shellPid=300")

    def test_console_session_raises_its_window(self):
        with mock.patch.object(focus, "processes", return_value=PROCS), \
                mock.patch.object(focus, "console_window", return_value=1234), \
                mock.patch.object(focus, "raise_window", return_value=True) as raise_window:
            self.assertEqual(focus.focus_session(501), "console")
        raise_window.assert_called_once_with(1234)

    def test_nothing_found(self):
        with mock.patch.object(focus, "processes", return_value=PROCS), \
                mock.patch.object(focus, "console_window", return_value=None):
            self.assertIsNone(focus.focus_session(501))

    def test_resume_command(self):
        self.assertEqual(focus.resume_command("abc-123"), "claude --resume abc-123")


if __name__ == "__main__":
    unittest.main()
