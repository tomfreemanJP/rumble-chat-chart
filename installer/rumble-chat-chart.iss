; Inno Setup script for RumbleLog.
; Built by ..\build.ps1 - run that rather than compiling this directly, since it
; expects the frozen output in ..\dist to already exist.

#define AppName      "RumbleLog"
#define AppVersion   "1.0.0"
#define AppPublisher "RumbleLog"
#define CliExe       "rumblelog.exe"
#define GuiExe       "rumblelogw.exe"

[Setup]
; Keep this GUID stable forever - it is how Windows recognises an upgrade
; of the same product rather than a second installation alongside it.
AppId={{8B3F2A14-6C5E-4D7B-9A21-3E8D5C1F7B40}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 'lowest' keeps this a per-user install: no UAC prompt, and the scheduled task
; runs as the same user that owns the API key.
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=rumblelog-setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\cli\{#CliExe}
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\rumblelog\*";  DestDir: "{app}\cli"; Flags: recursesubdirs ignoreversion
Source: "..\dist\rumblelogw\*"; DestDir: "{app}\gui"; Flags: recursesubdirs ignoreversion
Source: "..\README.md";         DestDir: "{app}";     Flags: ignoreversion isreadme
Source: "..\queries.sql";       DestDir: "{app}";     Flags: ignoreversion

[Icons]
; Console shortcuts open via cmd /k so the window stays up long enough to read.
Name: "{group}\Leaderboards";     Filename: "{cmd}"; Parameters: "/k """"{app}\cli\{#CliExe}"" leaderboard"""; Comment: "Rank viewers by chat, donations, gifts and tenure"
Name: "{group}\Status";           Filename: "{cmd}"; Parameters: "/k """"{app}\cli\{#CliExe}"" status"""; Comment: "What has been captured so far"
Name: "{group}\Set API key";      Filename: "{app}\gui\{#GuiExe}"; Parameters: "configure"; Comment: "Paste your Rumble Live Stream API URL"
Name: "{group}\Data folder";      Filename: "{localappdata}\RumbleLog"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Run]
; Order matters: write the key first so the service has something to poll.
Filename: "{app}\gui\{#GuiExe}"; Parameters: "configure --url ""{code:GetApiUrl}"""; \
    Check: HasApiUrl; StatusMsg: "Checking your API key..."
Filename: "{app}\cli\{#CliExe}"; Parameters: "install-task"; Flags: runhidden; \
    StatusMsg: "Registering the background service..."
Filename: "{cmd}"; Parameters: "/k """"{app}\cli\{#CliExe}"" status"""; \
    Description: "Show what RumbleLog is doing"; Flags: postinstall skipifsilent nowait unchecked

[UninstallRun]
Filename: "{app}\cli\{#CliExe}"; Parameters: "uninstall-task"; Flags: runhidden; \
    RunOnceId: "RemoveScheduledTask"

[Code]
var
  ApiPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  ApiPage := CreateInputQueryPage(wpSelectDir,
    'Rumble API key',
    'Where should RumbleLog get your stream data?',
    'Open Rumble in your browser, go to Account Settings ' + #8594 + ' API, and copy the' + #13#10 +
    'Live Stream API URL. Paste it below.' + #13#10#13#10 +
    'You can leave this blank and set it later from the Start Menu, but nothing' + #13#10 +
    'will be captured until you do.');
  ApiPage.Add('Live Stream API URL:', False);
end;

function GetApiUrl(Param: String): String;
begin
  Result := Trim(ApiPage.Values[0]);
end;

function HasApiUrl: Boolean;
begin
  Result := Trim(ApiPage.Values[0]) <> '';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Url: String;
begin
  Result := True;
  if CurPageID = ApiPage.ID then
  begin
    Url := Trim(ApiPage.Values[0]);
    if (Url <> '') and (Pos('http', Lowercase(Url)) <> 1) then
    begin
      MsgBox('That does not look like a URL.' + #13#10#13#10 +
             'It should start with http and come from Rumble' + #39 + 's Account Settings ' +
             #8594 + ' API page.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;
