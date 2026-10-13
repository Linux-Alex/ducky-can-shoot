"""Signature data for the built-in heuristic engine.

Everything here is data, not logic: extension sets, indicator tables and
regexes.  The engine in :mod:dcs.core.scanner.heuristic consumes it, which
keeps the detection logic readable and the indicator lists easy to review,
extend and cite in the report.

The lists are deliberately conservative: an indicator only becomes a finding
when the surrounding context makes it meaningful (for example a lone
"powershell" word inside a text file is not a finding, but a PowerShell
command line with -EncodedCommand and a base64 blob is a critical one).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# EICAR (assembled at runtime so this file never trips a real antivirus)
# ---------------------------------------------------------------------------

EICAR_MARKER = b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE"
EICAR_SIGNATURE = (
    b"X5O!P%@AP[4" + bytes([92]) + b"PZX54(P^)7CC)7}$" + EICAR_MARKER + b"!$H+H*"
)

# ---------------------------------------------------------------------------
# extensions
# ---------------------------------------------------------------------------

EXECUTABLE_EXTENSIONS = frozenset(
    {
        ".exe", ".com", ".scr", ".pif", ".cpl", ".msi", ".msp", ".mst", ".dll",
        ".sys", ".drv", ".ocx", ".efi", ".bin", ".elf", ".app", ".apk",
        ".jar", ".jnlp", ".deb", ".rpm", ".dmg", ".vbs", ".vbe", ".js", ".jse",
        ".wsf", ".wsh", ".wsc", ".hta", ".ps1", ".psm1", ".psd1", ".bat", ".cmd",
        ".btm", ".sh", ".bash", ".zsh", ".run", ".py", ".pl", ".rb", ".lnk",
        ".url", ".reg", ".msc", ".gadget", ".inf", ".scf", ".xll", ".docm",
        ".xlsm", ".pptm", ".dotm", ".xltm", ".slk", ".iqy", ".pub",
    }
)

#: extensions that look harmless but are used for double-extension tricks
DECOY_EXTENSIONS = frozenset(
    {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf",
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".tif", ".tiff",
        ".mp3", ".mp4", ".avi", ".mkv", ".mov", ".wav", ".flac", ".zip", ".rar",
        ".7z", ".csv", ".json", ".xml", ".html", ".htm", ".iso", ".img", ".log",
    }
)

#: extensions that are dangerous when they sit alone in the drive root
ROOT_SUSPICIOUS_EXTENSIONS = frozenset(
    {".exe", ".com", ".scr", ".pif", ".cpl", ".msi", ".bat", ".cmd", ".vbs", ".vbe",
     ".js", ".jse", ".wsf", ".hta", ".ps1", ".jar", ".lnk"}
)

#: interpreted formats that are commonly abused
SCRIPT_EXTENSIONS = frozenset(
    {
        ".ps1", ".psm1", ".psd1", ".bat", ".cmd", ".btm", ".vbs", ".vbe", ".js",
        ".jse", ".wsf", ".wsh", ".hta", ".sh", ".bash", ".zsh", ".ksh", ".py",
        ".pl", ".rb", ".php", ".asp", ".aspx", ".jsp", ".lua", ".tcl",
    }
)

TEXT_EXTENSIONS = frozenset(
    {
        ".txt", ".log", ".md", ".ini", ".cfg", ".conf", ".json", ".xml", ".csv",
        ".tsv", ".yml", ".yaml", ".toml", ".sh", ".bash", ".ps1", ".bat", ".cmd",
        ".vbs", ".js", ".py", ".pl", ".rb", ".php", ".html", ".htm", ".css",
        ".sql", ".c", ".h", ".cpp", ".java", ".cs", ".go", ".rs", ".ducky",
        ".duck", ".kbd", ".reg", ".inf", ".url", ".vba", ".bas", ".cls", ".frm",
    }
)

ARCHIVE_EXTENSIONS = frozenset(
    {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".cab", ".iso", ".img", ".dmg"}
)

# ---------------------------------------------------------------------------
# file name indicators
# ---------------------------------------------------------------------------

#: (regex, threat, severity) evaluated against the basename
SUSPICIOUS_NAME_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"^autorun\.inf$", re.I), "autorun.inf boot script", "medium"),
    (re.compile(r"[\u202a-\u202e\u2066-\u2069]"), "unicode bidi override in file name", "high"),
    (re.compile(r"^\s*\.\.", re.I), "name starts with multiple dots", "low"),
    (re.compile(r"\.(lnk|url)$", re.I), "shortcut file", "low"),
)

#: keywords in a file name that strongly suggest offensive tooling
MALWARE_NAME_KEYWORDS: tuple[tuple[str, str, str], ...] = (
    ("mimikatz", "credential dumper", "critical"),
    ("lazagne", "credential dumper", "critical"),
    ("pwdump", "credential dumper", "critical"),
    ("wce.exe", "credential dumper", "critical"),
    ("procdump", "process memory dumper", "high"),
    ("keylog", "keystroke logger", "high"),
    ("ransom", "ransomware indicator", "high"),
    ("cryptolocker", "ransomware family", "critical"),
    ("wannacry", "ransomware family", "critical"),
    ("petya", "ransomware family", "critical"),
    ("notpetya", "ransomware family", "critical"),
    ("lockbit", "ransomware family", "critical"),
    ("revil", "ransomware family", "critical"),
    ("emisisoft", "ransomware family", "critical"),
    ("nc.exe", "netcat backdoor tool", "high"),
    ("ncat", "netcat backdoor tool", "high"),
    ("netcat", "netcat backdoor tool", "high"),
    ("socat", "relay/backdoor tool", "medium"),
    ("psexec", "remote execution tool", "high"),
    ("paexec", "remote execution tool", "high"),
    ("cobaltstrike", "command and control framework", "critical"),
    ("beacon.exe", "command and control implant", "critical"),
    ("meterpreter", "post exploitation framework", "critical"),
    ("shellcode", "raw shellcode", "high"),
    ("payload.bin", "generic payload", "medium"),
    ("inject.bin", "HID injection payload", "high"),
    ("ngrok", "tunnelling tool", "medium"),
    ("chisel", "tunnelling tool", "medium"),
    ("frpc", "tunnelling tool", "medium"),
    ("hacktool", "hacking tool", "high"),
    ("crack.exe", "software cracker", "medium"),
    ("keygen", "software keygen", "medium"),
    ("patch.exe", "software patcher", "medium"),
)

#: names of files that suggest a physical-payload (Rubber Ducky style) attack
DUCKY_NAME_PATTERNS = (
    "payload.txt",
    "ducky.txt",
    "duckyscript.txt",
    "payload.bin",
    "inject.bin",
    "duckencoder",
    "rubberducky",
    "badusb",
    "hid_payload",
)

# ---------------------------------------------------------------------------
# Rubber Ducky / HID payload syntax
# ---------------------------------------------------------------------------

#: DuckyScript command verbs (Duck Encoder 2/3 compatible)
DUCKY_COMMANDS = frozenset(
    {
        "STRING", "STRINGLN", "DELAY", "DEFAULTDELAY", "DEFAULT_DELAY", "REPEAT",
        "REM", "REM_BLOCK", "END_REM", "GUI", "WINDOWS", "COMMAND", "CTRL", "CONTROL",
        "ALT", "SHIFT", "ENTER", "RETURN", "ESC", "ESCAPE", "TAB", "SPACE", "BACKSPACE",
        "DELETE", "INSERT", "HOME", "END", "PAGEUP", "PAGEDOWN", "UPARROW", "DOWNARROW",
        "LEFTARROW", "RIGHTARROW", "CAPSLOCK", "NUMLOCK", "SCROLLLOCK", "PRINTSCREEN",
        "PAUSE", "BREAK", "MENU", "APP", "F1", "F2", "F3", "F4", "F5", "F6", "F7",
        "F8", "F9", "F10", "F11", "F12", "HOLD", "RELEASE", "MOUSE", "WAIT_FOR_BUTTON_PRESS",
        "LED", "ATTACKMODE", "VID", "PID", "MAN", "SERIAL", "STORAGE", "RNDIS_ETHERNET",
    }
)

DUCKY_DANGEROUS_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("powershell", "runs PowerShell"),
    ("-enc", "uses an encoded command"),
    ("-e ", "uses an encoded command"),
    ("encodedcommand", "uses an encoded command"),
    ("cmd.exe", "opens a command prompt"),
    ("cmd /c", "opens a command prompt"),
    ("cmd /k", "opens a command prompt"),
    ("invoke-webrequest", "downloads a remote payload"),
    ("downloadstring", "downloads a remote payload"),
    ("downloadfile", "downloads a remote payload"),
    ("certutil", "uses certutil to fetch or decode a payload"),
    ("bitsadmin", "uses bitsadmin to fetch a payload"),
    ("mshta", "executes an HTA payload"),
    ("rundll32", "executes code through rundll32"),
    ("regsvr32", "executes code through regsvr32"),
    ("wmic", "uses WMI for execution"),
    ("reg add", "modifies the registry"),
    ("reg delete", "modifies the registry"),
    ("schtasks", "creates a scheduled task"),
    ("net user", "manipulates user accounts"),
    ("net localgroup", "manipulates group membership"),
    ("netsh", "reconfigures networking or the firewall"),
    ("firewall", "touches firewall configuration"),
    ("defender", "touches Defender configuration"),
    ("amsi", "attempts an AMSI bypass"),
    ("mimikatz", "dumps credentials"),
    ("keylog", "installs a keystroke logger"),
    ("curl ", "downloads content over the network"),
    ("wget ", "downloads content over the network"),
    ("start-process", "starts a process"),
    ("-windowstyle hidden", "hides its window"),
    ("-w hidden", "hides its window"),
    ("frombase64string", "decodes an embedded payload"),
    ("iex", "executes downloaded code"),
)

# ---------------------------------------------------------------------------
# script content indicators
# ---------------------------------------------------------------------------

#: (regex, threat, severity, description) -- applied to decoded text
SCRIPT_INDICATORS: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (
        re.compile(r"(?:-enc|-encodedcommand|-e)\s+[A-Za-z0-9+/]{80,}={0,2}", re.I),
        "PowerShell encoded command",
        "critical",
        "A base64 encoded PowerShell command of at least 80 characters. This is the "
        "single most common fileless execution trick.",
    ),
    (
        re.compile(r"\bIEX\b|\bInvoke-Expression\b", re.I),
        "PowerShell dynamic execution",
        "high",
        "Invoke-Expression executes a string as code and is typical for stagers.",
    ),
    (
        re.compile(r"(?:DownloadString|DownloadFile|DownloadData)\s*\(", re.I),
        "PowerShell download cradle",
        "high",
        "The script downloads and executes remote content.",
    ),
    (
        re.compile(r"\bFromBase64String\s*\(", re.I),
        "embedded base64 payload",
        "high",
        "The script decodes an embedded base64 blob, usually a second stage.",
    ),
    (
        re.compile(r"-w(?:indowstyle)?\s+hidden|-nop\b|-noprofile\b|-exec(?:utionpolicy)?\s+bypass", re.I),
        "PowerShell evasion flags",
        "high",
        "Flags that hide the window and bypass the execution policy are a strong "
        "sign of an unattended payload.",
    ),
    (
        re.compile(r"\bNew-Object\s+Net\.WebClient\b", re.I),
        "PowerShell web client",
        "medium",
        "Net.WebClient is commonly used to pull a payload from the internet.",
    ),
    (
        re.compile(r"(?:curl|wget)[^\n|]{0,120}\|\s*(?:sudo\s+)?(?:ba)?sh\b", re.I),
        "pipe to shell remote execution",
        "high",
        "Content is downloaded and piped straight into a shell.",
    ),
    (
        re.compile(r"base64\s+(?:-d|--decode)[^\n|]{0,60}\|\s*(?:ba)?sh\b", re.I),
        "base64 piped into a shell",
        "high",
        "A decoded blob is executed, a classic way to hide a payload.",
    ),
    (
        re.compile(r"/dev/tcp/\d{1,3}(?:\.\d{1,3}){3}/\d+", re.I),
        "bash reverse shell",
        "critical",
        "The /dev/tcp pseudo device is used for reverse shells.",
    ),
    (
        re.compile(r"\bnc\b[^\n]{0,40}\s-e\s|\bncat\b[^\n]{0,40}\s-e\s", re.I),
        "netcat reverse shell",
        "critical",
        "Netcat with -e spawns a shell on a remote host.",
    ),
    (
        re.compile(r"(?:CreateObject|GetObject)\s*\(\s*[\x22\x27]WScript\.Shell[\x22\x27]", re.I),
        "WSH shell object",
        "high",
        "VBScript/JScript creating a WScript.Shell object can run arbitrary commands.",
    ),
    (
        re.compile(r"new\s+ActiveXObject\s*\(\s*[\x22\x27](?:WScript\.Shell|Shell\.Application)[\x22\x27]", re.I),
        "ActiveX shell object",
        "high",
        "A JScript/HTA file instantiating a shell ActiveX object.",
    ),
    (
        re.compile(r"\bmshta\b|\brundll32\b\s+\S+\.dll|\bregsvr32\b\s+/i", re.I),
        "living off the land execution",
        "high",
        "Signed Windows binaries are abused to execute a payload.",
    ),
    (
        re.compile(r"bitsadmin\s+/transfer|certutil\s+-urlcache|certutil\s+-decode", re.I),
        "file download via LOLBin",
        "high",
        "bitsadmin/certutil are used to fetch or decode a payload without a browser.",
    ),
    (
        re.compile(r"vssadmin\s+delete\s+shadows|wbadmin\s+delete\s+catalog|bcdedit\s+/set", re.I),
        "backup destruction",
        "critical",
        "Shadow copy or recovery destruction is a ransomware precursor.",
    ),
    (
        re.compile(r"net\s+(?:stop|start)\s+(?:windefend|wuauserv|bits|sharedaccess)|sc\s+(?:stop|config)\s+\w*defend", re.I),
        "security service tampering",
        "high",
        "The script stops or reconfigures a security service.",
    ),
    (
        re.compile(r"Set-MpPreference\s+-Disable|Add-MpPreference\s+-ExclusionPath", re.I),
        "Defender exclusion added",
        "critical",
        "Adding a Defender exclusion path is a common step before dropping malware.",
    ),
    (
        re.compile(r"\bpython\s+-c\s+[\x22\x27].{0,40}socket|socket\.socket\(\s*AF_INET", re.I),
        "raw socket usage",
        "medium",
        "Direct socket use in a script can be a backdoor or a reverse shell.",
    ),
    (
        re.compile(r"\bos\.system\s*\(|\bsubprocess\.(?:Popen|call|run)\s*\(", re.I),
        "process execution from a script",
        "low",
        "The script starts other processes; review it if the source is unknown.",
    ),
)

#: text that indicates a command-and-control endpoint
C2_URL_PATTERN = re.compile(r"https?://(?:\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|[\w.-]+\.(?:ru|cn|tk|top|xyz|onion|su|pw|cc|gq|ml|cf|ga|buzz|click|country|stream|download|zip|review|kim|men|date|racing|loan|win|bid|trade|webcam|science|party|gdn|mom|icu|lol)(?::\d+)?)", re.I)

#: registry keys abused for persistence or defence evasion
REGISTRY_INDICATORS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"CurrentVersion\\+Run(?:Once)?", re.I), "run-key persistence", "high"),
    (re.compile(r"CurrentVersion\\+Explorer\\+Shell\s+Folders", re.I), "shell folder hijack", "high"),
    (re.compile(r"DisableAntiSpyware|DisableAntiVirus|DisableRealtimeMonitoring", re.I), "antivirus disabled via registry", "critical"),
    (re.compile(r"EnableLUA\s*=\s*dword:00000000", re.I), "UAC disabled via registry", "high"),
    (re.compile(r"Winlogon\\+Shell\s*=", re.I), "winlogon shell replacement", "critical"),
    (re.compile(r"Image\s+File\s+Execution\s+Options", re.I), "IFEO hijack", "high"),
    (re.compile(r"AppInit_DLLs", re.I), "AppInit DLL injection", "high"),
    (re.compile(r"Software\\+Microsoft\\+Windows\\+CurrentVersion\\+Policies\\+System", re.I), "policy tampering", "medium"),
)

# ---------------------------------------------------------------------------
# binary indicators
# ---------------------------------------------------------------------------

#: Windows API names that indicate injection, keylogging or download behaviour
SUSPICIOUS_API_NAMES: tuple[tuple[str, str, str], ...] = (
    ("VirtualAllocEx", "remote memory allocation", "high"),
    ("WriteProcessMemory", "process injection", "critical"),
    ("CreateRemoteThread", "remote thread creation", "critical"),
    ("NtUnmapViewOfSection", "process hollowing", "critical"),
    ("SetWindowsHookEx", "global hook installation", "high"),
    ("GetAsyncKeyState", "keystroke capture", "critical"),
    ("GetKeyState", "keystroke capture", "high"),
    ("URLDownloadToFile", "file download", "high"),
    ("InternetOpenUrl", "file download", "medium"),
    ("WinExec", "process execution", "medium"),
    ("ShellExecute", "process execution", "medium"),
    ("CreateProcessA", "process creation", "low"),
    ("AdjustTokenPrivileges", "privilege escalation", "medium"),
    ("CryptEncrypt", "file encryption", "high"),
    ("IsDebuggerPresent", "anti-debugging", "medium"),
    ("CheckRemoteDebuggerPresent", "anti-debugging", "medium"),
    ("sprintf", "format string use", "low"),
)

#: section names left behind by packers/protectors
PACKER_MARKERS: tuple[tuple[bytes, str], ...] = (
    (b"UPX!", "UPX"),
    (b"UPX0", "UPX"),
    (b"UPX1", "UPX"),
    (b".aspack", "ASPack"),
    (b".adata", "ASPack"),
    (b".petite", "Petite"),
    (b".themida", "Themida"),
    (b".vmp0", "VMProtect"),
    (b".vmp1", "VMProtect"),
    (b".enigma1", "Enigma"),
    (b".mpress1", "MPRESS"),
    (b"pebundle", "PEBundle"),
)

#: PE file entropy above this value in the raw header region suggests packing
HIGH_ENTROPY_THRESHOLD = 7.2

#: strings that mark a PE as a .NET assembly
DOTNET_MARKER = b"mscoree.dll"

# ---------------------------------------------------------------------------
# archive indicators
# ---------------------------------------------------------------------------

#: a compressed archive whose expansion ratio exceeds this is a probable bomb
ZIP_BOMB_RATIO = 150
ZIP_BOMB_MIN_UNCOMPRESSED = 500 * 1024 * 1024  # 500 MiB

#: file names inside an archive that are interesting
INTERESTING_ARCHIVE_MEMBERS: tuple[str, ...] = (
    ".exe", ".dll", ".scr", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".hta",
    ".lnk", ".jar", ".hta", ".msi", ".reg",
)

# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------

#: how much of a file the engine reads for the cheap checks
HEAD_BYTES = 64 * 1024

#: how much of a script/office document is decoded for the text checks
DEEP_READ_BYTES = 2 * 1024 * 1024

#: PDFs get their own, slightly larger, budget
PDF_READ_BYTES = 4 * 1024 * 1024
