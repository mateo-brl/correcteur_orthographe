from __future__ import annotations

import codecs
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import needs_grammalecte

from correcteur import __version__
from correcteur.cli import EXIT_UNCHECKED, _make_checker, _normalize_language, build_parser, main
from correcteur.config import SettingsStore

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args, stdin=None):
    import os

    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8", NO_COLOR="1")
    return subprocess.run([sys.executable, "-m", "correcteur", *args], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", env=env, timeout=120)


def test_parser_knows_commands():
    parser = build_parser()
    args = parser.parse_args(["verifier", "--json", "--hors-ligne", "bonjour", "toi"])
    assert args.texte == ["bonjour", "toi"] and args.json and args.hors_ligne
    assert parser.parse_args(["installer", "grammalecte"]).moteur == "grammalecte"


def test_version(capsys):
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "Correcteur" in capsys.readouterr().out


def test_quit_without_instance_returns_error():
    assert main(["quitter"]) == 1


def _fake_interface(monkeypatch, run_app):
    """correcteur.ui.app remplacé : ni Qt ni fenêtre."""
    import types

    module = types.ModuleType("correcteur.ui.app")
    module.run_app = run_app
    monkeypatch.setitem(sys.modules, "correcteur.ui.app", module)


def test_launch_opens_the_channel_before_loading_the_interface(home, monkeypatch):
    """Le canal de commande est ouvert avant de charger Qt et l'interface (0,2 à 0,3 s) : un raccourci tapé pendant
    ce temps attend l'application au lieu de lancer une 2e instance."""
    from correcteur.platform.ipc import send_command

    seen = {}

    def run_app(initial_command=None, startup=False, ipc=None):
        seen.update(command=initial_command, listening=ipc is not None and ipc.listening)
        seen["queued"] = send_command("correction-express")
        ipc.stop()
        return 0

    _fake_interface(monkeypatch, run_app)
    assert main(["selection"]) == 0
    assert seen == {"command": "verifier-selection", "listening": True, "queued": True}


def test_launch_forwards_to_an_instance_started_meanwhile(home, monkeypatch):
    """Une autre instance s'est mise à écouter entre la première tentative et l'ouverture du canal : elle reçoit
    la commande, sans charger l'interface pour rien."""
    from correcteur.platform import ipc

    received, done = [], threading.Event()
    other = ipc.IpcServer(lambda cmd, data: (received.append(cmd), done.set()))
    assert other.start(), other.error
    real_send, calls = ipc.send_command, []

    def send_command(command, payload="", timeout=2.0):
        calls.append(command)
        return False if len(calls) == 1 else real_send(command, payload, timeout)  # 1re tentative : trop tôt

    monkeypatch.setattr(ipc, "send_command", send_command)
    _fake_interface(monkeypatch, lambda **kwargs: pytest.fail("interface chargée alors qu'une instance répond"))
    try:
        assert main(["express"]) == 0
        assert done.wait(3)
    finally:
        other.stop()
    assert received == ["correction-express"]


@needs_grammalecte
def test_verifier_json_offline():
    out = run_cli("verifier", "--json", "--hors-ligne", "Il faut que tu viens.")
    assert out.returncode == 1, out.stderr
    data = json.loads(out.stdout)
    assert data["fautes"][0]["texte"] == "viens"
    assert "viennes" in data["fautes"][0]["suggestions"]


@needs_grammalecte
def test_corriger_from_stdin_offline():
    out = run_cli("corriger", "--hors-ligne", stdin="Je vais a la plage , et ecole est fermée.\n")
    assert out.returncode == 0, out.stderr
    assert out.stdout == "Je vais à la plage, et école est fermée.\n"


@needs_grammalecte
def test_verifier_clean_text():
    out = run_cli("verifier", "--hors-ligne", "Le chat dort sur le canapé.")
    assert out.returncode == 0, out.stderr
    assert "Aucune faute" in out.stdout


def test_diagnostic_runs():
    out = run_cli("diagnostic")
    assert out.returncode == 0, out.stderr
    assert "Grammalecte" in out.stdout


def test_version_matches_pyproject():
    # La CI publie la version lue dans pyproject.toml : l'application doit afficher la même.
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1) == __version__


