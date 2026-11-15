import QtQuick
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Marks.js" as Marks

// Bar entry for tim.claude-usage: the 5-hour session limit used, or the weekly
// one on a plan without a session limit, beside a spark. Amber near a limit,
// red when almost out, dimmed while the numbers are stale. While a session
// needs you the spark becomes a message with an alert mark, in red, so the
// colour never carries that alone.
// Left click opens the panel; middle or right click fetches the numbers now.
// One instance per monitor; all state lives in the service.
BarWidget {
  id: root
  moduleName: "tim.claude-usage"

  // serviceFor() is a plain call, so resolve again until the service exists.
  property var service: null
  readonly property var barView: service && service.view.bar ? service.view.bar : ({})
  readonly property int attention: Number(barView.attention) || 0
  readonly property string severity: String(barView.severity || "stale")
  readonly property string colour: Marks.barColour(attention, severity)
  readonly property bool showPercent: String(setting("showPercent", "On")) !== "Off"
  readonly property string tooltip: String(barView.tooltip || "Claude usage")

  // Shape contract for shell.summon/hide/toggle routing: Bar.findPanelWidget
  // requires open/close/opened on the bar-widget root.
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false

  // Forwarded so this widget can stand in for the panel as the bar's popout
  // identity: Bar.requestPopout prefers closeForPopoutSwitch over close, and
  // KeyboardPanel reads popoutSwitchClosing back off its owner.
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  // The panel is created on first open and dropped a while after closing.
  property bool panelWanted: false
  property bool openWhenLoaded: false

  function resolveService() {
    var found = bar && bar.shell && typeof bar.shell.serviceFor === "function"
      ? bar.shell.serviceFor("tim.claude-usage") : null
    if (found !== service) service = found
  }

  // The notification settings and the Stalled threshold are the helper's to act on.
  function sendSettings() {
    if (!service) return
    service.configure({
      notifyAttention: String(setting("notifyAttention", "On")) !== "Off",
      notifyFinished: String(setting("notifyFinished", "On")) !== "Off",
      stalledMinutes: Number(setting("stalledMinutes", 5)) || 5
    })
  }

  function open() {
    panelWanted = true
    if (panelLoader.item) panelLoader.item.open()
    else openWhenLoaded = true
  }

  function close() {
    openWhenLoaded = false
    if (panelLoader.item) panelLoader.item.close()
  }

  function togglePanel() {
    if (opened) close()
    else open()
  }

  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    target.bar = root.bar
    target.settings = root.settings
    target.anchorItem = button
    target.hostWidget = root
    target.service = root.service
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: {
    resolveService()
    injectPanel()
  }
  onSettingsChanged: {
    injectPanel()
    sendSettings()
  }
  onServiceChanged: {
    injectPanel()
    sendSettings()
  }
  onOpenedChanged: {
    if (opened) unloadTimer.stop()
    else unloadTimer.restart()
  }

  Connections {
    target: root.service
    function onFocused() { root.close() }
  }

  Timer {
    interval: 250
    repeat: true
    running: root.service === null && root.bar !== null
    onTriggered: root.resolveService()
  }

  Timer {
    id: unloadTimer
    interval: 45000
    onTriggered: if (!root.opened) root.panelWanted = false
  }

  Loader {
    id: panelLoader
    active: root.panelWanted
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      if (root.openWhenLoaded) {
        root.openWhenLoaded = false
        item.open()
      }
    }
  }

  BarIconButton {
    id: button
    readonly property string percent: root.showPercent && !root.vertical ? String(root.barView.text || "") : ""

    anchors.fill: parent
    bar: root.bar
    text: Marks.barGlyph(root.attention)
    labelText: percent
    slotSize: Style.bar.iconSlot * (percent !== "" ? 2 : 1)
    active: root.colour !== ""
    activeColor: root.colour || foreground
    dimmed: root.severity === "stale" && root.attention === 0
    tooltipText: root.opened ? "" : root.tooltip

    onPressed: function(b) {
      if (b === Qt.LeftButton) root.togglePanel()
      else if (root.service) root.service.refresh()
    }
  }
}
