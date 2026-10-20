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
import codecs
import errno
import json
import locale
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
# Langues de LanguageTool (et le français de Grammalecte) : un code inconnu (« zz ») est refusé avant tout envoi,
# au lieu de la réponse anglaise de LanguageTool et de sa longue liste de codes.
_LANGUAGES = frozenset("""ar ast be br ca crh da de el en eo es fa fr ga gl is it ja km lt ml nb nl no pl pt ro ru sk
    sl sv ta tl uk zh""".split())

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


# Caractères absents de la page de code d'une console (850 en France) : un équivalent plutôt qu'un « ? ».
_PLAIN_EQUIVALENTS = {"’": "'", "‘": "'", "“": '"', "”": '"', "œ": "oe", "Œ": "OE", "…": "...", "\u2013": "-",
                      "\u2014": "-", "€": "EUR", "\u202f": " ", "\u2009": " ", "→": "->", "✓": "v"}


def _plain_equivalents(error: UnicodeError):
    if not isinstance(error, UnicodeEncodeError):
        raise error
    part = error.object[error.start:error.end]
    return "".join(_PLAIN_EQUIVALENTS.get(char, "?") for char in part), error.end


codecs.register_error("correcteur", _plain_equivalents)


def _windows_stream(number: int) -> str:
    """Nature d'une entrée ou sortie standard sous Windows : "console", "tube", "fichier" ou ""."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetFileType.argtypes = [ctypes.c_void_p]
        kind = kernel32.GetFileType(kernel32.GetStdHandle(number))
    except Exception:
        return ""
    return {1: "fichier", 2: "console", 3: "tube"}.get(kind, "")


def _console_code_page(output: bool = True) -> str | None:
    """Page de code de la console (« cp850 »), None sans console."""
    try:
        import ctypes

        page = ctypes.windll.kernel32.GetConsoleOutputCP() if output else ctypes.windll.kernel32.GetConsoleCP()
    except Exception:
        return None
    if not page:
        return None
    return "utf-8" if page == 65001 else f"cp{page}"


def _forced_encoding() -> tuple[str, str] | None:
    """Encodage imposé par PYTHONIOENCODING (« utf-8 », « cp850:replace »), appliqué par le correcteur lui-même :
    l'exécutable empaqueté (PyInstaller) ignore les variables PYTHON* et aurait écrit en Windows-1252."""
    name, _, errors = os.environ.get("PYTHONIOENCODING", "").partition(":")
    if not name:
        return None
    try:
        codecs.lookup(name)
    except LookupError:
        return None
    return name, errors or "replace"


def _utf8_io(utf8: bool = False) -> None:
    """Sorties en UTF-8, sauf sous Windows vers un tube : PowerShell, findstr ou more lisent alors dans la page de
    code de la console (850), et « $r = correcteur-cli ... » donnait « lev├® » au lieu de « levé ». Les caractères
    absents de cette page (œ, ’, …) y deviennent « oe », « ' », « ... » : `utf8` (option --utf8) garde l'UTF-8,
    sans perte, pour un programme qui le lit. Un encodage imposé par PYTHONIOENCODING passe avant tout."""
    forced = _forced_encoding() or (("utf-8", "replace") if utf8 else None)
    for number, stream in ((-11, sys.stdout), (-12, sys.stderr)):
        encoding, errors = forced or ("utf-8", "replace")
        if not forced and sys.platform == "win32" and _windows_stream(number) == "tube":
            encoding = _console_code_page() or "utf-8"
            errors = "correcteur"
        try:
            stream.reconfigure(encoding=encoding, errors=errors)  # type: ignore[union-attr]
        except Exception:
            pass


def _ansi() -> str:
    """Encodage des anciens fichiers Windows (« ANSI ») : celui du système sous Windows, Windows-1252 ailleurs."""
    preferred = locale.getpreferredencoding(False) if sys.platform == "win32" else ""
    return preferred if preferred and codecs.lookup(preferred).name not in ("utf-8", "ascii") else "cp1252"


class BinaryData(ValueError):
    """Contenu qui n'est pas du texte (programme, image...) : le vérifier prendrait des minutes pour rien."""


# Caractères de contrôle qu'un texte ne contient pas (tabulation, fins de ligne, saut de page, fin de fichier DOS et
# échappement mis à part) : nombreux, ils trahissent un fichier binaire.
_CONTROLS = re.compile("[\x00-\x08\x0e-\x19\x1c-\x1f]")


