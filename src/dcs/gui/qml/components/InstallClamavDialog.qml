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

    While it runs, the output is a ListView that follows the last line: apt
    prints hundreds of lines and the interesting ones are always at the bottom.
    Scrolling up is still possible (the view stops following as soon as the user
    scrolls, and resumes when they return to the end).
*/
Kirigami.Dialog {
    id: dialog

    objectName: "clamavInstallDialog"
    title: "Install ClamAV"
    preferredWidth: Kirigami.Units.gridUnit * 42
    preferredHeight: Kirigami.Units.gridUnit * 32
    padding: Kirigami.Units.largeSpacing
    showCloseButton: true

    property var plan: ({})
    property bool hasPlan: false
    //: true while the user is reading history instead of the tail
    readonly property bool following: outputList.atYEnd

    readonly property bool running: backend.installRunning
    readonly property var outcome: backend.installOutcome
    readonly property bool finished: backend.installState === "done" || backend.installState === "failed"
    readonly property bool alreadyAvailable: plan.status !== undefined
                                             && plan.status !== null
                                             && plan.status.available === true

    function refresh() {
        plan = backend.clamavInstallPlan();
        hasPlan = true;
    }

    onOpened: refresh()

    ColumnLayout {
        spacing: Kirigami.Units.largeSpacing

        Controls.Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: "ClamAV adds signature based detection on top of the built-in heuristics. "
                  + "Installing it needs administrator rights; Ducky Can Shoot never sees your password."
        }

        // ---------------------------------------------------------- summary
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
            visible: dialog.alreadyAvailable && !dialog.running && !dialog.finished
            type: Kirigami.MessageType.Positive
            text: "ClamAV is already available here: " + ((dialog.plan.status && dialog.plan.status.line) || "")
                  + "  The plan below would reinstall or update it."
        }

        // ---------------------------------------------------------- steps
        Controls.Label {
            Layout.fillWidth: true
            visible: dialog.plan.possible === true
            text: "These commands will run, in this order:"
            font.weight: Font.DemiBold
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing / 2
            visible: dialog.plan.possible === true && !dialog.running && !dialog.finished

            Repeater {
                model: dialog.plan.steps !== undefined ? dialog.plan.steps : []

                delegate: RowLayout {
                    id: stepRow
                    required property var modelData

                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing

                    Controls.Label {
                        Layout.alignment: Qt.AlignTop
                        text: stepRow.modelData.optional ? "optional" : "required"
                        font: Kirigami.Theme.smallFont
                        opacity: 0.6
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 4.5
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0

                        Controls.Label {
                            Layout.fillWidth: true
                            text: stepRow.modelData.title
                            elide: Text.ElideRight
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
                visible: !dialog.running
                text: "- " + modelData
                font: Kirigami.Theme.smallFont
                opacity: 0.7
                wrapMode: Text.WordWrap
            }
        }

        // ---------------------------------------------------------- output
        RowLayout {
            Layout.fillWidth: true
            visible: dialog.running || dialog.finished

            DcsProgressBar {
                Layout.fillWidth: true
                indeterminate: dialog.running
                value: dialog.finished ? 1 : 0
            }

            Controls.Label {
                text: dialog.running ? "Installing..." : (dialog.outcome.ok === true ? "Finished" : "Did not finish")
                font: Kirigami.Theme.smallFont
            }
        }

        Controls.Label {
            Layout.fillWidth: true
            visible: (dialog.running || dialog.finished) && !dialog.following
            text: "You are reading the log from above; scroll to the end to follow the installation again."
            font: Kirigami.Theme.smallFont
            opacity: 0.7
            wrapMode: Text.WordWrap
        }

        Controls.ScrollView {
            id: outputScroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: Kirigami.Units.gridUnit * 9
            visible: dialog.running || dialog.finished
            clip: true
            Controls.ScrollBar.vertical.policy: Controls.ScrollBar.AsNeeded
            Controls.ScrollBar.horizontal.policy: Controls.ScrollBar.AlwaysOff

            ListView {
                id: outputList
                model: backend.installOutput

                // follow the tail: every new line scrolls the view down, which is
                // what somebody watching apt wants, and scrolling up pauses it
                onCountChanged: {
                    if (following) {
                        positionViewAtEnd();
                    }
                }

                delegate: Controls.Label {
                    id: outputLine
                    required property string line

                    width: outputList.width
                    text: outputLine.line
                    font.family: "monospace"
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    color: outputLine.line.startsWith("#") ? Kirigami.Theme.disabledTextColor
                           : Kirigami.Theme.textColor
                    opacity: outputLine.line.startsWith("#") ? 0.8 : 1.0
                    wrapMode: Text.WrapAnywhere
                    textFormat: Text.PlainText
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
    }

    customFooterActions: [
        Kirigami.Action {
            text: "Open the script"
            icon.name: "text-x-script"
            visible: dialog.plan.scriptPath !== undefined && dialog.plan.scriptPath !== ""
            onTriggered: backend.openPath(dialog.plan.scriptPath)
        },
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
                  : (dialog.alreadyAvailable ? "Run the plan anyway" : "Install ClamAV")
            icon.name: dialog.running ? "process-stop" : "install"
            visible: dialog.plan.possible === true
            enabled: dialog.plan.possible === true && !dialog.running
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
