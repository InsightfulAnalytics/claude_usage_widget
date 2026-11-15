.pragma library

// Glyphs (Material Design icons in the Nerd Font) and status colours shared by
// the bar icon and the panel. The colours are the warning, critical and running
// steps the Windows panel uses; they read on dark and light themes alike.

var WARN = "#fab219"
var ERROR = "#d03b3b"
var RUNNING = "#3fae6a"

function glyph(codePoint) { return String.fromCodePoint(codePoint) }

var SPARK = glyph(0xF06C4)      // asterisk
var ASKING = glyph(0xF0362)     // message-alert
var REFRESH = glyph(0xF0450)
var ALERT = glyph(0xF0026)

// The bar icon: the spark, or a message with an alert mark while a session
// needs you, so the colour never carries that alone.
function barGlyph(attention) {
  return attention > 0 ? ASKING : SPARK
}

// The colour for a limit's severity, or "" for the ink the caller uses anyway.
function severityColour(severity) {
  if (severity === "warning") return WARN
  if (severity === "critical") return ERROR
  return ""
}

// The bar icon's colour: red while a session needs you or a limit is almost
// out, amber near a limit, "" (the bar's own ink) otherwise.
function barColour(attention, severity) {
  return attention > 0 ? ERROR : severityColour(severity)
}

// A session state's colour; `neutral` for Unknown.
function stateColour(state, neutral) {
  if (state === "needs_you" || state === "stalled") return ERROR
  if (state === "your_turn") return WARN
  if (state === "running") return RUNNING
  return neutral
}
