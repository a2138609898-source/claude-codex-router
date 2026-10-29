Codex SOTA for Windows 10/11 (x64)

Download the setup.exe or portable.zip from the project's GitHub Releases.
The application includes its Python runtime. No Python, Node.js, Git, or
administrator account is needed to run it.

Installer: double-click setup.exe. It installs for the current Windows user
under LocalAppData\Programs\Codex SOTA and creates a Start menu shortcut.
Install a newer setup.exe in the same location to upgrade. Uninstallation
removes the application; personal profiles and credentials remain available.

Portable: extract the entire ZIP to a writable folder, then double-click
codex-sota.exe. Keep the _internal directory and codex-sota-runtime.exe beside
the main executable. Move the entire folder together.

First launch: select Codex App or Claude Desktop, add your own provider,
endpoint, API key, and models, then save and apply. Install the corresponding
official Codex or Claude desktop application separately before launching it
from the manager.

Configuration stays under %USERPROFILE%\.codex-sota or .claude-sota. Secrets
use Windows DPAPI and cannot be copied to a different Windows user or computer.
On another computer, install the application and enter your own credentials.
Do not share these personal profile folders with other people.

Checksums: SHA256SUMS.txt contains SHA-256 hashes for the downloads. In
PowerShell, run Get-FileHash -Algorithm SHA256 <downloaded-file> to compare.

Source and issues:
https://github.com/a2138609898-source/claude-codex-router
