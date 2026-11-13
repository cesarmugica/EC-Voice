; ============================================================
;  EC-Voice — instalador de Windows (Inno Setup 6)
;  Lo compila windows\build.ps1. Instala por usuario (sin admin).
;  El motor de IA (~4 GB) se descarga desde la app la primera vez y vive
;  en %LOCALAPPDATA%\EC-Voice\engine. Al desinstalar se pregunta si se
;  borra también (junto con las voces).
; ============================================================
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "dist\ecvoice"
#endif

[Setup]
AppId={{6A9E2F31-4C8B-4D7E-9B21-EC70C1CE0001}
AppName=EC-Voice
AppVersion={#AppVersion}
AppVerName=EC-Voice {#AppVersion}
AppPublisher=Cesar Mugica
DefaultDirName={localappdata}\Programs\EC-Voice
DefaultGroupName=EC-Voice
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=EC-Voice-Setup-{#AppVersion}
SetupIconFile=..\src\web\img\ecvoice.ico
UninstallDisplayIcon={app}\ecvoice.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear acceso directo «EC-Voice» en el escritorio"; GroupDescription: "Accesos directos:"
Name: "vbcable"; Description: "Instalar el cable virtual VB-CABLE (de VB-Audio, www.vb-cable.com · donationware). Pide permiso de administrador y luego hay que reiniciar."; GroupDescription: "Audio hacia TikTok LIVE Studio:"; Check: not CableInstalled

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\EC-Voice"; Filename: "{app}\ecvoice.exe"; Comment: "Tu voz en vivo con el timbre de otra"
Name: "{group}\Carpeta de datos de EC-Voice"; Filename: "{localappdata}\EC-Voice"
Name: "{group}\Desinstalar EC-Voice"; Filename: "{uninstallexe}"
Name: "{userdesktop}\EC-Voice"; Filename: "{app}\ecvoice.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ecvoice.exe"; Description: "Abrir EC-Voice"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM ecvoice.exe /T /F"; Flags: runhidden; RunOnceId: "StopEcVoice"

[Code]
var CableJustInstalled: Boolean;

function CableInstalled(): Boolean;
var F: TFindRec;
begin
  Result := FindFirst(ExpandConstant('{sys}\drivers\vbaudio_cable*.sys'), F);
  if Result then FindClose(F);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var R: Integer;
begin
  if CurStep = ssInstall then
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM ecvoice.exe /T /F', '', SW_HIDE, ewWaitUntilTerminated, R);
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('vbcable') then
  begin
    { El instalador de VB-CABLE necesita administrador: Windows pide permiso y luego confirma el controlador. }
    if ShellExec('runas', ExpandConstant('{app}\vbcable\VBCABLE_Setup_x64.exe'), '-i -h', ExpandConstant('{app}\vbcable'),
                 SW_SHOWNORMAL, ewWaitUntilTerminated, R) then
      CableJustInstalled := True
    else
      MsgBox('No se instaló el cable virtual (' + SysErrorMessage(R) + '). Puedes instalarlo después desde EC-Voice.', mbInformation, MB_OK);
  end;
end;

function NeedRestart(): Boolean;
begin
  Result := CableJustInstalled;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    if MsgBox('¿Borrar también el motor de IA (~6 GB) y tus voces guardadas?' + #13#10 +
              '(%LOCALAPPDATA%\EC-Voice)', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{localappdata}\EC-Voice'), True, True, True);
end;
