#ifndef AppVersion
  #define AppVersion "0.2.1"
#endif
#ifndef BundleRoot
  #error BundleRoot must point to the validated application bundle
#endif
#ifndef OutputRoot
  #define OutputRoot "..\release"
#endif

[Setup]
AppId={{714AB1A3-D344-49B0-A734-342EC47E8C68}
AppName=Codex SOTA
AppVersion={#AppVersion}
AppPublisher=Codex SOTA contributors
AppPublisherURL=https://github.com/a2138609898-source/claude-codex-router
AppSupportURL=https://github.com/a2138609898-source/claude-codex-router/issues
AppUpdatesURL=https://github.com/a2138609898-source/claude-codex-router/releases
DefaultDirName={localappdata}\Programs\Codex SOTA
DefaultGroupName=Codex SOTA
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputRoot}
OutputBaseFilename=codex-sota-{#AppVersion}-windows-x64-setup
SetupIconFile=..\CodexSotaManager\codex-sota.ico
UninstallDisplayIcon={app}\codex-sota.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
LicenseFile=..\LICENSE
VersionInfoVersion={#AppVersion}.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
#if FileExists(AddBackslash(CompilerPath) + "Languages\ChineseSimplified.isl")
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#BundleRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Codex SOTA"; Filename: "{app}\codex-sota.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Codex SOTA"; Filename: "{app}\codex-sota.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{group}\Uninstall Codex SOTA"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\codex-sota.exe"; Description: "Launch Codex SOTA"; Flags: nowait postinstall skipifsilent

; Profiles and DPAPI credentials live under the user's own profile roots. Never
; delete or overwrite them during installation, upgrades, or uninstallation.
