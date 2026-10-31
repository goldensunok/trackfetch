; Inno Setup script for Trackfetch.
;
; Produces TrackfetchSetup.exe, which installs Trackfetch.exe with a Start Menu
; entry and an optional desktop shortcut. Installing per user by default means
; no administrator prompt appears.

#define AppName "Trackfetch"
#define AppVersion "1.0.4"
#define AppPublisher "Trackfetch"
#define AppExeName "Trackfetch.exe"

[Setup]
AppId={{7B2F5A64-7C1E-4C2D-9E0B-2F4A9C6E1D33}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir=..\dist\installer
OutputBaseFilename=TrackfetchSetup
SetupIconFile=..\trackfetch\resources\trackfetch.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AppPublisherURL=https://github.com/
AppSupportURL=https://github.com/
AppUpdatesURL=https://github.com/

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Put a Trackfetch shortcut on my desktop"; \
  GroupDescription: "Shortcuts:"; Flags: checkedonce

[Files]
Source: "..\dist\Trackfetch\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\Trackfetch\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README_FOR_TOBIAS.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Read me first"; Filename: "{app}\README_FOR_TOBIAS.txt"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Open Trackfetch now"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only the program is removed. Settings, the download list and any files that
; have been downloaded are deliberately left alone.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
