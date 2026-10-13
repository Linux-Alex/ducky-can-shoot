# Threat model

What Ducky Can Shoot defends against, what it explicitly does not, and what a clean verdict
can and cannot support.

[Back to the README](../README.md)

DCS defends against a **medium you have already decided not to trust**. It tells
you whether a stick carries malware or attack tooling, and it produces a
transferable image of that stick for somebody else to inspect.

It defends against an attacker who controls the *contents* of the medium: hostile
file names, hostile documents, hostile scripts, an autorun.inf, a fake "PDF" that
is really an executable, an archive that expands to fill the disk, or a
Duckyscript payload aimed at the human who plugs the stick in.

It does **not** defend against an attacker who controls the machine running DCS
(DCS is an ordinary user process with no sandbox and no privilege separation), an
attacker who controls the network path to the upload host or the mail server (the
TLS of those services is the only protection there), a kernel or filesystem
exploit that triggers merely by attaching the device before DCS starts, or a
compromised or malicious upload provider.

A clean verdict means "no engine found anything", not "this medium is safe". The
full statement, including what counts as supported, is in
[SECURITY.md](../SECURITY.md).

---

