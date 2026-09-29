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

[Tasks]
Name: "demarrage"; Description: "Lancer le correcteur à l'ouverture de session (conseillé)"
Name: "bureau"; Description: "Créer un raccourci sur le Bureau"; Flags: unchecked

[Files]
Source: "..\dist\Correcteur\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Correcteur"; Filename: "{app}\Correcteur.exe"
Name: "{userdesktop}\Correcteur"; Filename: "{app}\Correcteur.exe"; Tasks: bureau

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Correcteur"; \
  ValueData: """{app}\Correcteur.exe"" --demarrage"; Tasks: demarrage; Flags: uninsdeletevalue

[Run]
Filename: "{app}\Correcteur.exe"; Description: "Lancer le correcteur maintenant"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\correcteur-cli.exe"; Parameters: "quitter"; Flags: runhidden; RunOnceId: "QuitterCorrecteur"
Filename: "{app}\correcteur-cli.exe"; Parameters: "demarrage non"; Flags: runhidden; RunOnceId: "SansDemarrage"
