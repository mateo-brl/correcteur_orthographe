"""Ligne de commande.

    correcteur                     lance l'application (icône dans la zone de notification)
    correcteur selection           vérifie le texte sélectionné (pour un raccourci du bureau)
    correcteur express             corrige directement le texte sélectionné
    correcteur verifier "texte"    vérifie un texte dans le terminal (ou via l'entrée standard)
    correcteur corriger "texte"    affiche le texte avec les corrections sûres appliquées
    correcteur installer grammalecte|languagetool
    correcteur diagnostic

Codes de retour de verifier et corriger : 0 (aucune faute, ou texte corrigé), 1 (fautes trouvées),
2 (erreur), 3 (aucun moteur n'a pu vérifier le texte).
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import os
import re
import sys

from correcteur import APP_NAME, __version__

# Aucun moteur n'a vérifié le texte (langue non gérée, moteurs en échec) : ce n'est pas « aucune faute ».
EXIT_UNCHECKED = 3
# « fr », « en-US », et les variantes de LanguageTool : « ca-ES-valencia », « de-DE-x-simple-language ».
# Langue sur 2 lettres seule, ou sur 2-3 lettres suivie d'un pays sur 2 lettres (« en-US », « ast-ES »), puis
# d'éventuelles variantes.
_LANGUAGE_RE = re.compile(r"[a-z]{2}|[a-z]{2,3}-[a-z]{2}(?:-[a-z0-9]+)*", re.IGNORECASE)

_GUI_COMMANDS = {
    "selection": "verifier-selection",
    "express": "correction-express",
    "ouvrir": "ouvrir",
    "parametres": "parametres",
    "presse-papiers": "presse-papiers",
}


def _attach_console_windows() -> None:
    """Version empaquetée sans console : on réutilise celle du terminal appelant."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False) or sys.stdout is not None:
        return
    try:
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stdin = open("CONIN$", encoding="utf-8", errors="replace")
    except Exception:
        pass


def _utf8_io() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _colors() -> bool:
    if os.environ.get("NO_COLOR") or not getattr(sys.stdout, "isatty", lambda: False)():
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                return True
        except Exception:
            return False
        return False
    return True


def _read_file(path: str) -> str:
    """Contenu exact du fichier (fins de ligne d'origine, sans l'éventuel BOM UTF-8)."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return fh.read()
    except UnicodeDecodeError:
        raise ValueError(f"le fichier « {path} » n'est pas en UTF-8 : enregistrez-le en UTF-8 puis "
                         "recommencez.") from None
    except FileNotFoundError:
        raise ValueError(f"fichier « {path} » introuvable.") from None
    except OSError as exc:
        raise ValueError(f"lecture du fichier « {path} » impossible ({exc.strerror or exc}).") from None


def _read_text(args) -> str:
    if getattr(args, "fichier", None):
        return _read_file(args.fichier)
    if getattr(args, "texte", None):
        return " ".join(args.texte)
    if sys.stdin is None or sys.stdin.isatty():
        print("Tapez ou collez le texte, puis Ctrl+D (Linux) ou Ctrl+Z puis Entrée (Windows) :", file=sys.stderr)
    return sys.stdin.read() if sys.stdin else ""


def _normalize_language(value: str | None) -> str | None:
    """Option -l : « auto », ou un code de la forme « fr » ou « en-US »."""
    if value is None:
        return None
    if value.lower() == "auto":
        return "auto"
    match = _LANGUAGE_RE.fullmatch(value)
    if not match:
        raise ValueError(f"langue « {value} » non reconnue : utilisez « auto », « fr » ou un code de la forme "
                         "« en », « en-US » ou « ca-ES-valencia ».")
    lang, *rest = value.split("-")
    return "-".join([lang.lower()] + [p.upper() if i == 0 and len(p) == 2 else p for i, p in enumerate(rest)])


def _make_checker(args):
    from correcteur.checker import Checker
    from correcteur.config import PersonalDictionary, SettingsStore

    settings = SettingsStore().load()
    if getattr(args, "langue", None):
        settings.general.language = args.langue
    if getattr(args, "strict", False):
        settings.general.typography = "stricte"
    # Hors ligne : seul le serveur LanguageTool de cette machine peut rester ; tout autre mode (public, perso,
    # premium, ou valeur inconnue traitée comme une adresse) passe par le réseau.
    if getattr(args, "hors_ligne", False) and settings.languagetool.mode != "local":
        settings.languagetool.enabled = False
    return Checker(settings, PersonalDictionary())


def _unchecked_reason(args, checker, result) -> str | None:
    """Pourquoi aucun moteur n'a vérifié le texte, ou None si au moins un l'a fait."""
    if any(status.ok for status in result.statuses.values()):
        return None
    failures = [status.detail for status in result.statuses.values() if status.detail]
    if failures:
        return "Aucun moteur n'a pu vérifier le texte : " + " ; ".join(failures)
    if not any(engine.is_enabled() for engine in checker.engines):
        return "Aucun moteur n'a vérifié le texte : tous les moteurs sont désactivés dans les paramètres."
    language = checker.resolve_language(result.text)
    reason = "Aucun moteur activé ne gère " + ("la langue de ce texte." if language == "auto"
                                                else f"cette langue ({language}).")
    if getattr(args, "hors_ligne", False):
        reason += " Hors ligne, seul Grammalecte (français uniquement) est utilisé."
    return reason


def _counts(issues) -> bool:
    """Le code 1 (fautes trouvées) ne vient pas d'une simple remarque de style (« futur proche… »)."""
    from correcteur.models import Category

    return any(i.category is not Category.STYLE for i in issues)