def _looks_binary(text: str) -> bool:
    return len(_CONTROLS.findall(text)) > max(3, len(text) // 100)


def _utf16(data: bytes) -> tuple[str, str] | None:
    """UTF-16 sans BOM (Bloc-notes, PowerShell) : un octet sur deux nul pour un texte surtout latin, et aucun
    caractère nul ni de contrôle une fois décodé (sinon c'est un fichier binaire)."""
    if len(data) < 2 or len(data) % 2:
        return None
    for name, high in (("utf-16-le", data[1::2]), ("utf-16-be", data[0::2])):
        if high.count(0) * 2 <= len(high):
            continue
        try:
            text = data.decode(name)
        except UnicodeDecodeError:
            continue
        if "\0" not in text.rstrip("\0") and not _looks_binary(text):
            return text.rstrip("\0"), name
    return None


def decode_text(data: bytes, fallback: str | None = None) -> tuple[str, str]:
    """(texte, encodage) d'un fichier ou de l'entrée standard : BOM UTF-8 ou UTF-16, UTF-16 sans BOM (Bloc-notes,
    PowerShell), UTF-8, sinon `fallback` (par défaut : ANSI). BinaryData si ce n'est pas du texte.

    Un texte en UTF-16 sans BOM fait seulement de caractères ASCII est aussi de l'UTF-8 valide (octets nuls
    compris) : il était lu tel quel, et « on » n'était pas corrigé. Les octets nuls sont donc regardés d'abord."""
    for bom, name in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
                      (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if data.startswith(bom):
            try:
                return data[len(bom):].decode(name), name
            except UnicodeDecodeError:
                break
    if b"\0" in data:
        found = _utf16(data)
        if found is not None:
            return found
        data = data.rstrip(b"\0")  # octets nuls de remplissage en fin de fichier : tolérés
        if b"\0" in data:
            raise BinaryData("contenu binaire")
    try:
        text, name = data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        name = fallback or _ansi()
        text = data.decode(name, errors="replace")
    if _looks_binary(text):
        raise BinaryData("contenu binaire")
    return text, name


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


_ENCODING_NAMES = {"utf-16-le": "UTF-16", "utf-16-be": "UTF-16", "utf-32-le": "UTF-32", "utf-32-be": "UTF-32"}
# Causes des erreurs de fichier en français (le système les donne en anglais : « Permission denied »).
_OS_CAUSES = {errno.EACCES: "accès refusé", errno.EPERM: "accès refusé", errno.EISDIR: "c'est un dossier",
              errno.ENOTDIR: "chemin invalide", errno.ENAMETOOLONG: "nom trop long", errno.EINVAL: "nom invalide",
              errno.ELOOP: "liens symboliques en boucle", errno.EIO: "erreur de lecture du disque",
              errno.EMFILE: "trop de fichiers ouverts", errno.EBUSY: "fichier occupé par un autre programme"}


def _read_file(path: str) -> str:
    """Contenu exact du fichier (fins de ligne d'origine, sans BOM) : UTF-8, UTF-16 ou ANSI (Windows-1252),
    signalé sur la sortie d'erreur s'il n'est pas en UTF-8."""
    if os.path.isdir(path):
        raise ValueError(f"« {path} » est un dossier, pas un fichier.")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        raise ValueError(f"fichier « {path} » introuvable.") from None
    except OSError as exc:
        if getattr(exc, "winerror", None) in (32, 33):  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
            cause = "fichier ouvert par un autre programme"
        else:
            cause = _OS_CAUSES.get(exc.errno or 0, f"erreur système {exc.errno}" if exc.errno else "erreur système")
        raise ValueError(f"lecture du fichier « {path} » impossible ({cause}).") from None
    try:
        text, encoding = decode_text(data)
    except BinaryData:
        raise ValueError(f"« {path} » n'est pas un fichier texte (contenu binaire).") from None
    if encoding != "utf-8":
        name = _ENCODING_NAMES.get(encoding) or f"ANSI ({encoding})"
        print(f"Remarque : « {path} » n'est pas en UTF-8, il a été lu en {name}.", file=sys.stderr)
    return text


def _read_stdin() -> str:
    """Entrée standard. Sous Windows, les octets d'un tube viennent d'un autre programme : « echo » de cmd écrit
    dans la page de code de la console (850, « levé » devenait « lev‚ »), PowerShell 7 en UTF-8."""
    if sys.stdin is None:
        return ""
    if not hasattr(sys.stdin, "buffer") or sys.stdin.isatty():
        return sys.stdin.read()  # console : Python y lit déjà de l'Unicode
    forced = _forced_encoding()
    if forced:
        return sys.stdin.buffer.read().decode(*forced)
    if sys.platform != "win32":
        return sys.stdin.read()
    kind = _windows_stream(-10)
    if kind not in ("tube", "fichier"):
        return sys.stdin.read()
    data = sys.stdin.buffer.read()
    fallback = _console_code_page(output=False) if kind == "tube" else None
    try:
        return decode_text(data, fallback)[0]
    except BinaryData:
        raise ValueError("l'entrée standard n'est pas du texte (contenu binaire).") from None


def _read_text(args) -> str:
    if getattr(args, "fichier", None):
        return _read_file(args.fichier)
    if getattr(args, "texte", None):
        return " ".join(args.texte)
    if sys.stdin is None or sys.stdin.isatty():
        print("Tapez ou collez le texte, puis Ctrl+D (Linux) ou Ctrl+Z puis Entrée (Windows) :", file=sys.stderr)
    return _read_stdin()


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
    if lang.lower() not in _LANGUAGES:
        raise ValueError(f"langue « {value} » inconnue. Langues gérées : " + ", ".join(sorted(_LANGUAGES)) + ".")
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


def _ascii_json() -> bool:
    """JSON en ASCII (« \\u00e9 ») quand la sortie ne sait pas tout écrire (page de code d'une console) : il reste
    exact pour qui le lit, au lieu de perdre des caractères."""
    encoding = (getattr(sys.stdout, "encoding", "") or "utf-8").lower().replace("_", "-")
    return encoding not in ("utf-8", "utf8")


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
        if unchecked:
            payload["non_verifie"] = unchecked
        print(json.dumps(payload, ensure_ascii=_ascii_json(), indent=2))
        if unchecked:
            return EXIT_UNCHECKED  # la raison est dans le JSON (« non_verifie ») : rien sur la sortie d'erreur
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
    print("Session : " + " ".join(part for part in (session_type(), desktop_name()) if part))
    print(f"Paramètres : {config_dir()}")
    print(f"Données : {data_dir()}")
    print(f"Journal : {log_path()}")
    print(f"Plantages : {data_dir() / 'plantages.log'}")
    def yes(value) -> str:
        return "oui" if value else "non"

    print(f"Instance en cours : {yes(send_command('ping', timeout=1))}")
    print(f"Grammalecte : {'installé' if import_grammalecte() else 'non installé'} (activé : {yes(settings.grammalecte.enabled)})")
    print(f"LanguageTool : mode {settings.languagetool.mode} (activé : {yes(settings.languagetool.enabled)})")
    print(f"  serveur local installé : {yes(_find_server_jar())} · Java : {find_java() or 'introuvable'}")
    print(f"Raccourcis globaux activés : {yes(settings.general.hotkeys_enabled)}")
    try:
        from correcteur.platform.autostart import autostart_enabled

        print(f"Démarrage automatique : {yes(autostart_enabled())}")
    except Exception as exc:
        print(f"Démarrage automatique : inconnu ({exc})")
    if session_type() == "x11":
        from correcteur.platform.bridge import X11Bridge

        ok, why = X11Bridge().hotkeys_supported()
        print(f"Raccourcis globaux : {'oui (pynput)' if ok else why.splitlines()[0]}")
    if sys.platform.startswith("linux"):
        if "gnome" in desktop_name():
            try:
                from correcteur.platform.autostart import gnome_shortcuts
                from correcteur.platform.bridge import _gnome_display

                found = gnome_shortcuts()
                print("Raccourcis GNOME : " + (", ".join(_gnome_display(b) for b in found.values()) or "aucun"))
            except Exception as exc:
                print(f"Raccourcis GNOME : illisibles ({exc})")
        for tool in ("wl-paste", "wl-copy", "wtype", "ydotool", "xdotool", "gsettings"):
            print(f"  {tool} : {yes(which(tool))}")
        if which("ydotool"):
            print(f"  ydotoold (service de ydotool) : {_ydotoold_state()}")
    return 0


def _ydotoold_state() -> str:
    """ydotoold, sans lequel ydotool ne tape rien : « en marche », « arrêté » ou « inconnu »."""
    import subprocess

    from correcteur.platform import child_env, which

    if not which("pgrep"):
        return "inconnu"
    try:
        out = subprocess.run(["pgrep", "-x", "ydotoold"], capture_output=True, timeout=2, env=child_env())
    except (OSError, subprocess.TimeoutExpired):
        return "inconnu"
    return "en marche" if out.returncode == 0 else "arrêté"


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


# Textes d'argparse (aide et erreurs) en français, comme le reste du correcteur.
_ARGPARSE_FR = {
    "usage: ": "utilisation : ",
    "positional arguments": "arguments",
    "optional arguments": "options",
    "options": "options",
    "show this help message and exit": "afficher cette aide",
    "show program's version number and exit": "afficher la version",
    "%(prog)s: error: %(message)s\n": "%(prog)s : erreur : %(message)s\n",
    "the following arguments are required: %s": "il manque : %s",
    "invalid choice: %(value)r (choose from %(choices)s)": "choix inconnu : %(value)r (possibles : %(choices)s)",
    "unrecognized arguments: %s": "arguments inconnus : %s",
    "expected one argument": "une valeur est attendue",
    "ambiguous option: %(option)s could match %(matches)s": "option ambiguë : %(option)s (%(matches)s ?)",
    "argument %(argument_name)s: %(message)s": "argument %(argument_name)s : %(message)s",
    "invalid %(type)s value: %(value)r": "valeur invalide : %(value)r",
    "not allowed with argument %s": "impossible avec l'argument %s",
}


def _french(message: str) -> str:
    return _ARGPARSE_FR.get(message, message)


class _Formatter(argparse.HelpFormatter):
    """Titres de l'aide à la française : « options : », pas « options: »."""

    def start_section(self, heading):
        super().start_section(f"{heading} " if heading else heading)


class _Parser(argparse.ArgumentParser):
    """Avec --json, les erreurs de la ligne de commande (option inconnue, argument manquant) sortent aussi en JSON,
    sur la sortie standard, comme les autres erreurs (voir main). Les sous-commandes héritent de cette classe."""

    json_errors = False

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("formatter_class", _Formatter)
        super().__init__(*args, **kwargs)

    def error(self, message: str):
        if _Parser.json_errors:
            print(json.dumps({"erreur": message}, ensure_ascii=_ascii_json()))
            raise SystemExit(2)
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    argparse._ = _french  # type: ignore[attr-defined]  (argparse traduit ses textes par cette fonction)
    # Exécutable empaqueté : son vrai nom (« correcteur-cli.exe » sous Windows) dans l'aide.
    prog = os.path.basename(sys.argv[0]) if getattr(sys, "frozen", False) and sys.argv[0] else "correcteur"
    parser = _Parser(prog=prog, description="Correcteur d'orthographe et de grammaire.")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--demarrage", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help="journal détaillé")
    sub = parser.add_subparsers(dest="commande", title="commandes", metavar="COMMANDE")

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
        p.add_argument("texte", nargs="*", help=("texte à vérifier" if name == "verifier" else "texte à corriger")
                       + " (sinon : entrée standard)")
        p.add_argument("-f", "--fichier", help="fichier texte (UTF-8, UTF-16 ou ANSI)")
        p.add_argument("-l", "--langue", help="auto, fr, ou un code comme en ou en-US")
        p.add_argument("--strict", action="store_true", help="typographie stricte")
        p.add_argument("--hors-ligne", action="store_true", help="n'utiliser que les moteurs locaux")
        p.add_argument("--utf8", action="store_true",
                       help="sortie en UTF-8, même dans un tube sous Windows (sinon : page de code de la console)")
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
    if sys.platform != "win32":
        sub.add_parser("raccourcis-gnome", help="créer les raccourcis clavier GNOME").set_defaults(func=cmd_gnome)
    p = sub.add_parser("demarrage", help="lancement automatique à l'ouverture de session")
    p.add_argument("etat", choices=["oui", "non"])
    p.set_defaults(func=cmd_demarrage)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] not in ("--demarrage",) + tuple(_GUI_COMMANDS):
        _attach_console_windows()
    _utf8_io(utf8="--utf8" in argv)
    _Parser.json_errors = "--json" in argv
    parser = build_parser()
    args, extras = parser.parse_known_args(argv)
    if extras:
        # Option inconnue d'une commande : l'erreur montre l'utilisation de cette commande, pas la générale.
        command = getattr(args, "commande", None) or ""
        target = next((a.choices[command] for a in parser._actions
                       if isinstance(a, argparse._SubParsersAction) and command in a.choices), parser)
        target.error(_french("unrecognized arguments: %s") % " ".join(extras))
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if hasattr(args, "func"):
        try:
            return args.func(args)
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            if getattr(args, "json", False):
                print(json.dumps({"erreur": str(exc)}, ensure_ascii=_ascii_json()))  # sortie toujours lisible en JSON
            else:
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