@pytest.mark.parametrize("command", ["verifier", "corriger"])
def test_offline_unsupported_language_is_not_a_success(home, command):
    """Hors ligne, rien ne vérifie l'anglais : ce n'est pas « Aucune faute trouvée » avec le code 0."""
    out = run_cli(command, "--hors-ligne", "-l", "en-US", "This is a speling mistake.")
    assert out.returncode == EXIT_UNCHECKED, out.stderr
    assert "Aucune faute" not in out.stdout
    assert "Aucun moteur" in out.stderr and "en-US" in out.stderr
    if command == "corriger":
        assert out.stdout == "This is a speling mistake.\n"  # le texte ressort tel quel


def test_offline_unsupported_language_json(home):
    """L-16 : avec --json, la raison est dans le JSON seulement, pas aussi sur la sortie d'erreur."""
    out = run_cli("verifier", "--json", "--hors-ligne", "-l", "en", "This is a speling mistake.")
    assert out.returncode == EXIT_UNCHECKED
    payload = json.loads(out.stdout)
    assert payload["moteurs"] == {} and "Aucun moteur" in payload["non_verifie"]
    assert out.stderr == ""


def test_cli_help_and_errors_in_french(capsys):
    """L-16 : « options : » avec l'espace, aide de corriger juste, et une option inconnue montre l'utilisation de
    la commande, pas celle de la commande principale."""
    from correcteur.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["corriger", "--help"])
    help_text = capsys.readouterr().out
    assert "options :" in help_text and "arguments :" in help_text and "texte à corriger" in help_text
    with pytest.raises(SystemExit) as stop:
        main(["verifier", "--inconnu", "Bonjour"])
    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("utilisation : correcteur verifier") and "arguments inconnus : --inconnu" in err


def test_diagnostic_says_yes_or_no(home, capsys):
    assert main(["diagnostic"]) == 0
    out = capsys.readouterr().out
    assert "True" not in out and "False" not in out and "(activé : oui)" in out
    assert "Démarrage automatique :" in out and "Raccourcis globaux activés : oui" in out


@pytest.mark.parametrize("value, expected", [("auto", "auto"), ("AUTO", "auto"), ("fr", "fr"), ("FR", "fr"),
                                             ("en-US", "en-US"), ("en-us", "en-US"), ("de", "de")])
def test_language_option_accepted(value, expected):
    assert _normalize_language(value) == expected


@pytest.mark.parametrize("value", ["fr_FR", "français", "e", "eng", "en-USA", "en-", "en US", ""])
def test_language_option_refused(value, capsys):
    assert main(["verifier", "-l", value, "texte"]) == 2
    err = capsys.readouterr().err
    assert f"langue « {value} » non reconnue" in err


@pytest.fixture
def personal_lt(home):
    """Faux serveur LanguageTool « perso » : note chaque requête et répond 400 (langue inconnue)."""
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            body = b"Error: 'xx' is not a language code known to LanguageTool."
            self.send_response(400)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store = SettingsStore()
    settings = store.load()
    settings.languagetool.mode = "perso"
    settings.languagetool.url = f"127.0.0.1:{server.server_port}"
    store.save(settings)
    yield hits
    server.shutdown()
    server.server_close()


def test_online_engine_failure_is_reported(personal_lt):
    """En ligne, LanguageTool répond 400 (langue qu'il ne connaît pas) : le message, en français, est affiché avec
    un code d'erreur. Un code inconnu de toutes les langues (« xx ») est refusé avant tout envoi."""
    out = run_cli("verifier", "-l", "xx", "Bonjour tout le monde.")
    assert out.returncode == 2 and "langue « xx » inconnue" in out.stderr and personal_lt == []
    out = run_cli("verifier", "-l", "nb", "Bonjour tout le monde.")
    assert personal_lt == ["/v2/check"]
    assert out.returncode == EXIT_UNCHECKED, out.stderr
    assert "Aucune faute" not in out.stdout
    assert "Aucun moteur n'a pu vérifier le texte" in out.stderr
    assert "LanguageTool ne connaît pas la langue « nb »" in out.stderr and "known" not in out.stderr


