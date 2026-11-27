; Inno Setup 6 Script for Vaani
; Produces dist/Vaani-Setup.exe
; Installs per-user to {localappdata}\Vaani (no admin UAC elevation required)

#define MyAppName "Vaani"
#define MyDisplayName "Vaani"  ; shown in the wizard and Apps list; folder and shortcuts keep MyAppName
#define MyAppVersion "1.7.0"
#define MyAppPublisher "Vaani"
#define MyAppURL "https://sachin1724.github.io/Vaani/"
#define MyAppExeName "Vaani.exe"

[Setup]
AppId={{D1A3F5B8-7E92-4C61-B9D4-4328E0B5B721}
AppName={#MyDisplayName}
AppVersion={#MyAppVersion}
AppVerName={#MyDisplayName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL=https://github.com/Sachin1724/Vaani/issues
AppUpdatesURL=https://github.com/Sachin1724/Vaani/releases
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=..\dist
OutputBaseFilename=Vaani-Setup
SetupIconFile=Vaani.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
CloseApplications=force
CloseApplicationsFilter=*Vaani*,*pythonw*
RestartApplications=no
DisableDirPage=auto
DisableProgramGroupPage=auto
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyDisplayName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Vaani\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs restartreplace

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[InstallDelete]
; autostart is the app's own setting (Run key); an old Startup shortcut launched it twice at boot
Type: files; Name: "{userstartup}\{#MyAppName}.lnk"
; the binary from before the rename to Vaani
Type: files; Name: "{app}\OpenFlow.exe"


[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent runasoriginaluser; WorkingDir: "{app}"
Filename: "{app}\{#MyAppExeName}"; Flags: nowait runasoriginaluser; WorkingDir: "{app}"; Check: WizardSilent


[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  // Forcibly terminate any running Vaani instances so executables and DLLs are never locked
  Exec('cmd.exe', '/c taskkill /f /im Vaani.exe /im OpenFlow.exe >nul 2>&1', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(300);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    Exec('cmd.exe', '/c taskkill /f /im Vaani.exe /im OpenFlow.exe >nul 2>&1', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(250);
  end;
end;
