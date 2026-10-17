#!/usr/bin/env bash
# Installe Correcteur pour l'utilisateur courant (aucun droit administrateur).
#
#  - depuis une version téléchargée (dossier contenant l'exécutable `correcteur`) ;
#  - ou depuis les sources (dépôt git) : environnement Python dédié.
#
# Variables : PREFIX (défaut ~/.local), SANS_DEMARRAGE=1 pour ne pas lancer
# le correcteur à l'ouverture de session.
#
# Désinstallation : installer-linux.sh --desinstaller (mêmes variables). Les
# paramètres et le dictionnaire personnel de l'utilisateur sont gardés.
set -euo pipefail

PREFIX="$(realpath -m "${PREFIX:-$HOME/.local}")"  # chemin absolu : il sert de cible aux liens
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$PREFIX/bin"
DEST="$PREFIX/opt/correcteur"
VENV="$PREFIX/share/correcteur/venv"
SOURCES=""

info() { printf '\033[1;34m▶\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }

# PID des instances de l'application lancées depuis l'exécutable installé (test -ef suit le lien
# /proc/PID/exe, sans programme externe). Seule compte l'application, lancée sans argument ou avec
# --demarrage (et --debug) : pas les commandes comme « correcteur selection » des raccourcis GNOME, qui
# transmettent leur demande à l'instance et s'arrêtent aussitôt.
instance_pids() {
    local proc arg app
    local -a args
    for proc in /proc/[0-9]*; do
        [ "$proc/exe" -ef "$DEST/correcteur" ] || continue
        mapfile -d '' -t args 2>/dev/null < "$proc/cmdline" || continue
        [ "${#args[@]}" -gt 0 ] || continue
        app=1
        for arg in "${args[@]:1}"; do
            case "$arg" in --demarrage|--debug) ;; *) app="" ;; esac
        done
        [ -n "$app" ] && echo "${proc#/proc/}"
    done
    return 0
}

instance_running() { [ -n "$(instance_pids)" ]; }

# Arrête l'instance en cours (5 s au plus) ; prévient si elle ne répond pas ou ne s'arrête pas.
stop_instance() {
    if "$BIN/correcteur" quitter >/dev/null 2>&1; then
        for _ in $(seq 50); do
            instance_running || return 0
            sleep 0.1
        done
        warn "L'ancienne instance du correcteur ne s'est pas arrêtée (PID $(instance_pids | xargs)) : $1"
    elif instance_running; then
        warn "Le correcteur en cours (PID $(instance_pids | xargs)) ne répond pas à « correcteur quitter » : $1"
    fi
}

# ldconfig est souvent dans /sbin, hors du PATH d'un utilisateur.
find_ldconfig() {
    local candidate
    for candidate in "$(command -v ldconfig 2>/dev/null || true)" /sbin/ldconfig /usr/sbin/ldconfig; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

# Retire les raccourcis GNOME du correcteur de la liste des raccourcis personnalisés, sans toucher aux autres.
remove_gnome_shortcuts() {
    local schema=org.gnome.settings-daemon.plugins.media-keys list errors part kept="" found="" path key i
    local -a parts
    command -v gsettings >/dev/null || return 0
    errors="$(mktemp)"
    if ! list="$(gsettings get "$schema" custom-keybindings 2>"$errors")" || grep -q "memory" "$errors"; then
        # Base en mémoire : la liste lue serait vide, et la réécrire effacerait les raccourcis de l'utilisateur.
        warn "Raccourcis GNOME non retirés : gsettings n'atteint pas les réglages de la session."
        rm -f "$errors"
        return 0
    fi
    rm -f "$errors"
    # Liste GVariant ['/chemin/a/', '/chemin/b/'] : les chemins sont entre apostrophes.
    IFS="'" read -r -a parts <<< "$list"
    for ((i = 1; i < ${#parts[@]}; i += 2)); do
        part="${parts[i]}"
        case "$part" in
            */correcteur-verifier/|*/correcteur-express/) found="$found $part" ;;
            *) kept="${kept:+$kept, }'$part'" ;;
        esac
    done
    [ -n "$found" ] || return 0
    gsettings set "$schema" custom-keybindings "[$kept]"
    for path in $found; do
        for key in name command binding; do
            gsettings reset "$schema.custom-keybinding:$path" "$key" 2>/dev/null || true
        done
    done
    info "Raccourcis GNOME du correcteur retirés"
}

uninstall() {
    local config data autostart_file
    if [ -n "${CORRECTEUR_HOME:-}" ]; then
        config="$CORRECTEUR_HOME/config"
        data="$CORRECTEUR_HOME/data"
    else
        config="${XDG_CONFIG_HOME:-$HOME/.config}/Correcteur"
        data="${XDG_DATA_HOME:-$HOME/.local/share}/Correcteur"
    fi
    autostart_file="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/correcteur.desktop"
    info "Désinstallation du correcteur ($PREFIX)"
    if [ -x "$BIN/correcteur" ]; then
        stop_instance "elle continue de tourner jusqu'à la fin de la session."
    fi
    # Le lien de la commande, seulement s'il mène à cette installation.
    case "$(readlink "$BIN/correcteur" 2>/dev/null || true)" in
        "$DEST/correcteur"|"$VENV/bin/correcteur") rm -f "$BIN/correcteur" ;;
    esac
    rm -rf "$DEST" "$DEST.ancien" "$VENV"
    rmdir "$PREFIX/share/correcteur" 2>/dev/null || true
    rm -f "$PREFIX/share/applications/correcteur.desktop" "$autostart_file" \
          "$PREFIX/share/icons/hicolor/scalable/apps/correcteur.svg"
    remove_gnome_shortcuts
    info "Correcteur désinstallé."
    info "Paramètres et dictionnaire personnel gardés dans $config"
    info "Moteurs téléchargés et journal gardés dans $data"
    info "Supprimez ces deux dossiers pour tout effacer."
}

case "${1:-}" in
    "") ;;
    --desinstaller) uninstall; exit 0 ;;
    *) echo "Usage : $0 [--desinstaller]" >&2; exit 2 ;;
esac

mkdir -p "$BIN" "$PREFIX/share/applications" "$PREFIX/share/icons/hicolor/scalable/apps"

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
        # On attend la fin de l'ancienne instance : ses fichiers vont disparaître, et le correcteur relancé
        # plus bas lui passerait la main au lieu de démarrer.
        stop_instance "elle est remplacée quand même ; fermez-la (kill) ou fermez la session si les raccourcis ne répondent plus."
        # Ancienne version mise de côté, pas supprimée : jamais de moment sans installation complète.
        rm -rf "$DEST.ancien"
        if [ -e "$DEST" ]; then
            mv "$DEST" "$DEST.ancien"
        fi
        if ! mv "$STAGING" "$DEST"; then
            [ -e "$DEST.ancien" ] && mv "$DEST.ancien" "$DEST"
            exit 1
        fi
        STAGING=""
        rm -rf "$DEST.ancien"
    fi
    ln -sf "$DEST/correcteur" "$BIN/correcteur"
    ICON_SRC="$DEST/_internal/correcteur/resources/icone.svg"
elif [ -f "$HERE/../pyproject.toml" ]; then
    REPO="$(cd "$HERE/.." && pwd)"
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
    if LDCONFIG="$(find_ldconfig)" && ! "$LDCONFIG" -p 2>/dev/null | grep -q libxcb-cursor.so.0; then
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