def test_offline_never_calls_a_personal_server(personal_lt):
    run_cli("verifier", "--hors-ligne", "Il faut que tu viens.")
    run_cli("corriger", "--hors-ligne", "-l", "xx", "Bonjour.")
    assert personal_lt == []


@pytest.mark.parametrize("mode, kept", [("public", False), ("premium", False), ("perso", False),
                                        ("mode inconnu", False), ("local", True)])
def test_offline_keeps_only_the_local_languagetool_server(home, mode, kept):
    store = SettingsStore()
    settings = store.load()
    settings.languagetool.mode = mode
    store.save(settings)
    checker = _make_checker(build_parser().parse_args(["verifier", "--hors-ligne", "texte"]))
    try:
        assert checker.engine("languagetool").is_enabled() is kept
    finally:
        checker.close()


def run_cli_bytes(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8", NO_COLOR="1")
    return subprocess.run([sys.executable, "-m", "correcteur", *args], capture_output=True, env=env, timeout=120)


def test_file_is_read_exactly(home, tmp_path):
    """-f : fins de ligne d'origine gardées (positions justes), BOM retiré."""
    path = tmp_path / "texte.txt"
    path.write_bytes(codecs.BOM_UTF8 + "Ligne un.\r\nLigne deux.\r\n".encode("utf-8"))
    out = run_cli_bytes("verifier", "--json", "--hors-ligne", "-f", str(path))
    assert json.loads(out.stdout.decode("utf-8"))["texte"] == "Ligne un.\r\nLigne deux.\r\n"


@needs_grammalecte
def test_corriger_file_keeps_crlf(home, tmp_path):
    path = tmp_path / "texte.txt"
    path.write_bytes(codecs.BOM_UTF8 + "Je vais a la plage.\r\nEt ecole est fermée.\r\nFin".encode("utf-8"))
    out = run_cli_bytes("corriger", "--hors-ligne", "-f", str(path))
    assert out.returncode == 0, out.stderr.decode("utf-8")
    assert out.stdout.decode("utf-8") == "Je vais à la plage.\r\nEt école est fermée.\r\nFin"


@needs_grammalecte
def test_file_not_in_utf8_is_read_as_ansi_with_a_remark(home, tmp_path, capsys):
    """Fichier ANSI (Windows-1252) : refusé avant, lu maintenant, avec une remarque sur la sortie d'erreur."""
    path = tmp_path / "ansi.txt"
    path.write_bytes("Le café est très bon.".encode("cp1252"))
    assert main(["corriger", "--hors-ligne", "-f", str(path)]) == 0
    out, err = capsys.readouterr()
    assert out == "Le café est très bon." and "n'est pas en UTF-8" in err and "ANSI" in err
    assert main(["corriger", "-f", str(tmp_path / "absent.txt")]) == 2
    assert "introuvable" in capsys.readouterr().err


@pytest.mark.parametrize("data, expected", [
    ("Élève à l'école".encode("utf-8"), ("Élève à l'école", "utf-8")),
    (b"\xef\xbb\xbf" + "Élève".encode("utf-8"), ("Élève", "utf-8")),
    (b"\xff\xfe" + "Élève\r\nou".encode("utf-16-le"), ("Élève\r\nou", "utf-16-le")),
    (b"\xfe\xff" + "Élève".encode("utf-16-be"), ("Élève", "utf-16-be")),
    ("Élève à l'école".encode("utf-16-le"), ("Élève à l'école", "utf-16-le")),  # Bloc-notes, PowerShell
    ("Élève à l'école".encode("cp1252"), ("Élève à l'école", "cp1252")),
    # L-07 : tout en ASCII, l'UTF-16 sans BOM est aussi de l'UTF-8 valide (octets nuls compris) : lu tel quel.
    ("Les enfants on faim.\n".encode("utf-16-le"), ("Les enfants on faim.\n", "utf-16-le")),
    ("Les enfants on faim.\n".encode("utf-16-be"), ("Les enfants on faim.\n", "utf-16-be")),
    ("Fin\n".encode("utf-8") + b"\0\0", ("Fin\n", "utf-8")),  # octets nuls de remplissage
], ids=["utf-8", "utf-8 BOM", "utf-16 LE BOM", "utf-16 BE BOM", "utf-16 sans BOM", "ansi", "utf-16 LE ascii",
        "utf-16 BE ascii", "remplissage"])
def test_decode_text_recognizes_windows_encodings(data, expected):
    from correcteur.cli import decode_text

    assert decode_text(data) == expected


@pytest.mark.parametrize("data", [
    b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" * 64 + bytes(range(256)) * 8,
    b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4,
    bytes(range(1, 32)) * 50,
], ids=["programme", "image", "contrôles"])
def test_decode_text_refuses_binary_data(data):
    """W-09 : pip.exe lu en ANSI, puis « vérifié » pendant 73 s."""
    from correcteur.cli import BinaryData, decode_text

    with pytest.raises(BinaryData):
        decode_text(data)


def test_binary_file_is_refused_at_once(home, tmp_path, capsys):
    path = tmp_path / "pip.exe"
    path.write_bytes(b"MZ\x90\x00" + bytes(range(256)) * 400)
    assert main(["verifier", "--hors-ligne", "-f", str(path)]) == 2
    assert "n'est pas un fichier texte" in capsys.readouterr().err


def test_locked_file_cause_is_named(home, tmp_path, capsys, monkeypatch):
    """W-11 : fichier ouvert en écriture exclusive par un autre programme (Windows : erreur 32), pas « accès
    refusé »."""
    import builtins

    def locked(*args, **kwargs):
        exc = PermissionError(13, "The process cannot access the file")
        exc.winerror = 32
        raise exc

    path = tmp_path / "verrou.txt"
    path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(builtins, "open", locked)
    assert main(["verifier", "-f", str(path)]) == 2
    assert "(fichier ouvert par un autre programme)" in capsys.readouterr().err


def test_decode_text_uses_the_console_code_page_for_pipes():
    """« echo levé| correcteur-cli » dans cmd : octets en page de code 850, lus en 1252 avant (« lev‚ »)."""
    from correcteur.cli import decode_text

    assert decode_text("Il ces levé tôt".encode("cp850"), "cp850") == ("Il ces levé tôt", "cp850")
    assert decode_text("Il ces levé tôt".encode("utf-8"), "cp850")[0] == "Il ces levé tôt"  # PowerShell 7


@needs_grammalecte
def test_utf16_file_is_corrected(home, tmp_path, capsys):
    path = tmp_path / "utf16.txt"
    path.write_bytes(b"\xff\xfe" + "Je vais a la plage.".encode("utf-16-le"))
    assert main(["corriger", "--hors-ligne", "-f", str(path)]) == 0
    out, err = capsys.readouterr()
    assert out == "Je vais à la plage." and "UTF-16" in err


def test_json_errors_stay_json(home, tmp_path, capsys):
    """Avec --json, toute erreur sort en JSON sur la sortie standard, en français : fichier absent ou dossier,
    option inconnue, langue inconnue (refusée avant l'envoi à LanguageTool)."""
    import json

    assert main(["verifier", "--json", "-f", str(tmp_path / "absent.txt")]) == 2
    assert "introuvable" in json.loads(capsys.readouterr().out)["erreur"]
    assert main(["verifier", "--json", "-f", str(tmp_path)]) == 2
    assert "est un dossier" in json.loads(capsys.readouterr().out)["erreur"]
    with pytest.raises(SystemExit) as stop:
        main(["verifier", "--json", "--inconnu", "Bonjour"])
    assert stop.value.code == 2
    assert json.loads(capsys.readouterr().out)["erreur"] == "arguments inconnus : --inconnu"
    assert main(["verifier", "--json", "-l", "zz", "Bonjour"]) == 2
    assert "langue « zz » inconnue" in json.loads(capsys.readouterr().out)["erreur"]


def test_unreadable_file_cause_is_in_french(home, tmp_path, capsys, monkeypatch):
    import builtins

    def refused(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    path = tmp_path / "secret.txt"
    path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(builtins, "open", refused)
    assert main(["verifier", "-f", str(path)]) == 2
    err = capsys.readouterr().err
    assert "(accès refusé)" in err and "Permission" not in err


def test_help_is_in_french_and_hides_gnome_on_windows(monkeypatch):
    from correcteur.cli import build_parser

    text = build_parser().format_help()
    assert "utilisation :" in text and "afficher cette aide" in text and "usage:" not in text
    monkeypatch.setattr(sys, "platform", "win32")
    assert "raccourcis-gnome" not in build_parser().format_help()


def test_unencodable_characters_get_plain_equivalents():
    from correcteur import cli  # noqa: F401  (enregistre le gestionnaire « correcteur »)

    assert "C’est l’œuvre… 10 €".encode("cp850", errors="correcteur") == b"C'est l'oeuvre... 10 EUR"


def test_language_codes_accept_languagetool_variants():
    from correcteur.cli import _normalize_language

    assert _normalize_language("EN-us") == "en-US"
    assert _normalize_language("ca-ES-valencia") == "ca-ES-valencia"
    assert _normalize_language("de-DE-x-simple-language") == "de-DE-x-simple-language"


def test_old_mac_line_endings_split_paragraphs():
    from correcteur.textutils import split_paragraphs

    assert list(split_paragraphs("Un.\rDeux.\r\nTrois.")) == [(0, "Un."), (4, "Deux."), (11, "Trois.")]


def test_partial_check_is_reported():
    from correcteur.cli import _partial_warning
    from correcteur.models import CheckResult, EngineStatus

    statuses = {"grammalecte": EngineStatus("grammalecte", True), "languagetool": EngineStatus(
        "languagetool", False, "LanguageTool a mis plus de 8 s à répondre.")}
    assert "plus de 8 s" in _partial_warning(CheckResult("t", [], statuses, ()))
    assert _partial_warning(CheckResult("t", [], {"grammalecte": statuses["grammalecte"]}, ())) is None


def test_pythonioencoding_is_applied_even_if_python_ignored_it(monkeypatch):
    """L'exécutable empaqueté ignore PYTHONIOENCODING : sa sortie vers un fichier partait en Windows-1252 (« à »
    devenait l'octet 0xE0), et le test de l'exécutable de la CI, qui relit en UTF-8, échouait."""
    import io

    from correcteur import cli

    def stream():
        return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")

    out, err = stream(), stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    cli._utf8_io()
    assert out.encoding == "utf-8" and err.encoding == "utf-8"
    monkeypatch.setenv("PYTHONIOENCODING", "cp850:strict")
    cli._utf8_io()
    assert out.encoding == "cp850" and out.errors == "strict"
    monkeypatch.delenv("PYTHONIOENCODING")
    cli._utf8_io()
    if sys.platform != "win32":
        assert out.encoding == "utf-8" and out.errors == "replace"


def test_stdin_follows_pythonioencoding(monkeypatch):
    import io

    from correcteur import cli

    raw = io.BytesIO("Il ces levé tôt".encode("utf-8"))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(raw, encoding="cp1252"))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    assert cli._read_stdin() == "Il ces levé tôt"


def test_utf8_option_keeps_pipes_lossless(monkeypatch):
    """W-03 : dans un tube sous Windows, la sortie suit la page de code de la console (850) et « œ », « ’ », « … »
    y deviennent « oe », « ' », « ... ». --utf8 garde l'UTF-8, sans perte."""
    import io

    from correcteur import cli

    out, err = (io.TextIOWrapper(io.BytesIO(), encoding="cp1252") for _ in range(2))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_windows_stream", lambda number: "tube")
    monkeypatch.setattr(cli, "_console_code_page", lambda output=True: "cp850")
    cli._utf8_io()
    assert out.encoding == "cp850"
    cli._utf8_io(utf8=True)
    assert out.encoding == "utf-8"
