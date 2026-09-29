# Installe Correcteur depuis les sources pour l'utilisateur courant (sans droits administrateur).
# Pour une installation classique, préférez l'installateur Correcteur-...-installation-windows.exe
# des versions publiées sur GitHub.
#
#   powershell -ExecutionPolicy Bypass -File scripts\installer-windows.ps1

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Base = Join-Path $env:LOCALAPPDATA "Correcteur"
$Venv = Join-Path $Base "venv"

Write-Host "Création de l'environnement Python dans $Venv"
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m venv $Venv
} else {
    & python -m venv $Venv
}
& "$Venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$Venv\Scripts\python.exe" -m pip install --quiet $Repo

Write-Host "Téléchargement de Grammalecte (6 Mo)"
& "$Venv\Scripts\correcteur.exe" installer grammalecte

Write-Host "Raccourci dans le menu Démarrer"
$Programs = [Environment]::GetFolderPath("Programs")
$Shell = New-Object -ComObject WScript.Shell
$Link = $Shell.CreateShortcut((Join-Path $Programs "Correcteur.lnk"))
$Link.TargetPath = "$Venv\Scripts\pythonw.exe"
$Link.Arguments = "-m correcteur"
$Link.Description = "Correcteur d'orthographe et de grammaire"
$Link.Save()

& "$Venv\Scripts\correcteur.exe" demarrage oui
Write-Host "Lancement automatique à l'ouverture de session activé"

Start-Process -FilePath "$Venv\Scripts\pythonw.exe" -ArgumentList "-m", "correcteur"
Write-Host "Terminé. Sélectionnez un texte puis Ctrl+Alt+C pour le vérifier."
