# claude_usage_widget

A Windows tray icon and small desktop panel showing Claude plan usage (the 5-hour session limit,
the weekly limits and when each resets, the same numbers as Settings > Usage in the Claude app)
and every Claude Code session running on the machine: which ones need you, which are still
running, how full each one's context is and how many tokens it has used.

![The panel: plan usage bars on top, then six sessions with their state, context bar and token total](docs/panel.png)

## How it gets the numbers

It reads the OAuth token Claude Code stores in `%USERPROFILE%\.claude\.credentials.json`
(`CLAUDE_CONFIG_DIR` is respected) and calls `https://api.anthropic.com/api/oauth/usage`, the
endpoint behind Claude Code's `/usage`. It polls every 3 minutes, retries after 30 seconds when
offline (at login or after sleep) and backs off to 10 minutes if rate limited.

- The endpoint is undocumented and may change. `usage.py` reads the `limits` list first, skips any
  entry it can't read, and falls back to the older `five_hour` / `seven_day` keys if none is left.
  If nothing is readable the panel says "Unexpected data" and keeps the last good numbers.
- Errors go to `widget.log` (see below), since `pythonw` has no console.
- The widget never refreshes the token: refresh tokens rotate, so doing it here would log Claude
  Code out. When the token expires the panel says so; running Claude Code once refreshes it.

## Sessions

Each running Claude Code process keeps `%USERPROFILE%\.claude\sessions\<pid>.json` up to date
with its status (`busy`, `idle` or `waiting`, plus what it waits for). The panel reads those files
every 2 seconds, skips any whose process has exited (the PID must be running and have the start
time the file records), and reads token usage from the session transcripts under
`.claude\projects`: the whole file once, then only what was added.

| State | Shown as | Means |
| --- | --- | --- |
| Needs you | red dot | Blocked on a permission prompt, a question or a dialog until you act. Says what it waits for. |
| Stalled | red ring | Busy, but nothing has been written to its transcript (or its subagents') for 5 minutes. A long build or test run looks the same. |
| Your turn | amber dot | The turn has ended; it waits for your next prompt. |
| Running | green dot | Working on a turn. |
| Unknown | grey dot | The status could not be read (a Claude Code update may have changed the file). |

Each row shows the time in that state, the context fill as a small bar with a % (amber from 60%,
red from 80%), and the session total: every token the session and its subagents have used,
counting each reply once. Context and window size follow the
[claude-context-status](https://github.com/InsightfulAnalytics/claude-context-status) VS Code
extension: the latest main-conversation reply's input + cache tokens, against the window named by
the session's model or Claude Code's `model` setting (`opus[1m]` means 1M), else 200k; usage
above 200k means 1M. Hover a row for the details.

- **Click a row** to go to its terminal. For a session in a VS Code terminal the panel raises the
  VS Code window that is already open and asks claude-context-status (0.3.0 or later) over its
  named pipe to show that terminal; it never starts VS Code, so no second window opens. Without
  the extension (or before VS Code is reloaded after installing it) only the window comes forward.
  A session in a console window brings that window forward; for Windows Terminal that is the
  window, not the tab. A session with no terminal to bring forward gets its resume command
  (`claude --resume <id>`) copied instead, to paste into a new terminal.
- **Right-click a row** for Go to terminal, Copy resume command (`claude --resume <id>`), Open
  transcript (selects it in Explorer) and Open folder.
- **More than 8 sessions:** scroll the mouse wheel over the panel.
- **Notifications:** a Windows notification when a session turns Needs you or Stalled, and when a
  turn of 2 minutes or more finishes. Nothing fires for states already there when the widget
  starts. Turn either off from the tray menu.
- **Tray icon:** a red dot in its corner while any session needs you or is stalled; the hover text
  gives the count.

The sessions file is internal to Claude Code and undocumented, so an update can change it. The
widget never writes anything under `.claude`.

## Run

Needs Python 3.10+ on Windows with `pystray` and `Pillow` (`pip install -r requirements.txt`).

```powershell
pythonw widget.py
```

`pythonw` keeps a console window from opening. A second launch exits straight away, so only one
icon ever shows.

## Use

- **Tray icon:** the session % on a coloured square. Blue below 75%, amber from 75%, red from 90%,
  grey when the numbers are stale (offline, rate limited or logged out). A red corner dot while a
  session needs you. Hover for both limits and their reset times. Windows 11 puts new icons in the overflow (^): drag it onto the taskbar to
  keep it visible.
- **Left-click the icon** to show or hide the panel.
- **Panel:** drag it anywhere; it stays inside the monitor it sits on. Right-click it for Refresh,
  Keep on top, Hide and Quit.
- **Tray menu** (right-click the icon): Show panel, Refresh now, Keep panel on top, Notify when a
  session needs you, Notify when a long turn finishes, Start with Windows, Quit. Start with Windows writes `ClaudeUsageWidget` under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`; unticking it removes the value.

Panel position and toggles are saved in `%LOCALAPPDATA%\claude_usage_widget\settings.json`;
warnings go to `widget.log` beside it. `stalled_minutes` in that file (default 5) sets when a busy
session counts as Stalled; restart the widget after changing it.

## Test

```powershell
python -m unittest discover -s tests
```

## Files

- `usage.py`: token, fetch, parsing and formatting. No UI, covered by `tests/`.
- `sessions.py`: live sessions, their state, transcripts, window size and notification rules. No
  UI, covered by `tests/`.
- `focus.py`: finds the terminal a session runs in and brings it forward (Windows only).
- `widget.py`: tray icon (pystray), panel (tkinter), polling and session threads, settings.

## Licence

MIT. Not affiliated with or endorsed by Anthropic.
