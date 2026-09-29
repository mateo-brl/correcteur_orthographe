#!/usr/bin/env bash
# Installe Correcteur pour l'utilisateur courant (aucun droit administrateur).
#
#  - depuis une version téléchargée (dossier contenant l'exécutable `correcteur`) ;
#  - ou depuis les sources (dépôt git) : environnement Python dédié.
#
# Variables : PREFIX (défaut ~/.local), SANS_DEMARRAGE=1 pour ne pas lancer
# le correcteur à l'ouverture de session.
set -euo pipefail

PREFIX="${PREFIX:-$HOME/.local}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$PREFIX/bin"
mkdir -p "$BIN" "$PREFIX/share/applications" "$PREFIX/share/icons/hicolor/scalable/apps"

info() { printf '\033[1;34m▶\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }

if [ -x "$HERE/correcteur" ] && [ -d "$HERE/_internal" ]; then
    info "Installation de la version empaquetée dans $PREFIX/opt/correcteur"
    "$BIN/correcteur" quitter >/dev/null 2>&1 || true
    rm -rf "$PREFIX/opt/correcteur"
    mkdir -p "$PREFIX/opt"
    cp -r "$HERE" "$PREFIX/opt/correcteur"
    ln -sf "$PREFIX/opt/correcteur/correcteur" "$BIN/correcteur"
    ICON_SRC="$PREFIX/opt/correcteur/_internal/correcteur/resources/icone.svg"
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
            fi ;;
        *) warn "Wayland : créez deux raccourcis dans les paramètres du bureau : « correcteur selection » et « correcteur express »." ;;
    esac
else
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
