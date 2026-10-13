import QtQuick
import QtQuick.Controls as Controls
import org.kde.kirigami as Kirigami

/*!
    A compact bar chart of recent throughput samples.

    The bars are plain rectangles fed by the throughput model (whose rate role
    is already normalised against the peak), so drawing costs nothing at the
    10 Hz update rate of a running scan.
*/
Item {
    id: chart

    property var model: null
    property color barColor: Kirigami.Theme.highlightColor
    property real barSpacing: 1

    implicitHeight: Kirigami.Units.gridUnit * 3.5

    Rectangle {
        anchors.fill: parent
        radius: Kirigami.Units.smallSpacing
        color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g,
                       Kirigami.Theme.textColor.b, 0.05)

        Row {
            id: bars
            anchors.fill: parent
            anchors.margins: 3
            spacing: chart.barSpacing

            Repeater {
                model: chart.model

                delegate: Rectangle {
                    required property real rate

                    width: Math.max(1, (bars.width - chart.barSpacing * Math.max(0, bars.children.length - 1))
                                       / Math.max(1, bars.children.length))
                    height: Math.max(1, bars.height * Math.min(1, Math.max(0, rate)))
                    anchors.bottom: parent.bottom
                    color: chart.barColor
                    opacity: 0.85
                    radius: 1

                    Behavior on height {
                        NumberAnimation {
                            duration: 160
                            easing.type: Easing.OutCubic
                        }
                    }
                }
            }
        }
    }

    Controls.Label {
        anchors.centerIn: parent
        visible: !chart.model || chart.model.rowCount() === 0
        text: "throughput appears here while the job runs"
        font: Kirigami.Theme.smallFont
        opacity: 0.45
    }
}
