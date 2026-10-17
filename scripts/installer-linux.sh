#!/usr/bin/env bash
# Installe Correcteur pour l'utilisateur courant (aucun droit administrateur).
#
#  - depuis une version téléchargée (dossier contenant l'exécutable `correcteur`) ;
#  - ou depuis les sources (dépôt git) : environnement Python dédié.
#
# Variables : PREFIX (défaut ~/.local), SANS_DEMARRAGE=1 pour ne pas lancer
# le correcteur à l'ouverture de session.
set -euo pipefail

PREFIX="$(realpath -m "${PREFIX:-$HOME/.local}")"  # chemin absolu : il sert de cible aux liens
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$PREFIX/bin"
DEST="$PREFIX/opt/correcteur"
SOURCES=""
mkdir -p "$BIN" "$PREFIX/share/applications" "$PREFIX/share/icons/hicolor/scalable/apps"

info() { printf '\033[1;34m▶\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }

# Processus lancés depuis l'exécutable installé (test -ef suit le lien /proc/PID/exe, sans programme externe).
instance_running() {
    local exe
    for exe in /proc/[0-9]*/exe; do
        [ "$exe" -ef "$DEST/correcteur" ] && return 0
    done
    return 1
}

if [ -x "$HERE/correcteur" ] && [ -d "$HERE/_internal" ]; then
    if [ "$(realpath "$HERE")" = "$(realpath -m "$DEST")" ]; then
        # Relancé depuis la copie installée : la supprimer pour la recopier détruirait l'installation.
        info "Version empaquetée déjà en place dans $DEST : fichiers conservés"
    else
        info "Installation de la version empaquetée dans $DEST"
        mkdir -p "$PREFIX/opt"
        # Copie à côté d'abord : si elle échoue (disque plein...), l'ancienne version reste intacte.
        STAGING="$(mktemp -d "$PREFIX/opt/.correcteur-installation.XXXXXX")"
        trap 'if [ -n "${STAGING:-}" ]; then rm -rf "$STAGING"; fi' EXIT
        chmod "$(umask -S)" "$STAGING"  # mktemp crée le dossier en 700
        cp -r "$HERE/." "$STAGING"
        if "$BIN/correcteur" quitter >/dev/null 2>&1; then
            # On attend la fin de l'ancienne instance (5 s au plus) : ses fichiers vont disparaître, et le
            # correcteur relancé plus bas lui passerait la main au lieu de démarrer.
            for _ in $(seq 50); do
                instance_running || break
                sleep 0.1
            done
            if instance_running; then
                warn "L'ancienne instance du correcteur ne s'est pas arrêtée : elle sera remplacée quand même."
            fi
        fi
        rm -rf "$DEST"
        mv "$STAGING" "$DEST"
        STAGING=""
    fi
    ln -sf "$DEST/correcteur" "$BIN/correcteur"
    ICON_SRC="$DEST/_internal/correcteur/resources/icone.svg"
elif [ -f "$HERE/../pyproject.toml" ]; then
    REPO="$(cd "$HERE/.." && pwd)"
    VENV="$PREFIX/share/correcteur/venv"
    command -v python3 >/dev/null || { echo "Python 3.10+ est requis."; exit 1; }
    info "Installation depuis les sources ($REPO) dans $VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet "$REPO"
    info "Téléchargement de Grammalecte (6 Mo)"
    "$VENV/bin/correcteur" installer grammalecte
    ln -sf "$VENV/bin/correcteur" "$BIN/correcteur"
    ICON_SRC="$REPO/src/correcteur/resources/icone.svg"
    SOURCES=1
else
    echo "Lancez ce script depuis le dossier de la version téléchargée ou depuis scripts/ du dépôt."
    exit 1
fi

cp "$ICON_SRC" "$PREFIX/share/icons/hicolor/scalable/apps/correcteur.svg"
cat > "$PREFIX/share/applications/correcteur.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Correcteur
Comment=Correcteur d'orthographe et de grammaire
Exec=$BIN/correcteur
Icon=correcteur
Terminal=false
Categories=Utility;TextTools;
EOF

if [ -z "${SANS_DEMARRAGE:-}" ]; then
    "$BIN/correcteur" demarrage oui >/dev/null
    info "Lancement automatique à l'ouverture de session activé"
fi

# Dépendances système utiles selon la session.
session="${XDG_SESSION_TYPE:-}"
if [ "$session" = "wayland" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
    command -v wl-paste >/dev/null || warn "Wayland : installez wl-clipboard (ex. sudo apt install wl-clipboard)."
    if ! command -v wtype >/dev/null && ! command -v ydotool >/dev/null; then
        warn "Wayland : pour le remplacement automatique du texte, installez wtype (Sway, KDE) ou ydotool (GNOME)."
    fi
    case "${XDG_CURRENT_DESKTOP:-}" in
        *GNOME*|*gnome*)
            if "$BIN/correcteur" raccourcis-gnome >/dev/null 2>&1; then
                info "Raccourcis GNOME créés : Ctrl+Alt+C (vérifier) et Ctrl+Alt+X (correction express)"
            else
                warn "Raccourcis GNOME non créés : lancez « correcteur raccourcis-gnome » pour voir l'erreur."
            fi ;;
        *) warn "Wayland : créez deux raccourcis dans les paramètres du bureau : « correcteur selection » et « correcteur express »." ;;
    esac
elif [ -n "$SOURCES" ]; then
    # La version empaquetée embarque libxcb-cursor ; le Qt installé par pip, non.
    if command -v ldconfig >/dev/null && ! ldconfig -p 2>/dev/null | grep -q libxcb-cursor.so.0; then
        warn "Qt a besoin de libxcb-cursor0 sous X11 (ex. sudo apt install libxcb-cursor0)."
    fi
fi

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) warn "$BIN n'est pas dans votre PATH : ajoutez-le pour utiliser la commande « correcteur »." ;;
esac

info "Démarrage du correcteur"
nohup "$BIN/correcteur" >/dev/null 2>&1 &
info "Terminé. Sélectionnez un texte puis Ctrl+Alt+C pour le vérifier."
