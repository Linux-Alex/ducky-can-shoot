import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

import "../components"

/*!
    Step 2: the live view.

    Everything here updates while the job runs: overall progress, the seven
    pipeline stages, counters, throughput, findings as they are discovered and
    the log tail.
*/
Controls.ScrollView {
    id: view

    signal jobFinished()

    clip: true
    contentWidth: availableWidth

    property bool logVisible: true

    ColumnLayout {
        width: view.availableWidth
        spacing: Kirigami.Units.largeSpacing

        Item {
            Layout.preferredHeight: Kirigami.Units.largeSpacing
        }

        // ------------------------------------------------------------ headline
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                RowLayout {
                    Layout.fillWidth: true

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0

                        Kirigami.Heading {
                            level: 2
                            Layout.fillWidth: true
                            elide: Text.ElideRight
                            text: {
                                if (backend.state === "running") {
                                    return backend.liveStageTitle;
                                }
                                if (backend.state === "finished") {
                                    return "Finished - " + (backend.summary.verdict || "");
                                }
                                if (backend.state === "failed") {
                                    return "The job failed";
                                }
                                return "No job is running";
                            }
                        }

                        Controls.Label {
                            Layout.fillWidth: true
                            opacity: 0.75
                            elide: Text.ElideMiddle
                            text: backend.sourcePath || "Start a job from the New job tab."
                        }
                    }

                    Controls.Label {
                        text: Math.round(backend.overallPercent * 100) + " %"
                        font.pointSize: Kirigami.Theme.defaultFont.pointSize * 1.8
                        font.weight: Font.DemiBold
                        color: Kirigami.Theme.highlightColor
                    }

                    Controls.Button {
                        visible: backend.state === "running"
                        text: "Cancel"
                        icon.name: "process-stop"
                        onClicked: backend.cancelJob()
                    }
                }

                Controls.ProgressBar {
                    Layout.fillWidth: true
                    from: 0
                    to: 1
                    value: backend.overallPercent

                    Behavior on value {
                        NumberAnimation {
                            duration: 160
                            easing.type: Easing.OutCubic
                        }
                    }
                }

                Controls.Label {
                    Layout.fillWidth: true
                    visible: backend.liveMessage !== ""
                    text: backend.liveMessage
                    font: Kirigami.Theme.smallFont
                    opacity: 0.75
                    elide: Text.ElideRight
                }
            }
        }

        // ------------------------------------------------------------ live numbers
        GridLayout {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            columns: Math.max(2, Math.floor(view.availableWidth / (Kirigami.Units.gridUnit * 13)))
            columnSpacing: Kirigami.Units.largeSpacing
            rowSpacing: Kirigami.Units.largeSpacing

            StatTile {
                Layout.fillWidth: true
                label: "Files inspected"
                value: backend.liveFilesText
                iconName: "document-multiple"
            }

            StatTile {
                Layout.fillWidth: true
                label: "Data inspected"
                value: backend.liveBytesText
                iconName: "drive-harddisk"
            }

            StatTile {
                Layout.fillWidth: true
                label: "Findings"
                value: String(backend.findings.count)
                iconName: backend.findings.count > 0 ? "security-low" : "security-high"
                accent: backend.findings.count > 0 ? Kirigami.Theme.neutralTextColor
                                                   : Kirigami.Theme.positiveTextColor
            }

            StatTile {
                Layout.fillWidth: true
                label: "Rate"
                value: backend.liveRateText
                hint: backend.liveEtaText !== "--:--" ? ("ETA " + backend.liveEtaText) : ""
                iconName: "speedometer"
            }
        }

        // ------------------------------------------------------------ current file
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            padding: Kirigami.Units.largeSpacing

            contentItem: RowLayout {
                spacing: Kirigami.Units.largeSpacing

                Kirigami.Icon {
                    source: backend.state === "running" ? "document-open" : "dialog-information"
                    implicitWidth: Kirigami.Units.iconSizes.smallMedium
                    implicitHeight: Kirigami.Units.iconSizes.smallMedium
                    opacity: 0.7
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0

                    Controls.Label {
                        Layout.fillWidth: true
                        font: Kirigami.Theme.smallFont
                        opacity: 0.65
                        text: "Current file"
                    }

                    Controls.Label {
                        Layout.fillWidth: true
                        elide: Text.ElideMiddle
                        text: backend.liveCurrent !== "" ? backend.liveCurrent : "-"
                        font.family: "monospace"
                    }
                }

                ColumnLayout {
                    spacing: 0

                    Controls.Label {
                        font: Kirigami.Theme.smallFont
                        opacity: 0.65
                        text: "Elapsed"
                        Layout.alignment: Qt.AlignRight
                    }

                    Controls.Label {
                        text: backend.liveElapsedText
                        font.weight: Font.DemiBold
                        Layout.alignment: Qt.AlignRight
                    }
                }
            }
        }

        // ------------------------------------------------------------ throughput
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                RowLayout {
                    Layout.fillWidth: true

                    Kirigami.Heading {
                        level: 4
                        text: "Throughput"
                        Layout.fillWidth: true
                    }

                    Controls.Label {
                        font: Kirigami.Theme.smallFont
                        opacity: 0.65
                        text: backend.throughput.rowCount() + " samples"
                    }
                }

                Sparkline {
                    Layout.fillWidth: true
                    model: backend.throughput
                }
            }
        }

        // ------------------------------------------------------------ stages
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Heading {
                    level: 4
                    text: "Pipeline"
                }

                Repeater {
                    model: backend.stages

                    delegate: StageDelegate {
                        width: parent ? parent.width : implicitWidth
                    }
                }
            }
        }

        // ------------------------------------------------------------ findings
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Heading {
                    level: 4
                    Layout.fillWidth: true
                    text: "Findings (" + backend.findings.count + ")"
                }

                Controls.Label {
                    visible: backend.findings.count === 0
                    opacity: 0.6
                    text: backend.state === "running" ? "Nothing suspicious so far..." : "No findings."
                }

                ListView {
                    id: findingsList
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(contentHeight, Kirigami.Units.gridUnit * 14)
                    visible: backend.findings.count > 0
                    clip: true
                    model: backend.findings
                    spacing: 4

                    delegate: RowLayout {
                        id: findingRow
                        required property string severity
                        required property string threat
                        required property string path
                        required property string detail
                        required property string engine

                        width: findingsList.width
                        spacing: Kirigami.Units.smallSpacing

                        Rectangle {
                            Layout.alignment: Qt.AlignTop
                            Layout.topMargin: 4
                            implicitWidth: Kirigami.Units.gridUnit * 0.5
                            implicitHeight: implicitWidth
                            radius: width / 2
                            color: {
                                if (findingRow.severity === "critical" || findingRow.severity === "high") {
                                    return Kirigami.Theme.negativeTextColor;
                                }
                                if (findingRow.severity === "medium") {
                                    return Kirigami.Theme.neutralTextColor;
                                }
                                return Kirigami.Theme.disabledTextColor;
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing

                                Controls.Label {
                                    text: findingRow.threat
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }

                                Controls.Label {
                                    text: findingRow.severity.toUpperCase()
                                    font: Kirigami.Theme.smallFont
                                    opacity: 0.7
                                }
                            }

                            Controls.Label {
                                Layout.fillWidth: true
                                text: findingRow.path
                                font: Kirigami.Theme.smallFont
                                opacity: 0.75
                                elide: Text.ElideMiddle
                            }

                            Controls.Label {
                                Layout.fillWidth: true
                                visible: findingRow.detail !== ""
                                text: findingRow.detail
                                font: Kirigami.Theme.smallFont
                                opacity: 0.6
                                wrapMode: Text.WordWrap
                                maximumLineCount: 2
                                elide: Text.ElideRight
                            }
                        }
                    }
                }
            }
        }

        // ------------------------------------------------------------ log
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.bottomMargin: Kirigami.Units.gridUnit
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                RowLayout {
                    Layout.fillWidth: true

                    Kirigami.Heading {
                        level: 4
                        text: "Log"
                        Layout.fillWidth: true
                    }

                    Controls.Button {
                        text: view.logVisible ? "Hide" : "Show"
                        icon.name: view.logVisible ? "go-up" : "go-down"
                        onClicked: view.logVisible = !view.logVisible
                    }

                    Controls.Button {
                        text: "Copy"
                        icon.name: "edit-copy"
                        onClicked: backend.copyText(backend.logs.as_text())
                    }
                }

                ListView {
                    id: logArea
                    Layout.fillWidth: true
                    Layout.preferredHeight: Kirigami.Units.gridUnit * 12
                    visible: view.logVisible
                    clip: true
                    model: backend.logs
                    spacing: 0

                    delegate: Controls.Label {
                        id: logLine
                        required property string time
                        required property string level
                        required property string message

                        width: logArea.width
                        font.family: "monospace"
                        font.pointSize: Kirigami.Theme.smallFont.pointSize
                        wrapMode: Text.WrapAnywhere
                        color: logLine.level === "ERROR" ? Kirigami.Theme.negativeTextColor
                               : logLine.level === "WARNING" ? Kirigami.Theme.neutralTextColor
                               : Kirigami.Theme.textColor
                        opacity: logLine.level === "DEBUG" ? 0.5 : 1.0
                        text: logLine.time + "  " + logLine.level + "  " + logLine.message
                    }

                    onCountChanged: positionViewAtEnd()
                }
            }
        }
    }

    Component.onCompleted: logVisible = backend.settings.gui.show_log_pane

    Connections {
        target: backend
        function onStateChanged() {
            if (backend.state === "finished") {
                view.jobFinished();
            }
        }
    }
}
