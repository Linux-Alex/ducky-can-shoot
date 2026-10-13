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
*/
Controls.ScrollView {
    id: view

    signal installRequested()

    clip: true
    contentWidth: availableWidth

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
    }

    function splitEmails(text) {
        return text.split(/[,;\s]+/).filter(part => part.indexOf("@") > 0);
    }

    function save() {
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
        if (smtpPassword !== "") {
            payload.email.password = smtpPassword;
        }
        backend.saveSettings(payload);
    }

    Component.onCompleted: reset()

    Connections {
        target: backend
        function onSettingsChanged() {
            view.reset();
        }
        function onTestResult(what, ok, message) {
            testOutput.type = ok ? Kirigami.MessageType.Positive : Kirigami.MessageType.Error;
            testOutput.text = what + ": " + message;
            testOutput.visible = true;
        }
    }

    ColumnLayout {
        width: view.availableWidth
        spacing: Kirigami.Units.largeSpacing

        Item {
            Layout.preferredHeight: Kirigami.Units.largeSpacing
        }

        Kirigami.Heading {
            Layout.leftMargin: Kirigami.Units.gridUnit
            level: 1
            text: "Settings"
        }

        Controls.Label {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            opacity: 0.75
            text: "Stored in " + backend.configPath + ". Secrets stay out of the file: set "
                  + "DCS_SMTP_PASSWORD, DCS_GOFILE_TOKEN or DCS_PIXELDRAIN_API_KEY in the environment instead."
        }

        Kirigami.InlineMessage {
            id: testOutput
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.fillWidth: true
            visible: false
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
                            text: "Install ClamAV..."
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
                            text: "Test provider"
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

                    Controls.TextField {
                        Kirigami.FormData.label: "Password:"
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 18
                        echoMode: TextInput.Password
                        placeholderText: "leave empty to keep the stored value"
                        onTextEdited: view.smtpPassword = text
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
                            text: "Test SMTP"
                            icon.name: "network-connect"
                            onClicked: backend.testSmtp()
                        }
                    }
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
                            text: "Open config file"
                            icon.name: "document-open"
                            onClicked: backend.openConfig()
                        }

                        Controls.Button {
                            text: "Open report folder"
                            icon.name: "folder-open"
                            onClicked: backend.openPath(backend.settings.report.directory !== ""
                                                        ? backend.settings.report.directory
                                                        : "")
                        }
                    }
                }
            }
        }

        RowLayout {
            Layout.leftMargin: Kirigami.Units.gridUnit
            Layout.rightMargin: Kirigami.Units.gridUnit
            Layout.bottomMargin: Kirigami.Units.gridUnit * 2
            Layout.fillWidth: true
            spacing: Kirigami.Units.largeSpacing

            Item {
                Layout.fillWidth: true
            }

            Controls.Button {
                text: "Revert"
                icon.name: "document-revert"
                onClicked: view.reset()
            }

            Controls.Button {
                text: "Save settings"
                icon.name: "document-save"
                highlighted: true
                onClicked: view.save()
            }
        }
    }
}
