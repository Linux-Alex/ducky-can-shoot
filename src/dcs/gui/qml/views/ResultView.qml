import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

import "../components"

/*!
    Step 3: what happened, in one screen.  Verdict, numbers, the share link,
    every finding, the artefacts that were written and the timeline.
*/
Controls.ScrollView {
    id: view

    signal runAgain()

    clip: true
    contentWidth: availableWidth

    readonly property var summary: backend.summary && backend.summary.verdict !== undefined
                                   ? backend.summary : null
    readonly property string verdict: summary ? summary.verdict : ""
    readonly property color verdictColour: {
        if (verdict === "clean") {
            return Kirigami.Theme.positiveTextColor;
        }
        if (verdict === "infected") {
            return Kirigami.Theme.negativeTextColor;
        }
        if (verdict === "suspicious") {
            return Kirigami.Theme.neutralTextColor;
        }
        return Kirigami.Theme.disabledTextColor;
    }

    ColumnLayout {
        width: view.availableWidth
        spacing: Kirigami.Units.largeSpacing

        Item {
            Layout.preferredHeight: Kirigami.Units.largeSpacing
        }

        Controls.Label {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            visible: view.summary === null
            wrapMode: Text.WordWrap
            opacity: 0.7
            text: "No job has finished yet. Start one from the first tab."
        }

        // ------------------------------------------------------------ verdict
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            visible: view.summary !== null
            padding: Kirigami.Units.largeSpacing

            contentItem: RowLayout {
                spacing: Kirigami.Units.largeSpacing

                Rectangle {
                    Layout.alignment: Qt.AlignTop
                    implicitWidth: Kirigami.Units.gridUnit * 4
                    implicitHeight: implicitWidth
                    radius: width / 2
                    color: Qt.rgba(view.verdictColour.r, view.verdictColour.g, view.verdictColour.b, 0.15)

                    Kirigami.Icon {
                        anchors.centerIn: parent
                        source: {
                            if (view.verdict === "clean") {
                                return "security-high";
                            }
                            if (view.verdict === "infected") {
                                return "security-low";
                            }
                            return "dialog-warning";
                        }
                        color: view.verdictColour
                        implicitWidth: Kirigami.Units.iconSizes.large
                        implicitHeight: Kirigami.Units.iconSizes.large
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing

                    Kirigami.Heading {
                        level: 1
                        Layout.fillWidth: true
                        color: view.verdictColour
                        text: {
                            if (view.verdict === "clean") {
                                return "Safe";
                            }
                            if (view.verdict === "infected") {
                                return "Malware detected";
                            }
                            if (view.verdict === "suspicious") {
                                return "Suspicious content";
                            }
                            if (view.verdict === "cancelled") {
                                return "Cancelled";
                            }
                            return "Scan failed";
                        }
                    }

                    Controls.Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: {
                            if (view.verdict === "clean") {
                                return "Nothing malicious was found on " + (view.summary ? view.summary.sourceLabel : "") + ".";
                            }
                            if (view.verdict === "infected") {
                                return (view.summary ? view.summary.threatCount : 0) + " threatening file(s) found. "
                                       + "The archive was not uploaded and no link was shared.";
                            }
                            if (view.verdict === "suspicious") {
                                return "Files that behave like attack tooling were found. Review the findings below.";
                            }
                            if (view.verdict === "cancelled") {
                                return "The run was stopped before it finished. Nothing was uploaded.";
                            }
                            return "The scan could not complete, so the medium cannot be cleared.";
                        }
                    }

                    Controls.Label {
                        Layout.fillWidth: true
                        font: Kirigami.Theme.smallFont
                        opacity: 0.7
                        text: view.summary ? ("job " + view.summary.jobId + " - finished " + view.summary.finishedAt
                                              + " - took " + view.summary.durationText) : ""
                    }
                }
            }
        }

        // ------------------------------------------------------------ link
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            visible: view.summary !== null && (view.summary.shareLink !== "" || view.summary.uploadSkippedReason !== "")
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Heading {
                    level: 4
                    text: "Share link"
                    visible: view.summary !== null && view.summary.shareLink !== ""
                }

                RowLayout {
                    Layout.fillWidth: true
                    visible: view.summary !== null && view.summary.shareLink !== ""
                    spacing: Kirigami.Units.smallSpacing

                    Controls.TextField {
                        Layout.fillWidth: true
                        readOnly: true
                        text: view.summary ? view.summary.shareLink : ""
                        font.family: "monospace"
                        onPressed: selectAll()
                    }

                    Controls.Button {
                        text: "Copy"
                        icon.name: "edit-copy"
                        onClicked: backend.copyText(view.summary.shareLink)
                    }

                    Controls.Button {
                        text: "Open"
                        icon.name: "internet-services"
                        onClicked: backend.openUrl(view.summary.shareLink)
                    }
                }

                Controls.Label {
                    Layout.fillWidth: true
                    visible: view.summary !== null && view.summary.shareLink !== ""
                    font: Kirigami.Theme.smallFont
                    opacity: 0.65
                    wrapMode: Text.WordWrap
                    text: "Anyone with this link can download the archive. The host deletes it after its "
                          + "retention period."
                }

                Kirigami.InlineMessage {
                    Layout.fillWidth: true
                    visible: view.summary !== null && view.summary.shareLink === ""
                                && view.summary.uploadSkippedReason !== ""
                    type: Kirigami.MessageType.Warning
                    text: view.summary ? view.summary.uploadSkippedReason : ""
                }
            }
        }

        // ------------------------------------------------------------ numbers
        GridLayout {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            visible: view.summary !== null
            columns: Math.max(2, Math.floor(view.availableWidth / (Kirigami.Units.gridUnit * 13)))
            columnSpacing: Kirigami.Units.largeSpacing
            rowSpacing: Kirigami.Units.largeSpacing

            StatTile {
                Layout.fillWidth: true
                label: "Files scanned"
                value: view.summary ? String(view.summary.filesScanned) : "-"
                iconName: "document-multiple"
            }

            StatTile {
                Layout.fillWidth: true
                label: "Data scanned"
                value: view.summary ? view.summary.bytesScannedText : "-"
                iconName: "drive-harddisk"
            }

            StatTile {
                Layout.fillWidth: true
                label: "Findings"
                value: view.summary ? String(view.summary.findingCount) : "-"
                hint: view.summary ? (view.summary.threatCount + " threatening") : ""
                iconName: "security-low"
            }

            StatTile {
                Layout.fillWidth: true
                label: "Archive"
                value: view.summary && view.summary.archiveBytes > 0 ? view.summary.archiveBytesText : "not created"
                iconName: "package-x-generic"
            }
        }

        // ------------------------------------------------------------ artifacts
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            visible: view.summary !== null
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Heading {
                    level: 4
                    text: "Artefacts"
                }

                GridLayout {
                    Layout.fillWidth: true
                    columns: 2
                    columnSpacing: Kirigami.Units.largeSpacing
                    rowSpacing: Kirigami.Units.smallSpacing

                    Controls.Button {
                        text: "Open report"
                        icon.name: "text-html"
                        enabled: !!(view.summary && view.summary.reports && view.summary.reports.html !== "")
                        onClicked: backend.openUrl("file://" + view.summary.reports.html)
                    }

                    Controls.Label {
                        Layout.fillWidth: true
                        font: Kirigami.Theme.smallFont
                        opacity: 0.7
                        elide: Text.ElideMiddle
                        text: view.summary && view.summary.reports ? (view.summary.reports.html || "") : ""
                    }

                    Controls.Button {
                        text: "Open report folder"
                        icon.name: "folder-open"
                        enabled: !!(view.summary && view.summary.reports && view.summary.reports.html !== "")
                        onClicked: backend.openPath(view.summary.reports.html)
                    }

                    Controls.Button {
                        text: "Open archive folder"
                        icon.name: "folder-open"
                        enabled: !!(view.summary && view.summary.archive && view.summary.archive.primaryPath !== "")
                        onClicked: backend.openPath(view.summary.archive.primaryPath)
                    }
                }

                Controls.Label {
                    Layout.fillWidth: true
                    visible: !!(view.summary && view.summary.archiveSha256 !== "")
                    font.family: "monospace"
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    wrapMode: Text.WrapAnywhere
                    opacity: 0.7
                    text: view.summary ? ("archive sha256: " + view.summary.archiveSha256) : ""
                }
            }
        }

        // ------------------------------------------------------------ findings
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            visible: view.summary !== null
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Heading {
                    level: 4
                    Layout.fillWidth: true
                    text: "Findings (" + backend.findings.count + ")"
                }

                Controls.Label {
                    Layout.fillWidth: true
                    visible: backend.findings.count === 0
                    opacity: 0.65
                    text: "Nothing suspicious was found."
                }

                ListView {
                    id: resultFindings
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(contentHeight, Kirigami.Units.gridUnit * 18)
                    visible: backend.findings.count > 0
                    clip: true
                    model: backend.findings
                    spacing: 6

                    delegate: ColumnLayout {
                        id: findingCard
                        required property string severity
                        required property string threat
                        required property string path
                        required property string detail
                        required property string engine
                        required property string category
                        required property string sha256

                        width: resultFindings.width
                        spacing: 2

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Kirigami.Units.smallSpacing

                            Rectangle {
                                implicitWidth: Kirigami.Units.gridUnit * 0.6
                                implicitHeight: implicitWidth
                                radius: width / 2
                                Layout.alignment: Qt.AlignVCenter
                                color: {
                                    if (findingCard.severity === "critical" || findingCard.severity === "high") {
                                        return Kirigami.Theme.negativeTextColor;
                                    }
                                    if (findingCard.severity === "medium") {
                                        return Kirigami.Theme.neutralTextColor;
                                    }
                                    return Kirigami.Theme.disabledTextColor;
                                }
                            }

                            Controls.Label {
                                text: findingCard.threat
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }

                            Controls.Label {
                                text: findingCard.severity.toUpperCase() + "  " + findingCard.engine
                                font: Kirigami.Theme.smallFont
                                opacity: 0.7
                            }
                        }

                        Controls.Label {
                            Layout.fillWidth: true
                            text: findingCard.path
                            font.family: "monospace"
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                            opacity: 0.8
                            wrapMode: Text.WrapAnywhere
                        }

                        Controls.Label {
                            Layout.fillWidth: true
                            visible: findingCard.detail !== ""
                            text: findingCard.detail
                            font: Kirigami.Theme.smallFont
                            opacity: 0.65
                            wrapMode: Text.WordWrap
                        }

                        Kirigami.Separator {
                            Layout.fillWidth: true
                        }
                    }
                }
            }
        }

        // ------------------------------------------------------------ timeline + notes
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.bottomMargin: Kirigami.Units.gridUnit
            visible: view.summary !== null
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Heading {
                    level: 4
                    text: "Timeline"
                }

                Repeater {
                    model: view.summary ? view.summary.timings : []

                    delegate: RowLayout {
                        id: timingRow
                        required property var modelData

                        Layout.fillWidth: true
                        spacing: Kirigami.Units.largeSpacing

                        Controls.Label {
                            text: timingRow.modelData.stage
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 9
                            opacity: 0.85
                        }

                        Controls.Label {
                            text: timingRow.modelData.state
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 6
                            font: Kirigami.Theme.smallFont
                            opacity: 0.75
                            color: timingRow.modelData.state === "failed" ? Kirigami.Theme.negativeTextColor
                                                                          : Kirigami.Theme.textColor
                        }

                        Controls.Label {
                            Layout.fillWidth: true
                            text: timingRow.modelData.message
                            font: Kirigami.Theme.smallFont
                            elide: Text.ElideRight
                            opacity: 0.7
                        }

                        Controls.Label {
                            text: timingRow.modelData.secondsText
                            font: Kirigami.Theme.smallFont
                            opacity: 0.75
                        }
                    }
                }

                Controls.Label {
                    Layout.fillWidth: true
                    visible: !!(view.summary && view.summary.warnings && view.summary.warnings.length > 0)
                    text: "Notes"
                    font.weight: Font.DemiBold
                    topPadding: Kirigami.Units.smallSpacing
                }

                Repeater {
                    model: view.summary && view.summary.warnings ? view.summary.warnings : []

                    delegate: Controls.Label {
                        required property var modelData

                        Layout.fillWidth: true
                        text: "- " + modelData
                        font: Kirigami.Theme.smallFont
                        opacity: 0.75
                        wrapMode: Text.WordWrap
                    }
                }

                Controls.Button {
                    Layout.alignment: Qt.AlignRight
                    text: "Run another job"
                    icon.name: "document-new"
                    onClicked: view.runAgain()
                }
            }
        }
    }
}
