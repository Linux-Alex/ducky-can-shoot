import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

/*! A single headline number with a caption. */
Kirigami.Card {
    id: tile

    property string label: ""
    property string value: "-"
    property string hint: ""
    property string iconName: ""
    property color accent: Kirigami.Theme.highlightColor

    implicitWidth: Kirigami.Units.gridUnit * 11
    implicitHeight: Kirigami.Units.gridUnit * 4.8
    padding: Kirigami.Units.largeSpacing

    contentItem: RowLayout {
        spacing: Kirigami.Units.largeSpacing

        Rectangle {
            visible: tile.iconName !== ""
            Layout.alignment: Qt.AlignTop
            implicitWidth: Kirigami.Units.gridUnit * 2.2
            implicitHeight: Kirigami.Units.gridUnit * 2.2
            radius: width / 2
            color: Qt.rgba(tile.accent.r, tile.accent.g, tile.accent.b, 0.14)

            Kirigami.Icon {
                anchors.centerIn: parent
                source: tile.iconName
                implicitWidth: Kirigami.Units.iconSizes.smallMedium
                implicitHeight: Kirigami.Units.iconSizes.smallMedium
                color: tile.accent
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 0

            Controls.Label {
                text: tile.label
                font: Kirigami.Theme.smallFont
                opacity: 0.75
                elide: Text.ElideRight
                Layout.fillWidth: true
            }

            Controls.Label {
                text: tile.value
                font.pointSize: Kirigami.Theme.defaultFont.pointSize * 1.6
                font.weight: Font.DemiBold
                elide: Text.ElideRight
                Layout.fillWidth: true

                Behavior on text {
                    SequentialAnimation {
                        NumberAnimation {
                            target: parent
                            property: "opacity"
                            to: 0.35
                            duration: 70
                        }
                        NumberAnimation {
                            target: parent
                            property: "opacity"
                            to: 1.0
                            duration: 150
                        }
                    }
                }
            }

            Controls.Label {
                visible: tile.hint !== ""
                text: tile.hint
                font: Kirigami.Theme.smallFont
                opacity: 0.6
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
        }
    }
}
