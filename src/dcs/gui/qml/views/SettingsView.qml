import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

/*!
    Step 4: settings.

    The page edits a local draft and writes everything back in one go, so a
    half-finished form never reaches the config file.  Secrets (SMTP password,
    API keys) are never read back from disk: leave the field empty to keep the
    stored value, or use the environment variable / helper command instead.

    The form scrolls, the buttons do not: "Save Settings" sits in a bar at the
    bottom of the page and is visible from every scroll position, next to
    whether the draft differs from the file on disk.  "Test SMTP" tests the
    values that are in the form, not the ones that were saved last.
*/
Item {
    id: view

    signal installRequested()

    // ---- draft state -----------------------------------------------------
    property bool enginesClamav: true
    property bool enginesHeuristic: true
    property int maxFileSizeMb: 4096
    property bool hashFindings: true
    property string excludeGlobs: ""
    property string blockingSeverity: "high"

    property bool archiveEnabled: true
    property string archiveFormat: "zip"
    property int compressionLevel: 6
    property int splitSizeMb: 0
    property int sizeWarnGb: 20
    property string archiveWhen: "clean"

    property bool uploadEnabled: true
    property string provider: "gofile"
    property string uploadWhen: "clean"
    property bool deleteArchiveAfterUpload: false

    property bool emailEnabled: true
    property string smtpHost: ""
    property int smtpPort: 587
    property string security: "starttls"
    property string smtpUser: ""
    property string smtpPassword: ""
    property string fromAddr: ""
    property string fromName: "Ducky Can Shoot"
    property string recipients: ""
    property bool attachReport: true
    property bool insecureTls: false

    property bool keepArchive: true
    property bool confirmUpload: true
    property bool showLogPane: true
    property bool switchToResult: true
    property string outputDir: ""
    property string reportDir: ""

    property string watchMode: "off"
    property bool watchAutostart: false
    property bool updateCheckOnStart: true

    //! true while "Test SMTP" is waiting for the server
    property bool smtpTesting: false

    //! what the configuration would complain about if this draft were saved:
    //! shown in the message above the cards, and it keeps Save disabled while
    //! it is not empty (the HIG's "validate and disable confirm" rule)
    readonly property string validationText: backend.validateDraft(draftPayload())

    //! JSON of the draft as it was loaded (or last saved), so the bar at the
    //! bottom can say whether there is anything to save
    property string savedDraft: ""
    //! Item already has a FINAL "dirty" property, hence the longer name here
    readonly property bool unsavedChanges: JSON.stringify(draftPayload()) !== savedDraft

    //! the sentence under the USB combo box
    readonly property string watchModeDescription: {
        const modes = backend.watchModes;
        for (let i = 0; i < modes.length; ++i) {
            if (modes[i].value === watchMode) {
                return modes[i].description;
            }
        }
        return "";
    }

    //! only meaningful when a recipient is configured; said out loud below
    readonly property bool recipientsConfigured: splitEmails(recipients).length > 0

    function reset() {
        const s = backend.settings;
        enginesClamav = s.scan.engines.indexOf("clamav") >= 0;
        enginesHeuristic = s.scan.engines.indexOf("heuristic") >= 0;
        maxFileSizeMb = s.scan.max_file_size_mb;
        hashFindings = s.scan.hash_findings;
        excludeGlobs = s.scan.exclude_globs.join(", ");
        blockingSeverity = s.scan.blocking_severity;

        archiveEnabled = s.archive.enabled;
        archiveFormat = s.archive.format;
        compressionLevel = s.archive.compression_level;
        splitSizeMb = s.archive.split_size_mb;
        sizeWarnGb = s.archive.size_warn_gb;
        archiveWhen = s.archive.when;

        uploadEnabled = s.upload.enabled;
        provider = s.upload.provider;
        uploadWhen = s.upload.upload_when;
        deleteArchiveAfterUpload = s.upload.delete_archive_after_upload;

        emailEnabled = s.email.enabled;
        smtpHost = s.email.smtp_host;
        smtpPort = s.email.smtp_port;
        security = s.email.security;
        smtpUser = s.email.username;
        smtpPassword = "";
        fromAddr = s.email.from_addr;
        fromName = s.email.from_name;
        recipients = s.email.to_addrs.join(", ");
        attachReport = s.email.attach_report;
        insecureTls = s.email.insecure_tls;

        keepArchive = s.general.keep_archive;
        confirmUpload = s.general.confirm_before_upload;
        showLogPane = s.gui.show_log_pane;
        switchToResult = s.gui.switch_to_result;
        outputDir = s.general.output_dir;
        reportDir = s.report.directory;

        watchMode = s.watch.mode;
        watchAutostart = s.watch.autostart;
        updateCheckOnStart = s.updates.check_on_start;

        // what is on screen now is what the file says: nothing to save
        savedDraft = JSON.stringify(draftPayload());
    }

    function splitEmails(text) {
        return text.split(/[,;\s]+/).filter(part => part.indexOf("@") > 0);
    }

    /*! Everything this page edits, in the shape the backend expects. */
    function draftPayload() {
        const payload = {
            "general": {
                "keep_archive": keepArchive,
                "confirm_before_upload": confirmUpload,
                "output_dir": outputDir
            },
            "scan": {
                "engines": (enginesClamav ? ["clamav"] : []).concat(enginesHeuristic ? ["heuristic"] : []),
                "max_file_size_mb": maxFileSizeMb,
                "hash_findings": hashFindings,
                "exclude_globs": excludeGlobs.split(",").map(s => s.trim()).filter(s => s !== ""),
                "blocking_severity": blockingSeverity
            },
            "archive": {
                "enabled": archiveEnabled,
                "format": archiveFormat,
                "compression_level": compressionLevel,
                "split_size_mb": splitSizeMb,
                "size_warn_gb": sizeWarnGb,
                "when": archiveWhen
            },
            "upload": {
                "enabled": uploadEnabled,
                "provider": provider,
                "upload_when": uploadWhen,
                "delete_archive_after_upload": deleteArchiveAfterUpload
            },
            "email": {
                "enabled": emailEnabled,
                "smtp_host": smtpHost,
                "smtp_port": smtpPort,
                "security": security,
                "username": smtpUser,
                "from_addr": fromAddr,
                "from_name": fromName,
                "to_addrs": splitEmails(recipients),
                "attach_report": attachReport,
                "insecure_tls": insecureTls
            },
            "report": {
                "directory": reportDir
            },
            "gui": {
                "show_log_pane": showLogPane,
                "switch_to_result": switchToResult
            }
        };
        payload.watch = {
            "mode": watchMode,
            "autostart": watchAutostart
        };
        payload.updates = {
            "check_on_start": updateCheckOnStart
        };
        if (smtpPassword !== "") {
            payload.email.password = smtpPassword;
        }
        return payload;
    }

    function save() {
        backend.saveSettings(draftPayload());
    }

    /*! The email section of the draft: what "Test SMTP" must test. */
    function smtpDraft() {
        return draftPayload().email;
    }

    Component.onCompleted: reset()

    Connections {
        target: backend

        function onSettingsChanged() {
            view.reset();
        }

        function onTestResult(what, ok, message) {
            // The SMTP answer appears next to the button that asked for it.
            // The window wide toast is at the top of a page that scrolls, and
            // scrolling down to press "Test SMTP" is exactly when it is missed.
            if (what === "smtp") {
                view.smtpTesting = false;
                smtpResult.type = ok ? Kirigami.MessageType.Positive : Kirigami.MessageType.Error;
                smtpResult.text = message;
                smtpResult.visible = true;
                return;
            }
            testOutput.type = ok ? Kirigami.MessageType.Positive : Kirigami.MessageType.Error;
            testOutput.text = what + ": " + message;
            testOutput.visible = true;
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // The form scrolls, this bar does not: the save button used to sit at
        // the bottom of a long page, which is how a settings change could be
        // left unsaved without noticing.
        Controls.ScrollView {
            id: scroll
            objectName: "settingsScroll"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth

            ColumnLayout {
                // the form itself
                width: scroll.availableWidth
                spacing: Kirigami.Units.largeSpacing

                Item {
                    Layout.preferredHeight: Kirigami.Units.largeSpacing
                }

                // Title, description and messages form one group, so they sit
                // closer to each other than to the cards below (HIG: spacing
                // between a title group and other content is smallSpacing).
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.gridUnit
                    Layout.rightMargin: Kirigami.Units.gridUnit
                    spacing: Kirigami.Units.smallSpacing

                    Kirigami.Heading {
                        Layout.fillWidth: true
                        level: 1
                        text: "Settings"
                    }

                    Controls.Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        opacity: 0.75
                        text: "Stored in " + backend.configPath + ". Secrets stay out of the file: set "
                              + "DCS_SMTP_PASSWORD, DCS_GOFILE_TOKEN or DCS_PIXELDRAIN_API_KEY in the environment instead."
                    }

                    // The HIG asks for an InlineMessage when input is invalid,
                    // and for the action that would commit it to be disabled.
                    Kirigami.InlineMessage {
                        id: validationMessage
                        objectName: "validationMessage"
                        Layout.fillWidth: true
                        Layout.topMargin: Kirigami.Units.smallSpacing
                        visible: text.length > 0
                        type: Kirigami.MessageType.Warning
                        text: view.validationText
                    }

                    Kirigami.InlineMessage {
                        id: testOutput
                        Layout.fillWidth: true
                        visible: false
                    }
                }

                // ------------------------------------------------------------ scanning
                Kirigami.Card {
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.gridUnit
                    Layout.rightMargin: Kirigami.Units.gridUnit
                    padding: Kirigami.Units.largeSpacing

                    contentItem: ColumnLayout {
                        Kirigami.Heading {
                            level: 3
                            text: "Scanning"
                        }

                        Kirigami.FormLayout {
                            Layout.fillWidth: true

                            Controls.CheckBox {
                                Kirigami.FormData.label: "Engines:"
                                text: "ClamAV (signatures, needs the clamav package)"
                                checked: view.enginesClamav
                                onToggled: view.enginesClamav = checked
                            }

                            Controls.CheckBox {
                                text: "Built-in heuristics (always useful)"
                                checked: view.enginesHeuristic
                                onToggled: view.enginesHeuristic = checked
                            }

                            Controls.SpinBox {
                                Kirigami.FormData.label: "Skip files larger than (MiB):"
                                from: 0
                                to: 1024 * 1024
                                stepSize: 256
                                value: view.maxFileSizeMb
                                onValueModified: view.maxFileSizeMb = value
                            }

                            Controls.CheckBox {
                                Kirigami.FormData.label: "Reports:"
                                text: "Hash every finding (SHA-256)"
                                checked: view.hashFindings
                                onToggled: view.hashFindings = checked
                            }

                            Controls.TextField {
                                Kirigami.FormData.label: "Exclude patterns:"
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 22
                                text: view.excludeGlobs
                                placeholderText: "System Volume Information/*, *.tmp"
                                onTextEdited: view.excludeGlobs = text
                            }

                            Controls.ComboBox {
                                Kirigami.FormData.label: "Treat as infected from:"
                                model: ["medium", "high", "critical"]
                                currentIndex: Math.max(0, model.indexOf(view.blockingSeverity))
                                onActivated: view.blockingSeverity = model[currentIndex]
                            }
                        }

                        Repeater {
                            model: backend.engineStatus

                            delegate: RowLayout {
                                id: engineLine
                                required property var modelData

                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing

                                Controls.Label {
                                    Layout.fillWidth: true
                                    wrapMode: Text.WordWrap
                                    font: Kirigami.Theme.smallFont
                                    opacity: 0.75
                                    color: engineLine.modelData.available ? Kirigami.Theme.textColor
                                                                          : Kirigami.Theme.neutralTextColor
                                    text: (engineLine.modelData.available ? "OK  " : "missing  ")
                                          + engineLine.modelData.label + " - " + engineLine.modelData.detail
                                }

                                Controls.Button {
                                    visible: !engineLine.modelData.available
                                    text: "Install ClamAV…"
                                    icon.name: "install"
                                    onClicked: view.installRequested()
                                }

                                Controls.Button {
                                    visible: engineLine.modelData.available
                                    text: "Re-check"
                                    icon.name: "view-refresh"
                                    onClicked: backend.refreshEngineStatus()
                                }
                            }
                        }
                    }
                }

                // ------------------------------------------------------------ archive
                Kirigami.Card {
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.gridUnit
                    Layout.rightMargin: Kirigami.Units.gridUnit
                    padding: Kirigami.Units.largeSpacing

                    contentItem: ColumnLayout {
                        Kirigami.Heading {
                            level: 3
                            text: "Archive"
                        }

                        Kirigami.FormLayout {
                            Layout.fillWidth: true

                            Controls.CheckBox {
                                Kirigami.FormData.label: "Build an image:"
                                text: "Archive the medium before uploading"
                                checked: view.archiveEnabled
                                onToggled: view.archiveEnabled = checked
                            }

                            Controls.ComboBox {
                                Kirigami.FormData.label: "Format:"
                                model: ["zip", "7z"]
                                currentIndex: Math.max(0, model.indexOf(view.archiveFormat))
                                onActivated: view.archiveFormat = model[currentIndex]
                            }

                            Controls.ComboBox {
                                Kirigami.FormData.label: "When:"
                                model: ["clean", "always", "never"]
                                currentIndex: Math.max(0, model.indexOf(view.archiveWhen))
                                onActivated: view.archiveWhen = model[currentIndex]
                            }

                            Controls.Slider {
                                Kirigami.FormData.label: "Compression level:"
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 14
                                from: 0
                                to: 9
                                stepSize: 1
                                value: view.compressionLevel
                                onMoved: view.compressionLevel = value
                            }

                            Controls.SpinBox {
                                Kirigami.FormData.label: "Split into volumes of (MiB, 0 = no split):"
                                from: 0
                                to: 1024 * 64
                                stepSize: 256
                                value: view.splitSizeMb
                                onValueModified: view.splitSizeMb = value
                            }

                            Controls.SpinBox {
                                Kirigami.FormData.label: "Warn above (GiB):"
                                from: 0
                                to: 4096
                                value: view.sizeWarnGb
                                onValueModified: view.sizeWarnGb = value
                            }

                            Controls.TextField {
                                Kirigami.FormData.label: "Output folder:"
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 22
                                placeholderText: "default: ~/.local/share/dcs"
                                text: view.outputDir
                                onTextEdited: view.outputDir = text
                            }
                        }
                    }
                }

                // ------------------------------------------------------------ upload
                Kirigami.Card {
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.gridUnit
                    Layout.rightMargin: Kirigami.Units.gridUnit
                    padding: Kirigami.Units.largeSpacing

                    contentItem: ColumnLayout {
                        Kirigami.Heading {
                            level: 3
                            text: "Upload"
                        }

                        Kirigami.FormLayout {
                            Layout.fillWidth: true

                            Controls.CheckBox {
                                Kirigami.FormData.label: "Enabled:"
                                text: "Upload the archive to a free host"
                                checked: view.uploadEnabled
                                onToggled: view.uploadEnabled = checked
                            }

                            Controls.ComboBox {
                                id: providerCombo
                                Kirigami.FormData.label: "Provider:"
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 16
                                model: backend.providers
                                textRole: "label"
                                valueRole: "id"
                                currentIndex: {
                                    const index = backend.providers.findIndex(p => p.id === view.provider);
                                    return index >= 0 ? index : 0;
                                }
                                onActivated: view.provider = backend.providers[currentIndex].id
                                delegate: Controls.ItemDelegate {
                                    required property var modelData
                                    required property int index
                                    width: providerCombo.width
                                    text: modelData.label + (modelData.requiresKey ? "  (API key required)" : "")
                                    highlighted: providerCombo.highlightedIndex === index
                                }
                            }

                            Controls.ComboBox {
                                Kirigami.FormData.label: "Upload when:"
                                model: ["clean", "always", "never"]
                                currentIndex: Math.max(0, model.indexOf(view.uploadWhen))
                                onActivated: view.uploadWhen = model[currentIndex]
                            }

                            Controls.CheckBox {
                                text: "Delete the local archive after a successful upload"
                                checked: view.deleteArchiveAfterUpload
                                onToggled: view.deleteArchiveAfterUpload = checked
                            }

                            RowLayout {
                                Kirigami.FormData.label: "Reachability:"
                                Controls.Button {
                                    text: "Test Provider"
                                    icon.name: "network-connect"
                                    onClicked: backend.testProvider(view.provider)
                                }
                            }
                        }

                        Controls.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            font: Kirigami.Theme.smallFont
                            opacity: 0.7
                            text: {
                                const entry = backend.providers.find(p => p.id === view.provider);
                                return entry ? entry.notes : "";
                            }
                        }
                    }
                }

                // ------------------------------------------------------------ email
                Kirigami.Card {
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.gridUnit
                    Layout.rightMargin: Kirigami.Units.gridUnit
                    padding: Kirigami.Units.largeSpacing

                    contentItem: ColumnLayout {
                        Kirigami.Heading {
                            level: 3
                            text: "Email notification"
                        }

                        Kirigami.FormLayout {
                            Layout.fillWidth: true

                            Controls.CheckBox {
                                Kirigami.FormData.label: "Enabled:"
                                text: "Send the verdict by SMTP"
                                checked: view.emailEnabled
                                onToggled: view.emailEnabled = checked
                            }

                            Controls.TextField {
                                Kirigami.FormData.label: "SMTP server:"
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 18
                                placeholderText: "smtp.example.com"
                                text: view.smtpHost
                                onTextEdited: view.smtpHost = text
                            }

                            Controls.SpinBox {
                                Kirigami.FormData.label: "Port:"
                                from: 1
                                to: 65535
                                value: view.smtpPort
                                onValueModified: view.smtpPort = value
                            }

                            Controls.ComboBox {
                                Kirigami.FormData.label: "Security:"
                                model: ["starttls", "ssl", "none"]
                                currentIndex: Math.max(0, model.indexOf(view.security))
                                onActivated: view.security = model[currentIndex]
                            }

                            Controls.TextField {
                                Kirigami.FormData.label: "Username:"
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 18
                                text: view.smtpUser
                                onTextEdited: view.smtpUser = text
                            }

                            // Kirigami.PasswordField: the HIG's control for a
                            // password, with the reveal button built in.
                            Kirigami.PasswordField {
                                objectName: "smtpPasswordField"
                                Kirigami.FormData.label: "Password:"
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 18
                                text: view.smtpPassword
                                onTextEdited: view.smtpPassword = text
                            }

                            // One short sentence under the control it explains
                            // (the HIG's rule for inline help; a placeholder is
                            // for an example, not for instructions).
                            Controls.Label {
                                Layout.fillWidth: true
                                Layout.maximumWidth: Kirigami.Units.gridUnit * 22
                                wrapMode: Text.WordWrap
                                opacity: 0.7
                                font: Kirigami.Theme.smallFont
                                text: "Leave empty to keep the password that is already stored. "
                                      + "Secrets are best kept out of the file entirely: set "
                                      + "DCS_SMTP_PASSWORD in the environment instead."
                            }

                            Controls.TextField {
                                Kirigami.FormData.label: "Sender:"
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 18
                                placeholderText: "dcs@example.com"
                                text: view.fromAddr
                                onTextEdited: view.fromAddr = text
                            }

                            Controls.TextField {
                                Kirigami.FormData.label: "Recipients:"
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 22
                                placeholderText: "me@example.com, team@example.com"
                                text: view.recipients
                                onTextEdited: view.recipients = text
                            }

                            Controls.CheckBox {
                                Kirigami.FormData.label: "Attachments:"
                                text: "Attach the HTML report and the findings CSV"
                                checked: view.attachReport
                                onToggled: view.attachReport = checked
                            }

                            Controls.CheckBox {
                                text: "Do not verify the TLS certificate (internal relays only)"
                                checked: view.insecureTls
                                onToggled: view.insecureTls = checked
                            }

                            RowLayout {
                                Kirigami.FormData.label: "Reachability:"

                                Controls.Button {
                                    objectName: "testSmtpButton"
                                    text: view.smtpTesting ? "Testing…" : "Test SMTP"
                                    icon.name: "network-connect"
                                    enabled: !view.smtpTesting
                                    onClicked: {
                                        view.smtpTesting = true;
                                        smtpResult.visible = false;
                                        backend.testSmtp(view.smtpDraft());
                                    }
                                }

                                Controls.Label {
                                    Layout.fillWidth: true
                                    wrapMode: Text.WordWrap
                                    opacity: 0.7
                                    text: view.unsavedChanges
                                          ? "Tested values are the ones in the form; they are not saved yet"
                                          : "The values above are the saved ones"
                                }
                            }
                        }

                        // The answer belongs next to the button that asked the
                        // question, not only in the window wide toast at the top
                        // of a page that scrolls.
                        Kirigami.InlineMessage {
                            id: smtpResult
                            Layout.fillWidth: true
                            Layout.topMargin: Kirigami.Units.smallSpacing
                            visible: false
                            showCloseButton: true
                            type: Kirigami.MessageType.Information
                        }
                    }
                }

                // ------------------------------------------------------------ usb detection
                Kirigami.Card {
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.gridUnit
                    Layout.rightMargin: Kirigami.Units.gridUnit
                    padding: Kirigami.Units.largeSpacing

                    contentItem: ColumnLayout {
                        spacing: Kirigami.Units.largeSpacing

                        Kirigami.Heading {
                            level: 3
                            text: "USB detection"
                        }

                        Controls.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            opacity: 0.8
                            text: "What should happen when a medium is plugged in? Off by default: "
                                  + "nothing is watched until a mode is chosen below."
                        }

                        Controls.ComboBox {
                            id: watchCombo
                            objectName: "watchModeCombo"
                            Layout.fillWidth: true
                            Layout.maximumWidth: Kirigami.Units.gridUnit * 34
                            textRole: "label"
                            model: backend.watchModes
                            currentIndex: {
                                const modes = backend.watchModes;
                                for (let i = 0; i < modes.length; ++i) {
                                    if (modes[i].value === view.watchMode) {
                                        return i;
                                    }
                                }
                                return 0;
                            }
                            onActivated: view.watchMode = backend.watchModes[currentIndex].value
                        }

                        Controls.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            opacity: 0.75
                            text: view.watchModeDescription
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Kirigami.Units.smallSpacing

                            Controls.CheckBox {
                                id: autostartCheck
                                objectName: "watchAutostartCheck"
                                Layout.fillWidth: true
                                enabled: view.watchMode !== "off"
                                text: "Start the watcher with my session"
                                checked: view.watchAutostart
                                onToggled: view.watchAutostart = checked
                            }

                            // Three sentences of explanation would overrun this
                            // card, which is what the HIG's contextual help
                            // button is for.
                            Kirigami.ContextualHelpButton {
                                objectName: "watchHelpButton"
                                toolTipText: "With this on, " + backend.autostartPath
                                    + " runs a small background command with your desktop session "
                                    + "and watches for new media. Enabling a mode starts it right "
                                    + "away as well, and closing this window leaves it running. It "
                                    + "only ever looks at the list of block devices: no file is "
                                    + "read until a scan is agreed to. While this window is open "
                                    + "it does the watching itself, so a medium is never reported "
                                    + "twice. What it does is logged in " + backend.watchLogPath + "."
                            }
                        }

                        // The watcher only helps while it is actually running, so
                        // the page says whether it is and offers to start it
                        // now - the autostart entry only takes effect at the
                        // next login.
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Kirigami.Units.smallSpacing

                            Controls.Label {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                opacity: 0.75
                                text: "Background watcher: " + backend.watchService
                            }

                            Controls.Button {
                                objectName: "startWatcherButton"
                                text: "Start Now"
                                icon.name: "media-playback-start"
                                visible: view.watchMode !== "off"
                                         && backend.watchService === "not running"
                                onClicked: backend.startWatchService()
                            }

                            Controls.Button {
                                objectName: "stopWatcherButton"
                                text: "Stop"
                                icon.name: "process-stop"
                                visible: backend.watchService !== "not running"
                                onClicked: backend.stopWatchService()
                            }
                        }

                        Controls.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            visible: view.watchMode === "open_run"
                            opacity: 0.75
                            text: view.recipientsConfigured
                                  ? "A recipient is configured, so the automatic run also sends the email."
                                  : "Without a recipient the automatic run stops after the report and "
                                    + "shows the link on the Result page. Add one above to have the "
                                    + "email sent too."
                        }
                    }
                }

                // ------------------------------------------------------------ updates
                Kirigami.Card {
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.gridUnit
                    Layout.rightMargin: Kirigami.Units.gridUnit
                    padding: Kirigami.Units.largeSpacing

                    contentItem: ColumnLayout {
                        spacing: Kirigami.Units.largeSpacing

                        Kirigami.Heading {
                            level: 3
                            text: "Updates"
                        }

                        Controls.CheckBox {
                            id: updateCheckOnStartCheck
                            objectName: "updateCheckOnStartCheck"
                            Layout.fillWidth: true
                            text: "Check for updates when Ducky Can Shoot starts"
                            checked: view.updateCheckOnStart
                            onToggled: view.updateCheckOnStart = checked
                        }

                        Controls.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            opacity: 0.8
                            text: {
                                const info = backend.updateInfo;
                                const kind = info.install && info.install.detail
                                    ? info.install.detail : "";
                                return "Version " + backend.appVersion + (kind ? " · " + kind : "");
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Kirigami.Units.smallSpacing

                            Controls.Button {
                                objectName: "checkForUpdatesButton"
                                text: "Check for Updates"
                                icon.name: "system-software-update"
                                enabled: backend.updateState !== "checking"
                                         && backend.updateState !== "installing"
                                         && backend.updateState !== "restarting"
                                onClicked: backend.checkForUpdates()
                            }

                            Controls.Button {
                                objectName: "installUpdateButton"
                                visible: backend.updateInfo.canInstall === true
                                text: "Download and Install"
                                icon.name: "download"
                                enabled: backend.updateState === "available"
                                onClicked: backend.installUpdate()
                            }

                            Controls.Button {
                                objectName: "openReleaseButton"
                                visible: backend.updateState === "available"
                                text: "Release Notes"
                                icon.name: "help-about"
                                onClicked: backend.openReleasePage()
                            }

                            Controls.BusyIndicator {
                                objectName: "updateBusy"
                                running: backend.updateState === "checking"
                                         || backend.updateState === "installing"
                                         || backend.updateState === "restarting"
                                visible: running
                                implicitWidth: Kirigami.Units.iconSizes.smallMedium
                                implicitHeight: Kirigami.Units.iconSizes.smallMedium
                            }

                            Item {
                                Layout.fillWidth: true
                            }
                        }

                        Controls.Label {
                            objectName: "updateMessage"
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            visible: text !== ""
                            text: backend.updateInfo.message
                            color: backend.updateState === "error"
                                   ? Kirigami.Theme.negativeTextColor
                                   : (backend.updateState === "available"
                                      ? Kirigami.Theme.positiveTextColor
                                      : Kirigami.Theme.textColor)
                        }

                        Controls.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            opacity: 0.7
                            visible: backend.updateInfo.otherInstalls.length > 0
                            text: "Also installed on this machine: "
                                  + backend.updateInfo.otherInstalls.join("; ")
                                  + ". DCS only updates the copy it is running from."
                        }

                        Controls.Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            opacity: 0.7
                            visible: backend.updateState === "available"
                                  && backend.updateInfo.install.needsRoot === true
                            text: "Installing the package asks for your system password; "
                                  + "Ducky Can Shoot never sees it. The window restarts itself "
                                  + "afterwards, and so does the background watcher."
                        }
                    }
                }

                // ------------------------------------------------------------ interface
                Kirigami.Card {
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.gridUnit
                    Layout.rightMargin: Kirigami.Units.gridUnit
                    Layout.bottomMargin: Kirigami.Units.gridUnit
                    padding: Kirigami.Units.largeSpacing

                    contentItem: ColumnLayout {
                        Kirigami.Heading {
                            level: 3
                            text: "Interface and reports"
                        }

                        Kirigami.FormLayout {
                            Layout.fillWidth: true

                            Controls.CheckBox {
                                Kirigami.FormData.label: "Live view:"
                                text: "Show the log pane"
                                checked: view.showLogPane
                                onToggled: view.showLogPane = checked
                            }

                            Controls.CheckBox {
                                text: "Switch to the result page when a job finishes"
                                checked: view.switchToResult
                                onToggled: view.switchToResult = checked
                            }

                            Controls.CheckBox {
                                Kirigami.FormData.label: "Safety:"
                                text: "Keep the local archive after uploading"
                                checked: view.keepArchive
                                onToggled: view.keepArchive = checked
                            }

                            Controls.CheckBox {
                                text: "Ask before uploading a public link"
                                checked: view.confirmUpload
                                onToggled: view.confirmUpload = checked
                            }

                            Controls.TextField {
                                Kirigami.FormData.label: "Report folder:"
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 22
                                placeholderText: "default: ~/.local/share/dcs/reports"
                                text: view.reportDir
                                onTextEdited: view.reportDir = text
                            }

                            RowLayout {
                                Kirigami.FormData.label: "Files:"
                                spacing: Kirigami.Units.smallSpacing

                                Controls.Button {
                                    text: "Open Config File"
                                    icon.name: "document-open"
                                    onClicked: backend.openConfig()
                                }

                                Controls.Button {
                                    text: "Open Report Folder"
                                    icon.name: "folder-open"
                                    onClicked: backend.openPath(backend.settings.report.directory !== ""
                                                                ? backend.settings.report.directory
                                                                : "")
                                }
                            }
                        }
                    }
                }

            }
        }

        Kirigami.Separator {
            Layout.fillWidth: true
        }

        Rectangle {
            id: saveBar
            objectName: "saveBar"
            Layout.fillWidth: true
            // Tall enough for its contents whatever the font size is: a fixed
            // gridUnit multiple clips the buttons at a large system font.
            Layout.preferredHeight: Math.max(Kirigami.Units.gridUnit * 3.4,
                                             barRow.implicitHeight + Kirigami.Units.largeSpacing)
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
                id: barRow
                anchors.fill: parent
                anchors.leftMargin: Kirigami.Units.gridUnit
                anchors.rightMargin: Kirigami.Units.gridUnit
                // toolbar spacing (HIG: mediumSpacing between buttons in a bar)
                spacing: Kirigami.Units.mediumSpacing

                Kirigami.Icon {
                    source: view.unsavedChanges ? "dialog-warning" : "dialog-ok-apply"
                    implicitWidth: Kirigami.Units.iconSizes.small
                    implicitHeight: Kirigami.Units.iconSizes.small
                    opacity: view.unsavedChanges ? 1.0 : 0.55
                }

                Controls.Label {
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                    text: view.unsavedChanges
                          ? "Unsaved changes - press Save settings"
                          : (backend.saveMessage || "Nothing to save")
                    color: !view.unsavedChanges && !backend.saveOk
                           ? Kirigami.Theme.negativeTextColor
                           : (view.unsavedChanges ? Kirigami.Theme.neutralTextColor
                                         : Kirigami.Theme.textColor)
                }

                Controls.Button {
                    id: revertButton
                    text: "Revert"
                    icon.name: "document-revert"
                    enabled: view.unsavedChanges
                    onClicked: view.reset()
                }

                Controls.Button {
                    id: saveButton
                    objectName: "saveButton"
                    text: "Save Settings"
                    icon.name: "document-save"
                    highlighted: view.unsavedChanges
                    enabled: view.unsavedChanges && view.validationText === ""
                    onClicked: view.save()
                }
            }
        }
    }
}
