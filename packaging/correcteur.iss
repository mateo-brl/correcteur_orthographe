; Installateur Windows (Inno Setup 6). Construit par la CI :
;   iscc /DMyAppVersion=0.1.0 packaging\correcteur.iss
; Installation par utilisateur, sans droits administrateur.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{8F3C2B1E-5A7D-4C9E-9B2A-6D1E0F4A3C21}
AppName=Correcteur
AppVersion={#MyAppVersion}
AppVerName=Correcteur {#MyAppVersion}
AppPublisher=Correcteur
AppPublisherURL=https://github.com/mateo-brl/correcteur_orthographe
DefaultDirName={localappdata}\Programs\Correcteur
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Correcteur-{#MyAppVersion}-installation-windows
SetupIconFile=icone.ico
UninstallDisplayIcon={app}\Correcteur.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Messages]
; Le texte d'origine parle des « icônes créées sur le Bureau », alors qu'aucune ne l'est sans la case cochée.
FinishedLabel=L'installation de [name] est terminée. Lancez-le depuis le menu Démarrer : son icône se place ensuite dans la zone de notification, près de l'horloge.

[Tasks]
Name: "demarrage"; Description: "Lancer le correcteur à l'ouverture de session (conseillé)"
Name: "bureau"; Description: "Créer un raccourci sur le Bureau"; Flags: unchecked

[Files]
Source: "..\dist\Correcteur\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Même identifiant que l'application (SetCurrentProcessExplicitAppUserModelID) : Windows l'associe au raccourci.
Name: "{userprograms}\Correcteur"; Filename: "{app}\Correcteur.exe"; AppUserModelID: "Correcteur.App"
Name: "{userdesktop}\Correcteur"; Filename: "{app}\Correcteur.exe"; AppUserModelID: "Correcteur.App"; Tasks: bureau

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Correcteur"; \
  ValueData: """{app}\Correcteur.exe"" --demarrage"; Tasks: demarrage; Flags: uninsdeletevalue

[Run]
Filename: "{app}\Correcteur.exe"; Description: "Lancer le correcteur maintenant"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\correcteur-cli.exe"; Parameters: "quitter"; Flags: runhidden; RunOnceId: "QuitterCorrecteur"
Filename: "{app}\correcteur-cli.exe"; Parameters: "demarrage non"; Flags: runhidden; RunOnceId: "SansDemarrage"

[Code]
// Désinstallation : les paramètres, le dictionnaire personnel, les journaux et les moteurs téléchargés
// (LanguageTool : près de 400 Mo) sont dans %LOCALAPPDATA%\Correcteur. On demande avant de les supprimer ;
// sans réponse possible (désinstallation silencieuse), on les garde.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  Data := ExpandConstant('{localappdata}\Correcteur');
  if UninstallSilent or not DirExists(Data) then
    exit;
  if MsgBox('Supprimer aussi vos paramètres, votre dictionnaire personnel et les moteurs téléchargés ?' + #13#10#13#10 +
            'Dossier : ' + Data + #13#10 +
            'Le moteur LanguageTool téléchargé pour le mode « Sur ce PC » peut occuper près de 400 Mo.' + #13#10#13#10 +
            'Choisissez Non pour les garder en vue d''une réinstallation.',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(Data, True, True, True);
end;
