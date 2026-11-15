import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Marks.js" as Marks

// The Claude usage dropdown. BarWidget.qml loads this and injects the bar, the
// anchor button, the widget itself (bar identity) and the service.
//
// It draws service.view as it comes: a meter per plan limit with its reset
// time, then a row per running Claude Code session with its state, context
// fill and token total, and under them the details of the session the cursor
// is on. Session names, folders and waiting reasons are text from files Claude
// Code writes, so every Text here is PlainText.
// Mouse: click a session to go to its terminal · right-click copies its resume
// command · middle-click opens its folder.
// Keys: j / k / arrows pick a session · Enter goes to it · C copies its resume
// command · O opens its folder · R fetches the numbers now · Tab next panel ·
// Esc close.
Panel {
  id: claude
  moduleName: "tim.claude-usage"
  ipcTarget: "tim.claude-usage"
  manageIpc: false

  property var anchorItem: null
  property var service: null

  // The bar tracks the widget mounted in its slot, not this nested panel, so
  // the popout coordinator must compare against that widget.
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || claude

  readonly property var view: service ? service.view : ({ state: "starting", bar: {}, message: "Loading...", limits: [], sessions: [] })
  readonly property var barView: view.bar || ({})
  readonly property var limits: view.limits || []
  readonly property var sessions: view.sessions || []
  readonly property bool checking: service ? service.checking : false
  readonly property string flash: String(view.flash || "")

  // The session row the keyboard (or the pointer) is on; -1 for none. It follows its
  // session by PID when a change of state re-sorts the rows.
  property int cursor: -1
  property int cursorPid: 0
  readonly property string details: cursor >= 0 && cursor < sessions.length ? String(sessions[cursor].details || "") : ""

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  function ink(alpha) {
    return Qt.rgba(foreground.r, foreground.g, foreground.b, alpha)
  }

  // A meter's fill carries the severity; its track is a dim step of the same
  // colour, so the state reads across the whole bar.
  function fillColour(severity) {
    var colour = Marks.severityColour(severity)
    if (colour !== "") return colour
    return ink(severity === "stale" ? 0.3 : 0.62)
  }

  function trackColour(severity) {
    var colour = Marks.severityColour(severity)
    if (colour === "") return ink(0.14)
    var c = Qt.color(colour)
    return Qt.rgba(c.r, c.g, c.b, 0.28)
  }

  function refresh() {
    if (service) service.refresh()
  }

  function act(verb, index) {
    var entry = index >= 0 && index < sessions.length ? sessions[index] : null
    if (entry && service) service.act(verb, entry.pid)
  }

  function moveCursor(rows) {
    if (sessions.length === 0) {
      scrollBy(rows)
      return
    }
    cursor = Math.max(0, Math.min(sessions.length - 1, cursor < 0 ? (rows > 0 ? 0 : sessions.length - 1) : cursor + rows))
    var row = sessionRepeater.itemAt(cursor)
    if (!row) return
    var top = row.mapToItem(column, 0, 0).y
    if (top < flick.contentY) flick.contentY = top
    else if (top + row.height > flick.contentY + flick.height) flick.contentY = top + row.height - flick.height
  }

  function scrollBy(rows) {
    var maxY = Math.max(0, flick.contentHeight - flick.height)
    flick.contentY = Math.max(0, Math.min(maxY, flick.contentY + rows * Style.space(48)))
  }

  onCursorChanged: cursorPid = cursor >= 0 && cursor < sessions.length ? Number(sessions[cursor].pid) || 0 : 0

  onSessionsChanged: {
    if (cursor < 0) return
    for (var i = 0; i < sessions.length; i++) {
      if (Number(sessions[i].pid) === cursorPid) {
        cursor = i
        return
      }
    }
    cursor = -1  // that session ended
  }

  onOpenedChanged: if (opened) {
    flick.contentY = 0
    cursor = -1
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  KeyboardPanel {
    id: panel
    anchorItem: claude.anchorItem
    owner: claude.barIdentity
    bar: claude.bar
    open: claude.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(900))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onCloseRequested: claude.close()
      onTabRequested: function(direction) { claude.switchPanel(direction) }
      onMoveRequested: function(dx, dy) { if (dy !== 0) claude.moveCursor(dy) }
      onActivateRequested: {
        if (claude.cursor >= 0) claude.act("focus", claude.cursor)
        else claude.refresh()
      }
      onTextKey: function(t) {
        if (t === "r" || t === "R") claude.refresh()
        else if (t === "c" || t === "C") claude.act("copy", claude.cursor)
        else if (t === "o" || t === "O") claude.act("folder", claude.cursor)
      }

      Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: flick.width
          spacing: Style.space(10)

          PanelHero {
            width: parent.width
            title: "Claude usage"
            // What is wrong goes in the pill; otherwise the age of the numbers sits under the title.
            detail: claude.view.noteStrong === true ? String(claude.view.note || "") : ""
            meta: claude.view.noteStrong === true ? "" : String(claude.view.note || "")
            foreground: claude.foreground
            fontFamily: claude.fontFamily
            iconOpacity: claude.barView.severity === "stale" ? 0.5 : 1.0
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: Marks.SPARK
                color: Marks.severityColour(String(claude.barView.severity || "")) || claude.foreground
                font.family: claude.fontFamily
                font.pixelSize: Style.font.display
              }
            }
            trailingControl: Component {
              PanelActionButton {
                iconText: Marks.REFRESH
                tooltipText: claude.checking ? "Fetching..." : "Fetch the numbers now (R)"
                foreground: claude.foreground
                fontFamily: claude.fontFamily
                enabled: !claude.checking
                opacity: claude.checking ? 0.45 : 1
                onClicked: claude.refresh()
              }
            }
          }

          Text {
            visible: text !== ""
            textFormat: Text.PlainText
            width: parent.width
            text: String(claude.view.message || "")
            color: claude.view.noteStrong === true ? claude.foreground : claude.dim
            font.family: claude.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Column {
            width: parent.width
            spacing: Style.space(12)
            visible: limitRepeater.count > 0

            // The model is the count, and each block reads its entry by index: a new view
            // (every couple of seconds while a session runs) then updates the blocks in
            // place. A list model would rebuild them all each time.
            Repeater {
              id: limitRepeater
              model: claude.limits.length

              LimitBlock {
                required property int index
                width: parent.width
                entry: claude.limits[index] || ({})
              }
            }
          }

          Text {
            visible: text !== ""
            textFormat: Text.PlainText
            width: parent.width
            text: String(claude.view.breakdown || "")
            color: claude.dim
            font.family: claude.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          PanelSeparator { foreground: claude.foreground }

          RowLayout {
            width: parent.width
            spacing: Style.space(8)

            PanelSectionHeader {
              text: "Sessions"
              foreground: claude.foreground
              fontFamily: claude.fontFamily
              Layout.alignment: Qt.AlignVCenter
            }

            // What a click just did, else how many sessions there are or need you.
            Text {
              textFormat: Text.PlainText
              text: claude.flash !== "" ? claude.flash : String(claude.view.sessionsNote || "")
              color: claude.flash !== "" ? claude.foreground
                : claude.view.sessionsUrgent === true ? Marks.ERROR : claude.dim
              font.family: claude.fontFamily
              font.pixelSize: Style.font.bodySmall
              horizontalAlignment: Text.AlignRight
              elide: Text.ElideLeft
              Layout.fillWidth: true
              Layout.alignment: Qt.AlignVCenter
            }
          }

          Text {
            visible: claude.sessions.length === 0
            textFormat: Text.PlainText
            width: parent.width
            text: "No Claude Code sessions running"
            color: claude.dim
            font.family: claude.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Column {
            width: parent.width
            spacing: Style.space(2)
            visible: claude.sessions.length > 0

            // By count, as the limits are: rebuilt rows would drop the pointer's hover every
            // time the view changed.
            Repeater {
              id: sessionRepeater
              model: claude.sessions.length

              SessionRow {
                required property int index
                width: parent.width
                entry: claude.sessions[index] || ({})
                row: index
              }
            }
          }

          // The session the cursor is on, in full: its folder, context, token split, model
          // and ID, then what the keys and buttons do with it.
          PanelSeparator {
            visible: claude.details !== ""
            foreground: claude.foreground
          }

          Text {
            visible: claude.details !== ""
            textFormat: Text.PlainText
            width: parent.width
            text: claude.details
            color: claude.dim
            font.family: claude.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.Wrap
          }
        }
      }
    }
  }

  // A rounded meter: `percent` of the track filled in the severity's colour.
  component Meter: Rectangle {
    id: meter
    property real percent: 0
    property string severity: "normal"
    readonly property real share: Math.max(0, Math.min(100, Number(percent) || 0)) / 100

    radius: height / 2
    color: claude.trackColour(severity)

    Rectangle {
      visible: meter.share > 0
      // Never narrower than it is tall, so a small share still has round ends.
      width: Math.max(parent.height, parent.width * meter.share)
      height: parent.height
      radius: parent.radius
      color: claude.fillColour(meter.severity)
    }
  }

  // One plan limit: its name and the share used, the meter, then when it
  // resets and, near the limit, what that means.
  component LimitBlock: Column {
    id: limitBlock
    property var entry: ({})
    readonly property string severity: String(entry.severity || "normal")

    spacing: Style.space(4)

    RowLayout {
      width: parent.width
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        text: String(limitBlock.entry.label || "")
        color: claude.foreground
        font.family: claude.fontFamily
        font.pixelSize: Style.font.body
        elide: Text.ElideRight
        Layout.fillWidth: true
      }

      Text {
        textFormat: Text.PlainText
        text: String(limitBlock.entry.percentText || "")
        color: claude.foreground
        font.family: claude.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
      }
    }

    Meter {
      width: parent.width
      height: Style.space(8)
      percent: Number(limitBlock.entry.percent) || 0
      severity: limitBlock.severity
    }

    RowLayout {
      width: parent.width
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        text: String(limitBlock.entry.reset || "")
        color: claude.dim
        font.family: claude.fontFamily
        font.pixelSize: Style.font.bodySmall
        elide: Text.ElideRight
        Layout.fillWidth: true
      }

      Text {
        visible: String(limitBlock.entry.status || "") !== ""
        textFormat: Text.PlainText
        text: Marks.ALERT + " " + String(limitBlock.entry.status || "")
        color: Marks.severityColour(limitBlock.severity) || claude.foreground
        font.family: claude.fontFamily
        font.pixelSize: Style.font.bodySmall
      }
    }
  }

  // One session: a dot in its state's colour (a ring when Stalled), its name,
  // the context fill as a small meter with a %, then the state with how long
  // it has lasted and the session's token total.
  component SessionRow: CursorSurface {
    id: sessionRow
    property var entry: ({})
    property int row: -1
    readonly property string sessionState: String(entry.state || "unknown")
    readonly property color stateColour: Marks.stateColour(sessionState, claude.dim)
    readonly property bool hasPercent: entry.percent !== null && entry.percent !== undefined

    hasCursor: claude.cursor === row
    foreground: claude.foreground
    implicitHeight: lines.implicitHeight + Style.space(10)

    Column {
      id: lines
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(6)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(2)

      RowLayout {
        width: parent.width
        spacing: Style.space(8)

        Item {
          Layout.preferredWidth: Style.space(10)
          Layout.preferredHeight: Style.space(10)
          Layout.alignment: Qt.AlignVCenter

          Rectangle {
            anchors.centerIn: parent
            width: Style.space(8)
            height: width
            radius: width / 2
            color: sessionRow.sessionState === "stalled" ? "transparent" : sessionRow.stateColour
            border.width: sessionRow.sessionState === "stalled" ? Math.max(1, Style.space(2)) : 0
            border.color: sessionRow.stateColour
          }
        }

        Text {
          textFormat: Text.PlainText
          text: String(sessionRow.entry.name || "")
          color: claude.foreground
          font.family: claude.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
          Layout.fillWidth: true
          Layout.alignment: Qt.AlignVCenter
        }

        Meter {
          visible: sessionRow.hasPercent
          percent: Number(sessionRow.entry.percent) || 0
          severity: String(sessionRow.entry.contextSeverity || "normal")
          Layout.preferredWidth: Style.space(44)
          Layout.preferredHeight: Style.space(5)
          Layout.alignment: Qt.AlignVCenter
        }

        Text {
          visible: sessionRow.hasPercent
          textFormat: Text.PlainText
          text: String(sessionRow.entry.percentText || "")
          color: claude.foreground
          font.family: claude.fontFamily
          font.pixelSize: Style.font.bodySmall
          horizontalAlignment: Text.AlignRight
          Layout.preferredWidth: Style.space(32)
          Layout.alignment: Qt.AlignVCenter
        }
      }

      RowLayout {
        width: parent.width
        spacing: Style.space(8)

        Item { Layout.preferredWidth: Style.space(10) }

        Text {
          textFormat: Text.PlainText
          text: String(sessionRow.entry.stateText || "")
          color: sessionRow.stateColour
          font.family: claude.fontFamily
          font.pixelSize: Style.font.bodySmall
          elide: Text.ElideRight
          Layout.fillWidth: true
        }

        Text {
          textFormat: Text.PlainText
          text: String(sessionRow.entry.total || "")
          color: claude.dim
          font.family: claude.fontFamily
          font.pixelSize: Style.font.bodySmall
        }
      }
    }

    MouseArea {
      id: rowMouse
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton
      cursorShape: Qt.PointingHandCursor
      // Only a pointer that moves takes the cursor, so rows scrolling under a
      // still pointer don't pull it away from the keyboard.
      onPositionChanged: claude.cursor = sessionRow.row
      onClicked: function(mouse) {
        claude.cursor = sessionRow.row
        claude.act(mouse.button === Qt.RightButton ? "copy" : mouse.button === Qt.MiddleButton ? "folder" : "focus",
                   sessionRow.row)
      }
    }
  }
}
