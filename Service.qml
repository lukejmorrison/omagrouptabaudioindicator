import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

// Click-through speakers for the group pills named in group-audio.json.
// The empty mask is the same input region the OSD uses, so the group bar
// still receives the click.
Item {
  id: root

  property var marks: []
  property string parsed: ""
  property color activeInk: "#070B09"
  // Qt reads an 8-digit hex as AARRGGBB. #ffffff88 is opaque yellow, not dim white.
  property color inactiveInk: Qt.rgba(1, 1, 1, 136 / 255)

  readonly property string stateHome: {
    var fromEnv = Quickshell.env("XDG_STATE_HOME")
    if (fromEnv && fromEnv.length > 0)
      return fromEnv
    return Quickshell.env("HOME") + "/.local/state"
  }
  readonly property string statePath: stateHome + "/omarchy/group-audio.json"
  readonly property string colorsPath: stateHome + "/omarchy/current/theme/colors.toml"

  function hexOf(text, key, fallback) {
    var match = new RegExp(key + "\\s*=\\s*\"#([0-9A-Fa-f]{6})\"").exec(text || "")
    return match ? match[1] : fallback
  }

  function linear(value) {
    if (value <= 0.04045)
      return value / 12.92
    return Math.pow((value + 0.055) / 1.055, 2.4)
  }

  function channel(hex, index) {
    return parseInt(hex.substr(index, 2), 16) / 255
  }

  function luma(hex) {
    return 0.2126 * root.linear(root.channel(hex, 0))
      + 0.7152 * root.linear(root.channel(hex, 2))
      + 0.0722 * root.linear(root.channel(hex, 4))
  }

  function applyColors(raw) {
    var accent = root.hexOf(raw, "accent", "39CFAF")
    var background = root.hexOf(raw, "background", "070B09")
    var foreground = root.hexOf(raw, "foreground", "FFFFFF")
    root.activeInk = "#" + (root.luma(accent) > 0.4 ? background : foreground)
  }

  function applyMarks(raw) {
    if (raw === root.parsed)
      return
    try {
      var payload = JSON.parse(raw || "{}")
      root.marks = Array.isArray(payload.windows) ? payload.windows : []
      root.parsed = raw
    } catch (error) {
      // A torn read keeps the last good set. The next reload replaces it.
    }
  }

  function forScreen(screen) {
    var found = []
    var marks = root.marks
    for (var index = 0; index < marks.length; index++) {
      var mark = marks[index]
      if (mark && mark.monitor === screen.name)
        found.push(mark)
    }
    return found
  }

  FileView {
    id: marksFile
    path: root.statePath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.applyMarks(text())
    onLoadFailed: {
      root.parsed = ""
      root.marks = []
    }
  }

  FileView {
    id: colorsFile
    path: root.colorsPath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.applyColors(text())
  }

  // Atomic replace can drop the inotify watch. The usage records in the
  // shell already reload on a timer for the same reason.
  Timer {
    interval: 500
    running: true
    repeat: true
    onTriggered: marksFile.reload()
  }

  Variants {
    model: Quickshell.screens

    PanelWindow {
      id: panel
      required property var modelData

      readonly property var mine: root.forScreen(modelData)

      screen: modelData
      visible: mine.length > 0
      color: "transparent"
      exclusionMode: ExclusionMode.Ignore
      WlrLayershell.namespace: "wizwam-group-audio"
      WlrLayershell.layer: WlrLayer.Top
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
      anchors {
        top: true
        bottom: true
        left: true
        right: true
      }
      mask: Region {}

      Repeater {
        model: panel.mine

        delegate: Item {
          id: speaker
          required property var modelData
          readonly property color ink: modelData.focused ? root.activeInk : root.inactiveInk
          width: 14
          height: 14
          x: Math.round(modelData.x + modelData.width - (modelData.iconEdge || 6) - width)
          y: Math.round(modelData.y + (modelData.height - height) / 2)

          Canvas {
            id: glyph
            anchors.fill: parent
            property color ink: speaker.ink
            onInkChanged: requestPaint()
            onWidthChanged: requestPaint()
            Component.onCompleted: requestPaint()
            onPaint: {
              var ctx = getContext("2d")
              ctx.clearRect(0, 0, width, height)
              ctx.fillStyle = glyph.ink
              ctx.strokeStyle = glyph.ink
              ctx.lineCap = "round"
              ctx.lineWidth = Math.max(1, width / 14 * 1.25)
              var s = width / 14
              ctx.fillRect(1 * s, 5 * s, 3 * s, 4 * s)
              ctx.beginPath()
              ctx.moveTo(4 * s, 5 * s)
              ctx.lineTo(8 * s, 2 * s)
              ctx.lineTo(8 * s, 12 * s)
              ctx.closePath()
              ctx.fill()
              ctx.beginPath()
              ctx.arc(8.2 * s, 7 * s, 2.6 * s, -0.9, 0.9, false)
              ctx.stroke()
              ctx.beginPath()
              ctx.arc(8.2 * s, 7 * s, 4.6 * s, -0.8, 0.8, false)
              ctx.stroke()
            }
          }
        }
      }
    }
  }
}
