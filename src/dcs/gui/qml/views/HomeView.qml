import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import org.kde.kirigami as Kirigami

import "../components"

/*!
    Step 1: pick the medium, decide what the job should do, start it.
*/
Controls.ScrollView {
    id: view

    signal jobStarted()
    signal installRequested()

    clip: true
    contentWidth: availableWidth

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
            text: "Triage a removable medium"
        }

        Controls.Label {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            opacity: 0.8
            text: "Choose a USB stick, scan every file for malware, archive the stick into one image, "
                  + "upload it to a free host and email the verdict. If anything malicious is found, "
                  + "nothing is archived or uploaded."
        }

        // ---------------------------------------------------------------- devices
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.largeSpacing

                RowLayout {
                    Layout.fillWidth: true

                    Kirigami.Heading {
                        level: 3
                        text: "Removable media"
                        Layout.fillWidth: true
                    }

                    Controls.Button {
                        text: "Refresh"
                        icon.name: "view-refresh"
                        onClicked: backend.refreshDevices()
                    }

                    Controls.Button {
                        text: "Choose a folder..."
                        icon.name: "folder-open"
                        onClicked: folderDialog.open()
                    }
                }

                Controls.Label {
                    Layout.fillWidth: true
                    visible: backend.devices.count === 0
                    wrapMode: Text.WordWrap
                    opacity: 0.7
                    text: "No removable medium detected. Plug in a USB stick and press Refresh, "
                          + "or triage a folder that is already mounted."
                }

                Repeater {
                    model: backend.devices

                    delegate: Controls.ItemDelegate {
                        id: deviceRow
                        required property int index
                        required property string label
                        required property string devicePath
                        required property string sizeText
                        required property string fstype
                        required property string mountPath
                        required property bool mounted
                        required property string transport
                        required property string vendor
                        required property string modelName

                        Layout.fillWidth: true
                        highlighted: backend.selectedRow === index
                        onClicked: backend.selectDevice(index)

                        contentItem: RowLayout {
                            spacing: Kirigami.Units.largeSpacing

                            // On a highlighted row every colour has to come from the
                            // theme's highlighted palette: a green "mounted" line or a
                            // highlight-coloured icon on the Kubuntu light blue
                            // selection is unreadable.

                            Kirigami.Icon {
                                source: "drive-removable-media-usb"
                                implicitWidth: Kirigami.Units.iconSizes.medium
                                implicitHeight: Kirigami.Units.iconSizes.medium
                                color: deviceRow.highlighted ? Kirigami.Theme.highlightedTextColor
                                                             : Kirigami.Theme.textColor
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 0

                                Controls.Label {
                                    text: deviceRow.label
                                    color: deviceRow.highlighted ? Kirigami.Theme.highlightedTextColor
                                                                 : Kirigami.Theme.textColor
                                    font.weight: deviceRow.highlighted ? Font.DemiBold : Font.Normal
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }

                                Controls.Label {
                                    Layout.fillWidth: true
                                    font: Kirigami.Theme.smallFont
                                    opacity: deviceRow.highlighted ? 0.9 : 0.7
                                    color: deviceRow.highlighted ? Kirigami.Theme.highlightedTextColor
                                                                 : Kirigami.Theme.textColor
                                    elide: Text.ElideRight
                                    text: {
                                        let parts = [deviceRow.devicePath, deviceRow.sizeText];
                                        if (deviceRow.fstype) {
                                            parts.push(deviceRow.fstype);
                                        }
                                        if (deviceRow.transport) {
                                            parts.push(deviceRow.transport.toUpperCase());
                                        }
                                        if (deviceRow.vendor || deviceRow.modelName) {
                                            parts.push((deviceRow.vendor + " " + deviceRow.modelName).trim());
                                        }
                                        return parts.join(" - ");
                                    }
                                }

                                Controls.Label {
                                    Layout.fillWidth: true
                                    font: Kirigami.Theme.smallFont
                                    opacity: deviceRow.highlighted ? 0.9 : 0.75
                                    color: deviceRow.highlighted ? Kirigami.Theme.highlightedTextColor
                                                                 : (deviceRow.mounted
                                                                    ? Kirigami.Theme.positiveTextColor
                                                                    : Kirigami.Theme.neutralTextColor)
                                    elide: Text.ElideMiddle
                                    text: deviceRow.mounted ? ("mounted at " + deviceRow.mountPath)
                                                            : "not mounted yet - it will be mounted automatically"
                                }
                            }

                            Controls.Button {
                                visible: !deviceRow.mounted && deviceRow.highlighted
                                text: "Mount"
                                icon.name: "media-eject"
                                onClicked: backend.mountSelected()
                            }
                        }
                    }
                }
            }
        }

        // ---------------------------------------------------------------- options
        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.largeSpacing

                Kirigami.Heading {
                    level: 3
                    text: "What should this job do?"
                }

                GridLayout {
                    Layout.fillWidth: true
                    columns: view.width > Kirigami.Units.gridUnit * 42 ? 2 : 1
                    columnSpacing: Kirigami.Units.gridUnit
                    rowSpacing: Kirigami.Units.smallSpacing

                    Controls.CheckBox {
                        id: scanCheck
                        text: "Antivirus scan (required)"
                        checked: true
                        enabled: false
                    }

                    Controls.CheckBox {
                        id: archiveCheck
                        text: "Build a ZIP image of the medium"
                        checked: true
                    }

                    Controls.CheckBox {
                        id: uploadCheck
                        text: "Upload the image to a free host"
                        checked: true
                    }

                    Controls.CheckBox {
                        id: emailCheck
                        text: "Send the verdict by email"
                        checked: true
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing

                    Controls.Label {
                        text: "Recipient(s)"
                        font: Kirigami.Theme.smallFont
                        opacity: 0.75
                    }

                    Controls.TextField {
                        id: recipientsField
                        Layout.fillWidth: true
                        placeholderText: "someone@example.com, second@example.com"
                        text: backend.settings.email.to_addrs.join(", ")
                        enabled: emailCheck.checked
                    }

                    Controls.Label {
                        Layout.fillWidth: true
                        font: Kirigami.Theme.smallFont
                        opacity: 0.6
                        wrapMode: Text.WordWrap
                        text: "Separate several addresses with commas. The report is attached to the mail."
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.largeSpacing

                    Controls.Label {
                        text: "Host:"
                        font: Kirigami.Theme.smallFont
                        opacity: 0.75
                    }

                    Controls.ComboBox {
                        id: providerBox
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 14
                        enabled: uploadCheck.checked
                        model: backend.providers
                        textRole: "label"
                        valueRole: "id"
                        Component.onCompleted: {
                            const index = backend.providers.findIndex(p => p.id === backend.settings.upload.provider);
                            currentIndex = index >= 0 ? index : 0;
                        }
                        delegate: Controls.ItemDelegate {
                            required property var modelData
                            required property int index
                            width: providerBox.width
                            text: modelData.label + (modelData.requiresKey ? " (API key)" : "")
                            highlighted: providerBox.highlightedIndex === index
                        }
                    }

                    Controls.Label {
                        Layout.fillWidth: true
                        font: Kirigami.Theme.smallFont
                        opacity: 0.6
                        wrapMode: Text.WordWrap
                        text: {
                            const provider = backend.providers[providerBox.currentIndex];
                            if (!provider) {
                                return "";
                            }
                            let note = provider.notes || "";
                            if (provider.maxBytes > 0) {
                                note += "  Limit: " + Math.round(provider.maxBytes / (1024 * 1024 * 1024) * 10) / 10 + " GB.";
                            }
                            return note;
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    visible: uploadCheck.checked
                    spacing: 2

                    Controls.Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        font: Kirigami.Theme.smallFont
                        color: Kirigami.Theme.neutralTextColor
                        text: "The uploaded image is reachable by anyone who has the link. "
                              + "DCS never uploads a suspicious or infected image."
                    }
                }

                Controls.CheckBox {
                    id: dryRunCheck
                    text: "Dry run: write the email to disk instead of sending it"
                    checked: false
                }
            }
        }

        // ---------------------------------------------------------------- engines
        Kirigami.InlineMessage {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            type: Kirigami.MessageType.Warning
            visible: {
                const clam = backend.engineStatus.find(e => e.id === "clamav");
                return clam !== undefined && !clam.available;
            }
            text: {
                const clam = backend.engineStatus.find(e => e.id === "clamav");
                return clam ? ("ClamAV is not available (" + clam.detail + "). The built-in heuristic "
                               + "engine still runs: it detects malicious behaviour such as HID payload "
                               + "scripts, download cradles and double extensions, but it is not a "
                               + "signature scanner.") : "";
            }
            actions: [
                Kirigami.Action {
                    text: "Install ClamAV..."
                    icon.name: "install"
                    onTriggered: view.installRequested()
                },
                Kirigami.Action {
                    text: "Re-check"
                    icon.name: "view-refresh"
                    onTriggered: backend.refreshEngineStatus()
                }
            ]
        }

        Kirigami.InlineMessage {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            type: Kirigami.MessageType.Information
            visible: backend.validationProblems().length > 0
            text: "Check the settings page: " + backend.validationProblems()
        }

        // ---------------------------------------------------------------- start
        RowLayout {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.bottomMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            spacing: Kirigami.Units.largeSpacing

            Controls.Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                opacity: 0.75
                text: {
                    if (!backend.sourcePath) {
                        return "Select a medium above to begin.";
                    }
                    return "Ready: " + backend.sourceLabel + " (" + backend.sourcePath + ")";
                }
            }

            DcsProgressBar {
                visible: backend.state === "running"
                indeterminate: true
                Layout.preferredWidth: Kirigami.Units.gridUnit * 8
            }

            Controls.Button {
                text: "Start the job"
                icon.name: "media-playback-start"
                highlighted: true
                enabled: backend.sourcePath !== "" && backend.state !== "running"
                onClicked: {
                    if (uploadCheck.checked && backend.settings.general.confirm_before_upload) {
                        confirmDialog.open();
                    } else {
                        view.launch();
                    }
                }
            }
        }
    }

    function launch() {
        backend.startJob({
            "source": backend.sourcePath,
            "label": backend.sourceLabel,
            "scan": true,
            "archive": archiveCheck.checked,
            "upload": uploadCheck.checked,
            "email": emailCheck.checked,
            "recipients": recipientsField.text,
            "dryRun": dryRunCheck.checked,
            "assumeYes": false
        });
        view.jobStarted();
    }

    FolderDialog {
        id: folderDialog
        title: "Choose a folder to triage"
        onAccepted: backend.setFolder(selectedFolder.toString())
    }

    Kirigami.PromptDialog {
        id: confirmDialog
        title: "Upload the archive?"
        subtitle: "The image of this medium will be uploaded to " +
                  (backend.providers[providerBox.currentIndex] ? backend.providers[providerBox.currentIndex].label : "") +
                  + " and the download link will be emailed. Anyone with the link can download it."
        customFooterActions: [
            Kirigami.Action {
                text: "Cancel"
                icon.name: "dialog-cancel"
                onTriggered: confirmDialog.close()
            },
            Kirigami.Action {
                text: "Upload and email"
                icon.name: "cloud-upload"
                onTriggered: {
                    confirmDialog.close();
                    view.launch();
                }
            }
        ]
    }
}
