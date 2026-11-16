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
  property int waveIndex: 0
  // 0 cone, 1 one wave, 2 both waves, then back to one wave.
  readonly property int wavePhase: waveIndex === 2 ? 2 : (waveIndex === 0 ? 0 : 1)
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
      root.syncWaveTimer()
    } catch (error) {
      // A torn read keeps the last good set. The next reload replaces it.
    }
  }

  function syncWaveTimer() {
    waveTimer.running = !!(root.marks && root.marks.length > 0)
  }

  function tipOf(mark) {
    var parts = []
    if (mark && mark.tipApp)
      parts.push(mark.tipApp)
    if (mark && mark.tipMedia)
      parts.push(mark.tipMedia)
    if (mark && mark.tipSink)
      parts.push(mark.tipSink)
    return parts.join(" · ")
  }

  // One phase for every pill, so two playing speakers pulse together.
  // Started from applyMarks. A binding on marks.length does not see a
  // replaced array, so the clock would never start.
  Timer {
    id: waveTimer
    interval: 180
    repeat: true
    running: false
    onTriggered: root.waveIndex = (root.waveIndex + 1) % 4
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
      root.syncWaveTimer()
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

      // This layer commits a buffer when a frame is scheduled. One frame per
      // phase step is enough to show the new wave.
      FrameAnimation {
        id: present
        running: false
        onTriggered: running = false
      }

      Connections {
        target: root
        function onWaveIndexChanged() {
          if (panel.visible)
            present.running = true
        }
      }

      Repeater {
        model: panel.mine

        delegate: Item {
          id: speaker
          required property var modelData
          readonly property color ink: modelData.focused ? root.activeInk : root.inactiveInk
          readonly property string tip: root.tipOf(modelData)
          width: 14
          height: 14
          x: Math.round(modelData.x + modelData.width - (modelData.iconEdge || 6) - width)
          y: Math.round(modelData.y + (modelData.height - height) / 2)

          // Always on while this pill is marked. Hover over an empty-mask
          // layer never got pointer hits, so a cursor poll was unreliable.
          Rectangle {
            id: tipBox
            visible: speaker.tip.length > 0
            color: "#070B09"
            border.color: "#FFFFFF"
            border.width: 1
            radius: 4
            anchors.right: parent.left
            anchors.rightMargin: 8
            anchors.verticalCenter: parent.verticalCenter
            width: tipLabel.implicitWidth + 12
            height: tipLabel.implicitHeight + 8
            onVisibleChanged: present.running = true

            Text {
              id: tipLabel
              anchors.centerIn: parent
              text: speaker.tip
              color: "#FFFFFF"
              font.pixelSize: 12
            }
          }

          // A single canvas repaint does not commit a new frame on this
          // layer. Each wave is its own item so a phase step changes opacity,
          // which does.
          Canvas {
            id: body
            anchors.fill: parent
            property color ink: speaker.ink
            onInkChanged: requestPaint()
            onWidthChanged: requestPaint()
            Component.onCompleted: requestPaint()
            onPaint: {
              var ctx = getContext("2d")
              ctx.clearRect(0, 0, width, height)
              ctx.fillStyle = body.ink
              var s = width / 14
              ctx.fillRect(1 * s, 5 * s, 3 * s, 4 * s)
              ctx.beginPath()
              ctx.moveTo(4 * s, 5 * s)
              ctx.lineTo(8 * s, 2 * s)
              ctx.lineTo(8 * s, 12 * s)
              ctx.closePath()
              ctx.fill()
            }
          }

          Canvas {
            id: innerWave
            anchors.fill: parent
            property color ink: speaker.ink
            opacity: root.wavePhase >= 1 ? 1 : 0
            onInkChanged: requestPaint()
            onWidthChanged: requestPaint()
            Component.onCompleted: requestPaint()
            onPaint: {
              var ctx = getContext("2d")
              ctx.clearRect(0, 0, width, height)
              ctx.strokeStyle = innerWave.ink
              ctx.lineCap = "round"
              ctx.lineWidth = Math.max(1, width / 14 * 1.25)
              var s = width / 14
              ctx.beginPath()
              ctx.arc(8.2 * s, 7 * s, 2.6 * s, -0.9, 0.9, false)
              ctx.stroke()
            }
          }

          Canvas {
            id: outerWave
            anchors.fill: parent
            property color ink: speaker.ink
            opacity: root.wavePhase >= 2 ? 1 : 0
            onInkChanged: requestPaint()
            onWidthChanged: requestPaint()
            Component.onCompleted: requestPaint()
            onPaint: {
              var ctx = getContext("2d")
              ctx.clearRect(0, 0, width, height)
              ctx.strokeStyle = outerWave.ink
              ctx.lineCap = "round"
              ctx.lineWidth = Math.max(1, width / 14 * 1.25)
              var s = width / 14
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