def _partial_warning(result) -> str | None:
    """Un moteur a échoué mais un autre a vérifié : le dire (LanguageTool trop lent, par exemple)."""
    failed = [s.detail for s in result.statuses.values() if not s.ok and s.detail]
    if failed and any(s.ok for s in result.statuses.values()):
        return "Vérification partielle : " + " ; ".join(failed)
    return None


def cmd_verifier(args) -> int:
    args.langue = _normalize_language(args.langue)
    text = _read_text(args)
    checker = _make_checker(args)
    try:
        result = checker.check(text)
    finally:
        checker.close()
    unchecked = _unchecked_reason(args, checker, result)
    if args.json:
        payload = {
            "texte": text,
            "fautes": [i.to_dict(text) for i in result.issues],
            "moteurs": {n: {"ok": s.ok, "detail": s.detail, "ms": round(s.duration_ms)} for n, s in result.statuses.items()},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if unchecked:
            print(unchecked, file=sys.stderr)
            return EXIT_UNCHECKED
        return 1 if _counts(result.issues) else 0
    if unchecked:
        print(unchecked, file=sys.stderr)
        return EXIT_UNCHECKED
    color = _colors()
    red, dim, bold, reset = ("\033[31m", "\033[2m", "\033[1m", "\033[0m") if color else ("", "", "", "")
    for name, status in result.statuses.items():
        if not status.ok:
            print(f"{dim}[{name}] {status.detail}{reset}", file=sys.stderr)
    if not result.issues:
        print("Aucune faute trouvée.")
        return 0
    starts = [0] + [m.end() for m in re.finditer(r"\r\n|\r|\n", text)]  # débuts de ligne, toutes fins comprises
    for issue in result.issues:
        line_no = bisect.bisect_right(starts, issue.start) - 1
        col = issue.start - starts[line_no]
        sugg = ", ".join(issue.replacements[:4]) or "(pas de suggestion)"
        sure = " [sûre]" if issue.confident else ""
        print(f"{bold}{line_no + 1}:{col + 1}{reset} {red}{issue.original(text)!r}{reset} → {sugg}{sure}")
        print(f"    {issue.message} {dim}({issue.category.value}, {'+'.join(issue.sources)}){reset}")
    print(f"\n{len(result.issues)} remarque(s).")
    return 1 if _counts(result.issues) else 0


def cmd_corriger(args) -> int:
    from correcteur.textutils import apply_edits

    args.langue = _normalize_language(args.langue)
    text = _read_text(args)
    checker = _make_checker(args)
    try:
        if args.tout:
            result = checker.check(text)
            fixed = apply_edits(text, [(i.start, i.end, i.replacements[0]) for i in result.issues if i.replacements])
        else:
            fixed, _applied, _remaining, result = checker.autocorrect(text)
    finally:
        checker.close()
    if "\r" in fixed:
        try:
            # Fins de ligne écrites telles quelles (sous Windows, \r\n deviendrait \r\r\n).
            sys.stdout.reconfigure(newline="")  # type: ignore[union-attr]
        except Exception:
            pass
    # Un fichier redirigé ressort à l'identique ; dans le terminal, on finit toujours par un retour à la ligne.
    exact = bool(getattr(args, "fichier", None)) and not getattr(sys.stdout, "isatty", lambda: False)()
    end = "" if exact or fixed.endswith("\n") else ("\r\n" if "\r\n" in fixed else "\n")
    print(fixed, end=end)
    unchecked = _unchecked_reason(args, checker, result)
    if unchecked:
        print(unchecked, file=sys.stderr)
        return EXIT_UNCHECKED
    partial = _partial_warning(result)
    if partial:
        print(partial, file=sys.stderr)
    return 0


def cmd_installer(args) -> int:
    from correcteur import installer

    if args.moteur == "grammalecte":
        path = installer.install_grammalecte(installer.console_progress("Grammalecte"), args.dossier)
    else:
        path = installer.install_languagetool(installer.console_progress("LanguageTool"))
        if installer.find_java() is None:
            print("Attention : Java 17 ou plus récent est nécessaire pour le mode local.", file=sys.stderr)
    print(f"Installé dans {path}")
    return 0


def cmd_diagnostic(args) -> int:
    from correcteur.config import SettingsStore, config_dir, data_dir, log_path
    from correcteur.engines.grammalecte_engine import import_grammalecte
    from correcteur.engines.lt_server import _find_server_jar
    from correcteur.installer import find_java
    from correcteur.platform import desktop_name, session_type, which
    from correcteur.platform.ipc import send_command

    settings = SettingsStore().load()
    print(f"{APP_NAME} {__version__} (Python {sys.version.split()[0]}, {sys.platform})")
    print(f"Session : {session_type()} {desktop_name()}")
    print(f"Paramètres : {config_dir()}")
    print(f"Données : {data_dir()}")
    print(f"Journal : {log_path()}")
    print(f"Plantages : {data_dir() / 'plantages.log'}")
    print(f"Instance en cours : {'oui' if send_command('ping', timeout=1) else 'non'}")
    print(f"Grammalecte : {'installé' if import_grammalecte() else 'non installé'} (activé : {settings.grammalecte.enabled})")
    print(f"LanguageTool : mode {settings.languagetool.mode} (activé : {settings.languagetool.enabled})")
    print(f"  serveur local installé : {'oui' if _find_server_jar() else 'non'} · Java : {find_java() or 'introuvable'}")
    if session_type() == "x11":
        from correcteur.platform.bridge import X11Bridge

        ok, why = X11Bridge().hotkeys_supported()
        print(f"Raccourcis globaux : {'oui (pynput)' if ok else why.splitlines()[0]}")
    if sys.platform.startswith("linux"):
        for tool in ("wl-paste", "wl-copy", "wtype", "ydotool", "xdotool", "gsettings"):
            print(f"  {tool} : {'oui' if which(tool) else 'non'}")
    return 0


def cmd_gnome(args) -> int:
    from correcteur.config import SettingsStore
    from correcteur.platform.autostart import install_gnome_shortcuts

    g = SettingsStore().load().general
    print(install_gnome_shortcuts(g.hotkey_check, g.hotkey_autocorrect))
    return 0


def cmd_demarrage(args) -> int:
    from correcteur.config import SettingsStore
    from correcteur.platform.autostart import set_autostart

    set_autostart(args.etat == "oui")
    store = SettingsStore()
    settings = store.load()
    settings.general.autostart = args.etat == "oui"  # même état dans les paramètres que sur le disque
    store.save(settings)
    print("Démarrage automatique " + ("activé." if args.etat == "oui" else "désactivé."))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="correcteur", description="Correcteur d'orthographe et de grammaire.")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--demarrage", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help="journal détaillé")
    sub = parser.add_subparsers(dest="commande")

    for name, help_text in (
        ("selection", "vérifier le texte sélectionné (fenêtre)"),
        ("express", "corriger directement le texte sélectionné"),
        ("ouvrir", "ouvrir la fenêtre du correcteur"),
        ("parametres", "ouvrir les paramètres"),
        ("presse-papiers", "vérifier le contenu du presse-papiers"),
        ("quitter", "arrêter l'application en cours"),
    ):
        sub.add_parser(name, help=help_text)

    for name, func, help_text in (("verifier", cmd_verifier, "vérifier un texte dans le terminal"),
                                  ("corriger", cmd_corriger, "afficher le texte corrigé")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("texte", nargs="*", help="texte à vérifier (sinon : entrée standard)")
        p.add_argument("-f", "--fichier", help="fichier texte (UTF-8)")
        p.add_argument("-l", "--langue", help="auto, fr, ou un code comme en ou en-US")
        p.add_argument("--strict", action="store_true", help="typographie stricte")
        p.add_argument("--hors-ligne", action="store_true", help="n'utiliser que les moteurs locaux")
        if name == "verifier":
            p.add_argument("--json", action="store_true", help="sortie JSON")
        else:
            p.add_argument("--tout", action="store_true", help="appliquer toutes les premières suggestions")
        p.set_defaults(func=func)

    p = sub.add_parser("installer", help="télécharger un moteur")
    p.add_argument("moteur", choices=["grammalecte", "languagetool"])
    p.add_argument("--dossier", help="dossier d'installation de Grammalecte (par défaut : dossier des moteurs)")
    p.set_defaults(func=cmd_installer)
    sub.add_parser("diagnostic", help="état de l'installation").set_defaults(func=cmd_diagnostic)
    sub.add_parser("raccourcis-gnome", help="créer les raccourcis clavier GNOME").set_defaults(func=cmd_gnome)
    p = sub.add_parser("demarrage", help="lancement automatique à l'ouverture de session")
    p.add_argument("etat", choices=["oui", "non"])
    p.set_defaults(func=cmd_demarrage)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] not in ("--demarrage",) + tuple(_GUI_COMMANDS):
        _attach_console_windows()
    _utf8_io()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if hasattr(args, "func"):
        try:
            return args.func(args)
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            print(f"Erreur : {exc}", file=sys.stderr)
            return 2

    from correcteur.platform.ipc import send_command

    if args.commande == "quitter":
        return 0 if send_command("quitter") else 1
    gui_command = _GUI_COMMANDS.get(args.commande or "", None)
    # Déjà lancé : on transmet la commande à l'instance existante (instantané).
    forwarded = "ping" if args.demarrage else (gui_command or "ouvrir")
    if send_command(forwarded, timeout=1.5):
        return 0
    from correcteur.ui.app import run_app

    return run_app(initial_command=gui_command, startup=args.demarrage)


def main_gui() -> int:
    return main()


if __name__ == "__main__":
    raise SystemExit(main())
