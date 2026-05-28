; Inno Setup script -- Commander Builder Windows installer (FP-010 slice 4)
;
; Build (after PyInstaller one-folder dist exists):
;   python scripts/build_installer.py
; or directly:
;   "C:\Users\pilot\AppData\Local\Programs\Inno Setup 6\ISCC.exe" packaging\commander-builder.iss
;
; Output: dist\installer\CommanderBuilder-Setup.exe
;
; Version: bump AppVersion (and AppVerName) to match pyproject.toml on each release.

#define AppVersion "0.2.0"
#define AppName    "Commander Builder"
#define AppPublisher "LlamaAdam"
#define AppExeName "CommanderBuilder.exe"
; Source folder produced by PyInstaller (relative to the .iss file's location,
; which is packaging\; so ..\ is the repo root).
#define DistDir "..\dist\CommanderBuilder"

[Setup]
AppId={{A3F8D2C1-7E4B-4A9F-8B3D-2C6E1F0A5D8C}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
; Per-user install: no admin rights required.
PrivilegesRequired=lowest
; Install into %LOCALAPPDATA%\Programs\CommanderBuilder by default.
DefaultDirName={userpf}\CommanderBuilder
DefaultGroupName={#AppName}
; Allow the user to disable the desktop icon during install.
AllowNoIcons=yes
; Output
OutputDir=..\dist\installer
OutputBaseFilename=CommanderBuilder-Setup
; No compression algorithm is specified so Inno uses its default (LZMA2).
Compression=lzma2/ultra
SolidCompression=yes
; Disable start-menu dir page since we create only one shortcut.
DisableProgramGroupPage=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Bundle the entire one-folder PyInstaller output recursively.
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
