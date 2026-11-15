import QtQuick
import Quickshell
import Quickshell.Io

// The one service instance for tim.claude-usage. It runs
// claude_usage_status.py, which fetches the plan usage, watches the Claude
// Code sessions, raises the notifications and prints a ready-to-draw view as
// one JSON line whenever it changes. Bar widgets, one per monitor, read `view`
// through bar.shell.serviceFor("tim.claude-usage"), so the usage endpoint is
// asked and a change is announced once however many screens there are.
Item {
  id: root

  property var shell: null
  property var manifest: null

  // The shell strips manifest.__sourceDir for third-party plugins, so the
  // plugin finds its own files relative to this QML file.
  readonly property string rootDir: localPath(Qt.resolvedUrl("."))

  readonly property var emptyView: ({
    state: "starting",
    bar: { text: "", severity: "stale", attention: 0, tooltip: "Claude usage: loading" },
    note: "", noteStrong: false, message: "Loading...", limits: [], breakdown: "",
    sessionsNote: "", sessionsUrgent: false, sessions: [], flash: ""
  })
  property var view: emptyView
  // True from a refresh request until the next view arrives.
  property bool checking: false
  // The widget's settings as the helper takes them; sent again whenever the helper starts.
  property string config: ""

  // A session's terminal came forward: the panels close.
  signal focused()

  function localPath(url) {
    var path = String(url || "")
    if (path.indexOf("file://") === 0) path = decodeURIComponent(path.slice(7))
    return path.replace(/\/+$/, "")
  }

  function refresh() {
    if (!proc.running) {
      restart.stop()
      proc.running = true
      return
    }
    checking = true
    checkingTimeout.restart()
    proc.write("refresh\n")
  }

  function configure(settings) {
    var next = JSON.stringify(settings)
    if (next === config) return
    config = next
    if (proc.running) proc.write("config " + config + "\n")
  }

  // "focus", "copy" or "folder" for the live session with that PID.
  function act(verb, pid) {
    var id = Math.floor(Number(pid))
    if (!proc.running || !/^(focus|copy|folder)$/.test(String(verb)) || !(id > 0)) return
    proc.write(verb + " " + id + "\n")
  }

  function handleLine(line) {
    var parsed = null
    try { parsed = JSON.parse(line) } catch (e) { parsed = null }
    if (parsed && parsed.event === "focused") {
      focused()
      return
    }
    if (!parsed || typeof parsed !== "object" || !parsed.state || typeof parsed.bar !== "object") {
      console.warn("claude-usage: unexpected helper output: " + String(line).slice(0, 200))
      return
    }
    if (!Array.isArray(parsed.limits)) parsed.limits = []
    if (!Array.isArray(parsed.sessions)) parsed.sessions = []
    view = parsed
    checking = false
  }

  function helperStopped(exitCode) {
    checking = false
    view = {
      state: "error",
      bar: { text: "", severity: "stale", attention: 0, tooltip: "Claude usage: the helper stopped" },
      note: "Helper stopped", noteStrong: true,
      message: "claude_usage_status.py exited with code " + exitCode + ". Starting it again in 30 seconds; "
               + "see ~/.local/state/claude_usage_widget/widget.log.",
      limits: [], breakdown: "", sessionsNote: "", sessionsUrgent: false, sessions: [], flash: ""
    }
    restart.restart()
  }

  Process {
    id: proc
    command: ["python3", root.rootDir + "/claude_usage_status.py"]
    // Nothing may be written into the watched plugin directory.
    environment: ({ PYTHONDONTWRITEBYTECODE: "1", PYTHONUNBUFFERED: "1" })
    stdinEnabled: true
    running: true

    stdout: SplitParser {
      onRead: data => root.handleLine(data)
    }

    stderr: SplitParser {
      onRead: data => console.warn("claude-usage helper: " + data)
    }

    onStarted: if (root.config !== "") proc.write("config " + root.config + "\n")
    onExited: (exitCode, exitStatus) => root.helperStopped(exitCode)
  }

  Timer {
    id: restart
    interval: 30000
    onTriggered: proc.running = true
  }

  // A refresh that brings the same numbers prints nothing, so the mark clears by itself.
  Timer {
    id: checkingTimeout
    interval: 4000
    onTriggered: root.checking = false
  }
}
