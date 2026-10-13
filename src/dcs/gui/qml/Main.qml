import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

import "views"
import "components"

/*!
    Ducky Can Shoot - main window.

    Navigation is a TabBar over a StackLayout rather than a pageStack because
    the tool has four fixed destinations (setup, live run, result, settings)
    and the user must always be able to jump back to the live view while a job
    is running.  The window itself is a Kirigami.ApplicationWindow so it
    inherits the user's KDE theme, colours, icons and window decorations.
*/
Kirigami.ApplicationWindow {
    id: root

    title: backend.appName + " - USB triage"
    width: backend.settings.gui.remember_window_size ? backend.settings.gui.window_width : 1200
    height: backend.settings.gui.remember_window_size ? backend.settings.gui.window_height : 820
    minimumWidth: 900
    minimumHeight: 620

    // The colours, icons and fonts come from the Plasma theme: DCS sets no
    // colour values of its own, so it looks native on the user's desktop.

    property int tabIndex: 0
    property bool jobActive: backend.state === "running"

    //! convenience helper used by the views
    function showTab(index) {
        root.tabIndex = index;
        tabs.currentIndex = index;
    }

    function toast(message, level) {
        toastPopup.show(message, level);
    }

    // No global drawer on purpose: the tab bar below already carries every
    // destination, and a Kirigami drawer adds a hamburger row (and its own
    // DrawerHandle binding loop) that this window does not need.

    pageStack.initialPage: Kirigami.Page {
        id: dashboard
        padding: 0
        title: ""

        // Kirigami builds a toolbar row for the current page and reserves its
        // height (48 px with Breeze) even when there is no title, no action and
        // no drawer to show, which left an empty band above the tab bar.
        globalToolBarStyle: Kirigami.ApplicationHeaderStyle.None

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            Controls.TabBar {
                id: tabs
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                Layout.leftMargin: Kirigami.Units.smallSpacing
                Layout.rightMargin: Kirigami.Units.smallSpacing
                currentIndex: root.tabIndex
                onCurrentIndexChanged: root.tabIndex = currentIndex

                Controls.TabButton {
                    text: "New job"
                    icon.name: "document-new"
                }
                Controls.TabButton {
                    text: backend.jobActive ? "Scanning..." : "Progress"
                    icon.name: "view-statistics"
                }
                Controls.TabButton {
                    text: "Result"
                    icon.name: "dialog-ok-apply"
                }
                Controls.TabButton {
                    text: "Settings"
                    icon.name: "settings-configure"
                }
                Controls.TabButton {
                    text: "About"
                    icon.name: "help-about"
                }
            }

            Kirigami.Separator {
                Layout.fillWidth: true
            }

            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: tabs.currentIndex

                HomeView {
                    onJobStarted: root.showTab(1)
                    onInstallRequested: clamavDialog.open()
                }
                LiveView {
                    onJobFinished: {
                        if (!backend.summary || backend.summary.verdict === undefined) {
                            return;
                        }
                        if (backend.settings.gui.switch_to_result) {
                            root.showTab(2);
                        }
                    }
                }
                ResultView {
                    onRunAgain: root.showTab(0)
                }
                SettingsView {
                    onInstallRequested: clamavDialog.open()
                }
                AboutView {}
            }

            // The status bar is the last row of this column rather than the
            // window's footer.  A styled footer container paints its background
            // at its own implicit height while the window content (which
            // Kirigami raises above it) covers the rest, so the bar used to be
            // drawn about two thirds tall: a half visible footer.
            Rectangle {
                id: statusBar
                objectName: "statusBar"
                Layout.fillWidth: true
                Layout.preferredHeight: visible ? Kirigami.Units.gridUnit * 2.6 : 0
                visible: backend.state !== "idle"
                color: Kirigami.Theme.alternateBackgroundColor

                Rectangle {
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.right: parent.right
                    height: 1
                    color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g,
                                   Kirigami.Theme.textColor.b, 0.12)
                }

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: Kirigami.Units.largeSpacing
                    anchors.rightMargin: Kirigami.Units.largeSpacing
                    spacing: Kirigami.Units.largeSpacing

                    Kirigami.Icon {
                        source: backend.state === "running" ? "process-working"
                                : backend.state === "finished" ? "dialog-ok-apply"
                                : backend.state === "failed" ? "dialog-error" : "dialog-information"
                        implicitWidth: Kirigami.Units.iconSizes.small
                        implicitHeight: Kirigami.Units.iconSizes.small
                    }

                    Controls.Label {
                        text: {
                            if (backend.state === "running") {
                                return "Working on " + (backend.sourceLabel || "the selected medium");
                            }
                            if (backend.state === "finished") {
                                return "Finished: " + (backend.summary.verdict || "");
                            }
                            if (backend.state === "failed") {
                                return "The job failed";
                            }
                            return "Idle";
                        }
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Layout.minimumWidth: Kirigami.Units.gridUnit * 8
                    }

                    DcsProgressBar {
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 14
                        Layout.maximumWidth: Math.max(Kirigami.Units.gridUnit * 14, root.width * 0.35)
                        visible: backend.state !== "idle"
                        value: backend.overallPercent
                        indeterminate: backend.state === "running" && backend.overallPercent <= 0.001
                    }

                    Kirigami.ActionToolBar {
                        alignment: Qt.AlignRight
                        actions: [
                            Kirigami.Action {
                                text: "Cancel"
                                icon.name: "process-stop"
                                visible: backend.state === "running"
                                onTriggered: cancelDialog.open()
                            }
                        ]
                    }
                }
            }
        }
    }

    InstallClamavDialog {
        id: clamavDialog
    }

    Controls.Popup {
        id: toastPopup
        property string message: ""
        property string level: "info"

        function show(text, kind) {
            message = text;
            level = kind || "info";
            open();
            hideTimer.restart();
        }

        parent: Controls.Overlay.overlay
        x: Math.round((parent.width - width) / 2)
        y: parent.height - height - Kirigami.Units.gridUnit * 3
        width: Math.min(parent.width - Kirigami.Units.gridUnit * 2, toastRow.implicitWidth + Kirigami.Units.largeSpacing * 2)
        padding: Kirigami.Units.largeSpacing
        closePolicy: Controls.Popup.NoAutoClose
        background: Rectangle {
            color: Kirigami.Theme.backgroundColor
            border.width: 1
            border.color: toastPopup.level === "error" ? Kirigami.Theme.negativeTextColor
                          : toastPopup.level === "warning" ? Kirigami.Theme.neutralTextColor
                          : Kirigami.Theme.positiveTextColor
            radius: Kirigami.Units.smallSpacing
        }

        Timer {
            id: hideTimer
            interval: 4200
            onTriggered: toastPopup.close()
        }

        contentItem: RowLayout {
            id: toastRow
            spacing: Kirigami.Units.smallSpacing

            Kirigami.Icon {
                source: toastPopup.level === "error" ? "dialog-error"
                        : toastPopup.level === "warning" ? "dialog-warning"
                        : "dialog-information"
                implicitWidth: Kirigami.Units.iconSizes.small
                implicitHeight: Kirigami.Units.iconSizes.small
            }

            Controls.Label {
                text: toastPopup.message
                wrapMode: Text.WordWrap
                Layout.maximumWidth: Kirigami.Units.gridUnit * 26
            }
        }
    }

    Connections {
        target: backend
        function onToast(level, message) {
            root.toast(message, level);
        }
        function onStateChanged() {
            if (backend.state === "running" && root.tabIndex !== 1) {
                root.showTab(1);
            }
        }
    }

    Kirigami.PromptDialog {
        id: cancelDialog
        title: "Cancel the running job?"
        subtitle: "The scan stops after the file it is working on. Nothing is uploaded and no email is sent."
        customFooterActions: [
            Kirigami.Action {
                text: "Keep running"
                icon.name: "dialog-cancel"
                onTriggered: cancelDialog.close()
            },
            Kirigami.Action {
                text: "Cancel the job"
                icon.name: "process-stop"
                onTriggered: {
                    backend.cancelJob();
                    cancelDialog.close();
                }
            }
        ]
    }
}
