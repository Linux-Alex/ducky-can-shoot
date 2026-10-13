import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Controls.ScrollView {
    id: view

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
            level: 1
            text: backend.appName
        }

        Controls.Label {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            opacity: 0.8
            text: "Version " + backend.appVersion + " - free software under the GPL-3.0-or-later."
        }

        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Heading {
                    level: 3
                    text: "What it does"
                }

                Controls.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: "Ducky Can Shoot is a defensive USB triage tool. It takes a removable medium, "
                          + "scans every file with ClamAV and a built-in heuristic engine, archives the "
                          + "medium into an image, uploads that image to a free host and emails the verdict "
                          + "together with the download link. When anything malicious is found, nothing is "
                          + "archived or uploaded: the findings stay on the machine that owns the stick."
                }

                Controls.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: "The heuristic engine is aimed at the attack this tool is named after: a USB stick "
                          + "that types commands into the victim machine as soon as it is plugged in. "
                          + "Duckyscript payloads, PowerShell download cradles, double extensions, autorun.inf "
                          + "boot scripts, macro documents, packed binaries and archive bombs are reported "
                          + "with the evidence that triggered them."
                }
            }
        }

        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Heading {
                    level: 3
                    text: "Privacy"
                }

                Controls.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: "Scanning and archiving happen locally. The only thing that leaves the machine is "
                          + "the archive you explicitly upload, and the email carrying the verdict. Uploaded "
                          + "files are reachable by anyone who has the link, so share it deliberately."
                }
            }
        }

        Kirigami.Card {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.bottomMargin: Kirigami.Units.gridUnit
            padding: Kirigami.Units.largeSpacing

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Heading {
                    level: 3
                    text: "Links"
                }

                RowLayout {
                    spacing: Kirigami.Units.smallSpacing

                    Controls.Button {
                        text: "Homepage"
                        icon.name: "internet-services"
                        onClicked: backend.openUrl(backend.appHomepage)
                    }

                    Controls.Button {
                        text: "Open config file"
                        icon.name: "document-open"
                        onClicked: backend.openConfig()
                    }
                }

                Controls.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                    text: "Built with Python, PyQt6 and Kirigami. ClamAV is used when it is installed; "
                          + "the interface follows your Plasma colour scheme and icon theme."
                }
            }
        }
    }
}
