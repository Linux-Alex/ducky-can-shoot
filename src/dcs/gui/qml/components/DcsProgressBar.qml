import QtQuick
import org.kde.kirigami as Kirigami

/*!
    A progress bar drawn by DCS itself.

    QtQuick Controls' ProgressBar comes with a style implementation that the
    desktop provides; the Breeze one logs "Cannot read property 'top' of null"
    while it creates its segments, and it cannot be fixed from the outside.  A
    groove and a fill are all this interface needs, and drawing them here keeps
    the bar identical on every platform, immediately follows the Kirigami theme
    colours, and stays silent in the log.

    Set *indeterminate* for work of unknown length: the fill then sweeps back
    and forth instead of pretending to know a percentage.
*/
Item {
    id: bar

    property real from: 0
    property real to: 1
    property real value: 0
    property bool indeterminate: false
    property color barColor: Kirigami.Theme.highlightColor
    property real grooveHeight: Math.max(4, Math.round(Kirigami.Units.gridUnit * 0.4))

    implicitHeight: grooveHeight
    implicitWidth: Kirigami.Units.gridUnit * 10

    readonly property real fraction: {
        if (indeterminate) {
            return 0;
        }
        const span = to - from;
        if (!(span > 0)) {
            return 0;
        }
        return Math.max(0, Math.min(1, (value - from) / span));
    }

    Rectangle {
        id: groove
        anchors.fill: parent
        radius: height / 2
        color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g,
                       Kirigami.Theme.textColor.b, 0.14)
    }

    // determinate fill
    Rectangle {
        id: fill
        visible: !bar.indeterminate
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        height: parent.height
        width: bar.indeterminate ? 0 : Math.max(0, groove.width * bar.fraction)
        radius: height / 2
        color: bar.barColor

        Behavior on width {
            NumberAnimation {
                duration: 180
                easing.type: Easing.OutCubic
            }
        }
    }

    // indeterminate fill: a segment sweeping across the groove
    Rectangle {
        id: sweep
        visible: bar.indeterminate
        anchors.verticalCenter: parent.verticalCenter
        height: parent.height
        width: Math.max(8, groove.width * 0.28)
        radius: height / 2
        color: bar.barColor
        opacity: 0.85

        SequentialAnimation on x {
            running: bar.indeterminate && bar.visible
            loops: Animation.Infinite
            NumberAnimation {
                from: 0
                to: Math.max(0, groove.width - sweep.width)
                duration: 1100
                easing.type: Easing.InOutQuad
            }
            NumberAnimation {
                from: Math.max(0, groove.width - sweep.width)
                to: 0
                duration: 1100
                easing.type: Easing.InOutQuad
            }
        }
    }

    // a zero length bar still shows its groove, which reads as "not started"
    Rectangle {
        anchors.fill: parent
        radius: height / 2
        color: "transparent"
        border.width: 1
        border.color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g,
                              Kirigami.Theme.textColor.b, 0.08)
    }
}
