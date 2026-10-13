import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

/*!
    One pipeline stage: state icon, title, live detail line and a progress bar.

    Layout note: the root item takes its height from the content column and the
    column is anchored left/right/top only.  Anchoring the column with
    anchors.fill while also deriving implicitHeight from it is a binding loop,
    and Qt resolves it by collapsing the row, which stacks the rows on top of
    each other.
*/
Item {
    id: row

    property string stageId: ""
    property string title: ""
    property string description: ""
    property string iconName: ""
    property string state: "pending"
    property real percent: 0
    property string message: ""
    property string current: ""
    property string elapsedText: ""
    property string etaText: ""
    property string rateText: ""
    property int stageIndex: 0

    readonly property bool isActive: state === "running"
    readonly property bool isDone: state === "done"
    readonly property color stateColour: {
        if (isDone) {
            return Kirigami.Theme.positiveTextColor;
        }
        if (state === "failed") {
            return Kirigami.Theme.negativeTextColor;
        }
        if (state === "cancelled" || state === "skipped") {
            return Kirigami.Theme.neutralTextColor;
        }
        if (isActive) {
            return Kirigami.Theme.highlightColor;
        }
        return Kirigami.Theme.disabledTextColor;
    }

    implicitHeight: content.implicitHeight + Kirigami.Units.largeSpacing
    implicitWidth: content.implicitWidth

    Rectangle {
        anchors.fill: parent
        radius: Kirigami.Units.smallSpacing
        color: row.isActive ? Qt.rgba(Kirigami.Theme.highlightColor.r,
                                      Kirigami.Theme.highlightColor.g,
                                      Kirigami.Theme.highlightColor.b, 0.10)
                            : "transparent"

        Behavior on color {
            ColorAnimation {
                duration: 250
            }
        }
    }

    RowLayout {
        id: content
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.leftMargin: Kirigami.Units.smallSpacing
        anchors.rightMargin: Kirigami.Units.smallSpacing
        anchors.topMargin: Kirigami.Units.smallSpacing
        spacing: Kirigami.Units.largeSpacing

        // state marker
        Item {
            Layout.alignment: Qt.AlignTop
            implicitWidth: Kirigami.Units.gridUnit * 2.4
            implicitHeight: Kirigami.Units.gridUnit * 2.4

            Rectangle {
                anchors.fill: parent
                radius: width / 2
                color: Qt.rgba(row.stateColour.r, row.stateColour.g, row.stateColour.b, 0.16)

                Behavior on color {
                    ColorAnimation {
                        duration: 250
                    }
                }
            }

            Kirigami.Icon {
                anchors.centerIn: parent
                source: {
                    if (row.isDone) {
                        return "dialog-ok-apply";
                    }
                    if (row.state === "failed") {
                        return "dialog-error";
                    }
                    if (row.state === "cancelled") {
                        return "process-stop";
                    }
                    if (row.state === "skipped") {
                        return "dialog-cancel";
                    }
                    return row.iconName || "dialog-information";
                }
                color: row.stateColour
                implicitWidth: Kirigami.Units.iconSizes.smallMedium
                implicitHeight: Kirigami.Units.iconSizes.smallMedium
            }

            // pulse while running
            Rectangle {
                anchors.fill: parent
                radius: width / 2
                color: "transparent"
                border.color: row.stateColour
                border.width: 2
                visible: row.isActive

                SequentialAnimation on scale {
                    running: row.isActive
                    loops: Animation.Infinite
                    NumberAnimation {
                        from: 0.85
                        to: 1.25
                        duration: 900
                        easing.type: Easing.OutQuad
                    }
                    NumberAnimation {
                        from: 1.25
                        to: 0.85
                        duration: 900
                        easing.type: Easing.InQuad
                    }
                }

                SequentialAnimation on opacity {
                    running: row.isActive
                    loops: Animation.Infinite
                    NumberAnimation {
                        from: 0.9
                        to: 0.15
                        duration: 900
                    }
                    NumberAnimation {
                        from: 0.15
                        to: 0.9
                        duration: 900
                    }
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing / 2

            RowLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing

                Controls.Label {
                    text: row.title
                    font.weight: row.isActive ? Font.DemiBold : Font.Normal
                    color: row.state === "pending" ? Kirigami.Theme.disabledTextColor
                                                   : Kirigami.Theme.textColor
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }

                Controls.Label {
                    visible: row.state === "skipped"
                    text: "skipped"
                    font: Kirigami.Theme.smallFont
                    opacity: 0.6
                }

                Controls.Label {
                    visible: row.elapsedText !== "" && row.state !== "pending"
                    text: row.elapsedText
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                }
            }

            Controls.Label {
                Layout.fillWidth: true
                visible: row.message !== ""
                text: row.message
                font: Kirigami.Theme.smallFont
                opacity: 0.8
                elide: Text.ElideRight
            }

            DcsProgressBar {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing / 2
                visible: row.isActive || row.isDone
                value: row.percent
                indeterminate: row.isActive && row.percent <= 0.01
            }

            RowLayout {
                Layout.fillWidth: true
                visible: row.isActive
                spacing: Kirigami.Units.largeSpacing

                Controls.Label {
                    visible: row.current !== ""
                    Layout.fillWidth: true
                    text: row.current
                    font: Kirigami.Theme.smallFont
                    opacity: 0.65
                    elide: Text.ElideMiddle
                }

                Controls.Label {
                    visible: row.rateText !== ""
                    text: row.rateText
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                }

                Controls.Label {
                    visible: row.etaText !== ""
                    text: "ETA " + row.etaText
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                }
            }
        }
    }
}
