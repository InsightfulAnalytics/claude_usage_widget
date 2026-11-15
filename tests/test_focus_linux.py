import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import focus_linux as focus  # noqa: E402

# pid: parent pid, shaped like a real tree. A terminal with a shell and a session; VS Code with its
# terminal host; and a tmux server (a child of init) whose client runs in a second terminal.
PARENTS = {
    100: 1,                                   # foot
    110: 100, 111: 110,                       # bash -> claude
    200: 1, 210: 200, 220: 210, 221: 220,     # code -> pty host -> bash -> claude
    300: 1, 310: 300, 311: 310,               # tmux server -> bash -> claude
    400: 1, 410: 400, 411: 410,               # foot -> bash -> tmux client
}
CLIENTS = [
    {"pid": 100, "address": "0xaaa", "focusHistoryID": 2},
    {"pid": 200, "address": "0xb01", "focusHistoryID": 5},
    {"pid": 200, "address": "0xb02", "focusHistoryID": 1},
    {"pid": 400, "address": "0xccc", "focusHistoryID": 0},
    {"pid": "x", "address": "0xddd"},
    {"pid": 500, "address": "not an address"},
]
SOCKET = "/tmp/tmux-1000/default"
TMUX_ENV = {"TMUX": f"{SOCKET},300,0", "TMUX_PANE": "%7"}


class Shell:
    """Stands in for run(): answers hyprctl and tmux, and records every command."""

    def __init__(self, tmux_clients=("5\t411\t$0\t/dev/pts/3",), lua=True, pane="$0\t@2\tWork"):
        self.calls, self.tmux_clients, self.lua, self.pane = [], tmux_clients, lua, pane

    def __call__(self, args):
        self.calls.append(args)
        if args[:3] == ["hyprctl", "clients", "-j"]:
            return json.dumps(CLIENTS)
        if args[:2] == ["hyprctl", "dispatch"]:
            if args[2].startswith("hl.dsp.focus"):
                return "ok\n" if self.lua else None
            return "ok\n"
        if "display-message" in args:
            return self.pane + "\n" if self.pane else None
        if "list-clients" in args:
            return "\n".join(self.tmux_clients) + "\n"
        return ""

    def ran(self, *words):
        return [c for c in self.calls if all(w in c for w in words)]


def go(pid, shell, env=None):
    return focus.focus_session(pid, run=shell, parent_of=PARENTS.get, environ_of=lambda p: env or {})


class Windows(unittest.TestCase):
    def test_the_window_focused_last_stands_for_its_process(self):
        self.assertEqual(focus.windows(Shell()), {100: "0xaaa", 200: "0xb02", 400: "0xccc"})

    def test_no_hyprland_means_no_windows(self):
        self.assertEqual(focus.windows(lambda args: None), {})
        self.assertEqual(focus.windows(lambda args: "not json"), {})

    def test_ancestors_stop_at_init_and_at_a_loop(self):
        self.assertEqual(list(focus.ancestors(111, PARENTS.get)), [111, 110, 100])
        self.assertEqual(list(focus.ancestors(5, {5: 6, 6: 5}.get)), [5, 6])
        self.assertEqual(list(focus.ancestors(999, PARENTS.get)), [999])

    @unittest.skipUnless(sys.platform.startswith("linux"), "reads /proc")
    def test_this_process_has_a_parent(self):
        import os
        self.assertEqual(focus.parent_of(os.getpid()), os.getppid())
        self.assertIsNone(focus.parent_of(2 ** 22 + 1))


class FocusSession(unittest.TestCase):
    def test_a_session_in_a_terminal(self):
        shell = Shell()
        self.assertEqual(go(111, shell), (focus.WINDOW, None))
        self.assertEqual(shell.calls[-1], ["hyprctl", "dispatch", 'hl.dsp.focus({ window = "address:0xaaa" })'])

    def test_a_session_in_a_vs_code_terminal_raises_vs_code(self):
        shell = Shell()
        self.assertEqual(go(221, shell), (focus.WINDOW, None))
        self.assertIn("address:0xb02", shell.calls[-1][-1])

    def test_hyprland_without_a_lua_config_takes_the_plain_dispatcher(self):
        shell = Shell(lua=False)
        self.assertEqual(go(111, shell), (focus.WINDOW, None))
        self.assertEqual(shell.calls[-1], ["hyprctl", "dispatch", "focuswindow", "address:0xaaa"])

    def test_no_window_above_the_session(self):
        self.assertEqual(go(311, Shell()), (None, None))  # tmux server, and no tmux in its environment
        self.assertEqual(go(999, Shell()), (None, None))

    def test_a_tmux_session_shows_its_pane_and_raises_the_clients_terminal(self):
        shell = Shell()
        self.assertEqual(go(311, shell, TMUX_ENV), (focus.WINDOW, None))
        self.assertEqual(shell.ran("select-window"), [["tmux", "-S", SOCKET, "select-window", "-t", "@2"]])
        self.assertEqual(shell.ran("select-pane"), [["tmux", "-S", SOCKET, "select-pane", "-t", "%7"]])
        self.assertEqual(shell.ran("switch-client"), [])
        self.assertIn("address:0xccc", shell.calls[-1][-1])

    def test_a_client_on_another_tmux_session_is_moved_over(self):
        shell = Shell(tmux_clients=("9\t411\t$4\t/dev/pts/3",))
        self.assertEqual(go(311, shell, TMUX_ENV), (focus.WINDOW, None))
        self.assertEqual(shell.ran("switch-client"),
                         [["tmux", "-S", SOCKET, "switch-client", "-c", "/dev/pts/3", "-t", "$0"]])

    def test_the_client_already_on_the_session_wins_over_a_more_recent_one(self):
        shell = Shell(tmux_clients=("9\t111\t$4\t/dev/pts/1", "5\t411\t$0\t/dev/pts/3"))
        self.assertEqual(go(311, shell, TMUX_ENV), (focus.WINDOW, None))
        self.assertEqual(shell.ran("switch-client"), [])
        self.assertIn("address:0xccc", shell.calls[-1][-1])

    def test_a_detached_tmux_session_names_itself(self):
        shell = Shell(tmux_clients=())
        self.assertEqual(go(311, shell, TMUX_ENV), (focus.DETACHED, "Work"))
        self.assertEqual(shell.ran("hyprctl"), [])

    def test_a_pane_tmux_no_longer_knows(self):
        self.assertEqual(go(311, Shell(pane=None), TMUX_ENV), (None, None))

    def test_a_tmux_environment_that_is_not_one(self):
        for env in ({"TMUX": "relative,1,0", "TMUX_PANE": "%7"}, {"TMUX": f"{SOCKET},1,0", "TMUX_PANE": "7; rm -rf /"},
                    {"TMUX_PANE": "%7"}):
            self.assertIsNone(focus.tmux_pane(env))
        self.assertEqual(focus.tmux_pane(TMUX_ENV), (SOCKET, "%7"))


class Commands(unittest.TestCase):
    def test_resume_and_attach(self):
        self.assertEqual(focus.resume_command("abc-123"), "claude --resume abc-123")
        self.assertEqual(focus.attach_command("Work"), "tmux attach -t 'Work'")
        self.assertEqual(focus.attach_command("it's"), "tmux attach -t 'it'\\''s'")

    def test_a_folder_that_is_gone_is_not_opened(self):
        self.assertFalse(focus.open_folder("/no/such/folder/here"))
        self.assertFalse(focus.open_folder(""))


if __name__ == "__main__":
    unittest.main()
