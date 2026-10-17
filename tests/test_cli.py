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
    out = run_cli("verifier", "--json", "--hors-ligne", "-l", "en", "This is a speling mistake.")
    assert out.returncode == EXIT_UNCHECKED
    assert json.loads(out.stdout)["moteurs"] == {}
    assert "Aucun moteur" in out.stderr


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
    """En ligne avec -l xx, LanguageTool répond 400 : son message est affiché, avec un code d'erreur."""
    out = run_cli("verifier", "-l", "xx", "Bonjour tout le monde.")
    assert personal_lt == ["/v2/check"]
    assert out.returncode == EXIT_UNCHECKED, out.stderr
    assert "Aucune faute" not in out.stdout
    assert "Aucun moteur n'a pu vérifier le texte" in out.stderr and "400" in out.stderr


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


def test_file_not_in_utf8_gives_a_french_message(home, tmp_path, capsys):
    path = tmp_path / "latin1.txt"
    path.write_bytes("Le café est très bon.".encode("latin-1"))
    assert main(["verifier", "--hors-ligne", "-f", str(path)]) == 2
    err = capsys.readouterr().err
    assert "n'est pas en UTF-8" in err and "codec" not in err
    assert main(["corriger", "-f", str(tmp_path / "absent.txt")]) == 2
    assert "introuvable" in capsys.readouterr().err
