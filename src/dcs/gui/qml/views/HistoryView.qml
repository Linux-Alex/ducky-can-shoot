import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

/*!
    Finished jobs: the local index from dcs.core.history.

    The page is a list, not a second report viewer: opening a row hands the
    stored HTML report to the desktop, which is the same file the report page
    shows during the run.  Nothing here leaves the machine - the index names
    the files of a medium, so it stays local by design.
*/
Controls.ScrollView {
    id: view

    clip: true
    contentWidth: availableWidth

    property string pendingDelete: ""

    function verdictColour(name) {
        if (name === "clean") {
            return Kirigami.Theme.positiveTextColor;
        }
        if (name === "infected") {
            return Kirigami.Theme.negativeTextColor;
        }
        if (name === "suspicious") {
            return Kirigami.Theme.neutralTextColor;
        }
        return Kirigami.Theme.disabledTextColor;
    }

    function verdictLabel(name) {
        if (name === "clean") {
            return "Safe";
        }
        if (name === "infected") {
            return "Infected";
        }
        if (name === "suspicious") {
            return "Suspicious";
        }
        if (name === "cancelled") {
            return "Cancelled";
        }
        if (name === "error") {
            return "Scan failed";
        }
        return name === "" ? "-" : name;
    }

    ColumnLayout {
        width: view.availableWidth
        spacing: Kirigami.Units.largeSpacing

        Item {
            Layout.preferredHeight: Kirigami.Units.largeSpacing
        }

        Kirigami.Heading {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            level: 1
            text: "Job history"
        }

        Controls.Label {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            opacity: 0.8
            text: "Every finished job is indexed on this machine: when it ran, which medium, "
                  + "the verdict, the numbers and where its report was written. Nothing in this "
                  + "list is uploaded or looked up online."
        }

        // ------------------------------------------------------------ toolbar
        RowLayout {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing

            Controls.TextField {
                id: searchField
                objectName: "historySearch"
                Layout.fillWidth: true
                placeholderText: "Search a label, a verdict, a file name or a hash"
                onTextEdited: backend.refreshHistory(text)
                onAccepted: backend.refreshHistory(text)
            }

            Controls.Button {
                text: "Search"
                icon.name: "search"
                onClicked: backend.refreshHistory(searchField.text)
            }

            Controls.Button {
                objectName: "historyClearButton"
                text: "Clear History…"
                icon.name: "edit-clear-history"
                enabled: backend.historyCount > 0
                onClicked: clearDialog.open()
            }
        }

        Kirigami.InlineMessage {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            visible: backend.historyError !== ""
            type: Kirigami.MessageType.Error
            text: backend.historyError
        }

        // ------------------------------------------------------------ the list
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            visible: backend.history.count > 0
            padding: 0

            contentItem: ColumnLayout {
                spacing: 0

                Repeater {
                    model: backend.history

                    delegate: Controls.ItemDelegate {
                        id: jobRow
                        required property int index
                        required property string jobId
                        required property string startedText
                        required property string label
                        required property string verdict
                        required property string sourcePath
                        required property string filesText
                        required property string findingsText
                        required property string threatsText
                        required property string archiveText
                        required property bool encrypted
                        required property string reportHtml
                        required property string durationText
                        required property string uploadProvider

                        Layout.fillWidth: true

                        contentItem: RowLayout {
                            spacing: Kirigami.Units.largeSpacing

                            Rectangle {
                                implicitWidth: Kirigami.Units.gridUnit * 0.6
                                implicitHeight: parent.height
                                radius: width / 2
                                color: view.verdictColour(jobRow.verdict)
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 0

                                Controls.Label {
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    font.weight: Font.DemiBold
                                    text: jobRow.label || jobRow.sourcePath
                                }

                                Controls.Label {
                                    Layout.fillWidth: true
                                    font: Kirigami.Theme.smallFont
                                    opacity: 0.75
                                    elide: Text.ElideRight
                                    text: {
                                        let parts = [jobRow.startedText, view.verdictLabel(jobRow.verdict)];
                                        parts.push(jobRow.filesText + " files");
                                        parts.push(jobRow.findingsText + " findings");
                                        if (jobRow.threatsText !== "0") {
                                            parts.push(jobRow.threatsText + " threatening");
                                        }
                                        parts.push(jobRow.archiveText);
                                        if (jobRow.encrypted) {
                                            parts.push("encrypted");
                                        }
                                        if (jobRow.uploadProvider) {
                                            parts.push(jobRow.uploadProvider);
                                        }
                                        parts.push("job " + jobRow.jobId);
                                        return parts.join(" · ");
                                    }
                                }
                            }

                            Controls.Button {
                                text: "Open Report"
                                icon.name: "text-html"
                                enabled: jobRow.reportHtml !== ""
                                onClicked: backend.openHistoryJob(jobRow.jobId)
                            }

                            Controls.Button {
                                objectName: "historyDeleteButton"
                                text: "Delete"
                                icon.name: "edit-delete"
                                onClicked: {
                                    view.pendingDelete = jobRow.jobId;
                                    deleteDialog.open();
                                }
                            }
                        }
                    }
                }
            }
        }

        Controls.Label {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            visible: backend.history.count === 0
            opacity: 0.7
            text: backend.historyCount === 0
                ? "No jobs yet. A job appears here as soon as one finishes; the New job page starts one."
                : "No job matches this search."
        }

        // ------------------------------------------------------------ footer
        Controls.Label {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.bottomMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            font: Kirigami.Theme.smallFont
            opacity: 0.6
            text: {
                let text = backend.historyCount + " job(s) in " + backend.historyDatabase;
                text += " · retention: " + (backend.settings.history.max_jobs > 0
                    ? backend.settings.history.max_jobs + " job(s)" : "no limit");
                text += ", " + (backend.settings.history.max_age_days > 0
                    ? backend.settings.history.max_age_days + " day(s)" : "no age limit");
                text += " · change it in Settings.";
                return text;
            }
        }
    }

    Kirigami.PromptDialog {
        id: deleteDialog
        title: "Remove this job from the history?"
        subtitle: "The index entry and its finding rows are deleted. The report files on disk are "
                  + "kept, so nothing you have already read disappears."
        customFooterActions: [
            Kirigami.Action {
                text: "Cancel"
                icon.name: "dialog-cancel"
                onTriggered: {
                    view.pendingDelete = "";
                    deleteDialog.close();
                }
            },
            Kirigami.Action {
                text: "Remove from History"
                icon.name: "edit-delete"
                onTriggered: {
                    backend.deleteHistoryJob(view.pendingDelete);
                    view.pendingDelete = "";
                    deleteDialog.close();
                }
            }
        ]
    }

    Kirigami.PromptDialog {
        id: clearDialog
        title: "Clear the whole job history?"
        subtitle: "This deletes all " + backend.historyCount + " index entries. The report files "
                  + "stay on disk; only the index goes away. It cannot be undone."
        customFooterActions: [
            Kirigami.Action {
                text: "Cancel"
                icon.name: "dialog-cancel"
                onTriggered: clearDialog.close()
            },
            Kirigami.Action {
                objectName: "clearHistoryAction"
                text: "Clear the History"
                icon.name: "edit-clear-history"
                onTriggered: {
                    backend.clearHistory();
                    searchField.text = "";
                    clearDialog.close();
                }
            }
        ]
    }
}
