import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

/*!
    Installs the optional ClamAV engine.

    Ducky Can Shoot never asks for a password: the plan is handed to pkexec
    (a graphical polkit dialog) or to a terminal with sudo inside, and only the
    output of those is streamed back here.  Everything that will run is shown
    first, and can be copied instead of executed.
*/
Kirigami.Dialog {
    id: dialog

    objectName: "clamavInstallDialog"
    title: "Install ClamAV"
    preferredWidth: Kirigami.Units.gridUnit * 40
    preferredHeight: Kirigami.Units.gridUnit * 30
    padding: Kirigami.Units.largeSpacing
    showCloseButton: true

    property var plan: ({})
    property bool hasPlan: false

    readonly property bool running: backend.installRunning
    readonly property var outcome: backend.installOutcome
    readonly property bool finished: backend.installState === "done" || backend.installState === "failed"

    function refresh() {
        plan = backend.clamavInstallPlan();
        hasPlan = true;
    }

    onOpened: refresh()

    Connections {
        target: backend
        function onInstallStateChanged() {
            if (backend.installState === "running" && !dialog.running) {
                dialog.refresh();
            }
        }
    }

    ColumnLayout {
        spacing: Kirigami.Units.largeSpacing

        Controls.Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: "ClamAV adds signature based detection on top of the built-in heuristics. "
                  + "Installing it needs administrator rights; Ducky Can Shoot never sees your password."
        }

        // ---------------------------------------------------------- summary
        Kirigami.Separator {
            Layout.fillWidth: true
        }

        GridLayout {
            Layout.fillWidth: true
            columns: 2
            columnSpacing: Kirigami.Units.largeSpacing
            rowSpacing: Kirigami.Units.smallSpacing

            Controls.Label {
                text: "Distribution"
                font: Kirigami.Theme.smallFont
                opacity: 0.7
            }
            Controls.Label {
                Layout.fillWidth: true
                text: dialog.plan.distro || "-"
                elide: Text.ElideRight
            }

            Controls.Label {
                text: "Package manager"
                font: Kirigami.Theme.smallFont
                opacity: 0.7
            }
            Controls.Label {
                Layout.fillWidth: true
                text: dialog.plan.managerLabel || "not recognised"
                elide: Text.ElideRight
            }

            Controls.Label {
                text: "Privileges"
                font: Kirigami.Theme.smallFont
                opacity: 0.7
            }
            Controls.Label {
                Layout.fillWidth: true
                text: dialog.plan.methodLabel || "-"
                wrapMode: Text.WordWrap
            }

            Controls.Label {
                visible: dialog.plan.scriptPath !== undefined && dialog.plan.scriptPath !== ""
                text: "Script"
                font: Kirigami.Theme.smallFont
                opacity: 0.7
            }
            Controls.Label {
                Layout.fillWidth: true
                visible: dialog.plan.scriptPath !== undefined && dialog.plan.scriptPath !== ""
                text: dialog.plan.scriptPath || ""
                font: Kirigami.Theme.smallFont
                elide: Text.ElideMiddle
                opacity: 0.7
            }
        }

        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: dialog.plan.blocker !== undefined && dialog.plan.blocker !== ""
            type: Kirigami.MessageType.Error
            text: dialog.plan.blocker || ""
        }

        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: dialog.plan.status !== undefined && dialog.plan.status.available === true
            type: Kirigami.MessageType.Positive
            text: "ClamAV is already available: " + ((dialog.plan.status && dialog.plan.status.line) || "")
        }

        // ---------------------------------------------------------- steps
        Controls.Label {
            Layout.fillWidth: true
            visible: !dialog.running && !dialog.finished
            text: "These commands will run, in this order:"
            font.weight: Font.DemiBold
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing / 2
            visible: !dialog.running && !dialog.finished

            Repeater {
                model: dialog.plan.steps !== undefined ? dialog.plan.steps : []

                delegate: RowLayout {
                    id: stepRow
                    required property var modelData

                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing

                    Controls.Label {
                        Layout.alignment: Qt.AlignTop
                        text: (stepRow.modelData.optional ? "optional" : "required")
                        font: Kirigami.Theme.smallFont
                        opacity: 0.6
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 4.5
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0

                        Controls.Label {
                            text: stepRow.modelData.title
                        }

                        Controls.Label {
                            Layout.fillWidth: true
                            text: stepRow.modelData.command
                            font.family: "monospace"
                            font.pointSize: Kirigami.Theme.smallFont.pointSize
                            opacity: 0.75
                            wrapMode: Text.WrapAnywhere
                        }
                    }
                }
            }
        }

        Repeater {
            model: dialog.plan.warnings !== undefined ? dialog.plan.warnings : []

            delegate: Controls.Label {
                required property var modelData

                Layout.fillWidth: true
                Layout.leftMargin: Kirigami.Units.smallSpacing
                text: "- " + modelData
                font: Kirigami.Theme.smallFont
                opacity: 0.7
                wrapMode: Text.WordWrap
                visible: !dialog.running
            }
        }

        // ---------------------------------------------------------- output
        Kirigami.Separator {
            Layout.fillWidth: true
            visible: dialog.running || dialog.finished
        }

        RowLayout {
            Layout.fillWidth: true
            visible: dialog.running || dialog.finished

            Controls.ProgressBar {
                Layout.fillWidth: true
                indeterminate: dialog.running
                from: 0
                to: 1
                value: dialog.finished ? 1 : 0
            }

            Controls.Label {
                text: dialog.running ? "Installing..." : (dialog.outcome.ok ? "Finished" : "Did not finish")
                font: Kirigami.Theme.smallFont
            }
        }

        Controls.ScrollView {
            id: outputScroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: Kirigami.Units.gridUnit * 8
            visible: dialog.running || dialog.finished
            clip: true

            Controls.TextArea {
                id: outputArea
                readOnly: true
                text: backend.installText
                font.family: "monospace"
                font.pointSize: Kirigami.Theme.smallFont.pointSize
                wrapMode: Controls.TextArea.NoWrap
                selectByMouse: true
                background: null
            }
        }

        Connections {
            target: backend
            function onInstallTextChanged() {
                if (dialog.running || dialog.visible) {
                    outputScroll.contentItem.contentY = Math.max(0, outputArea.contentHeight - outputScroll.height);
                }
            }
        }

        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: dialog.finished && dialog.outcome.ok === true
            type: Kirigami.MessageType.Positive
            text: {
                const status = dialog.outcome.verification || {};
                if (status.available) {
                    return "ClamAV is ready: " + (status.version || "") + " (" + (status.mode || "") + ").";
                }
                return "The packages are installed. ClamAV may need a moment to load its "
                       + "signature database; use Re-check when it has finished.";
            }
        }

        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: dialog.finished && dialog.outcome.ok !== true
            type: Kirigami.MessageType.Error
            text: "The installation did not finish: " + ((dialog.outcome && dialog.outcome.error) || "unknown error")
                  + "  You can copy the commands and run them yourself."
        }

        Controls.Label {
            Layout.fillWidth: true
            visible: !dialog.running && (!dialog.plan.possible || dialog.finished)
            text: "Commands (copy and run them yourself if you prefer):"
            font: Kirigami.Theme.smallFont
            opacity: 0.75
        }

        Controls.TextArea {
            Layout.fillWidth: true
            Layout.preferredHeight: Kirigami.Units.gridUnit * 7
            visible: !dialog.running && (!dialog.plan.possible || dialog.finished)
            readOnly: true
            selectByMouse: true
            font.family: "monospace"
            font.pointSize: Kirigami.Theme.smallFont.pointSize
            wrapMode: Controls.TextArea.NoWrap
            text: dialog.plan.manual !== undefined ? dialog.plan.manual : ""
        }
    }

    customFooterActions: [
        Kirigami.Action {
            text: "Copy commands"
            icon.name: "edit-copy"
            onTriggered: backend.copyText(dialog.plan.manual || "")
        },
        Kirigami.Action {
            text: "Re-check engine"
            icon.name: "view-refresh"
            onTriggered: backend.verifyClamav()
        },
        Kirigami.Action {
            text: "Close"
            icon.name: "dialog-close"
            onTriggered: dialog.close()
        },
        Kirigami.Action {
            text: dialog.running ? "Cancel"
                  : (dialog.plan.status !== undefined && dialog.plan.status.available === true
                     ? "Run the plan anyway" : "Install ClamAV")
            icon.name: dialog.running ? "process-stop" : "install"
            visible: dialog.plan.possible === true
            enabled: dialog.plan.possible === true && (!dialog.finished || !dialog.outcome.ok)
            onTriggered: {
                if (dialog.running) {
                    backend.cancelClamavInstall();
                } else {
                    backend.startClamavInstall();
                }
            }
        }
    ]
}
