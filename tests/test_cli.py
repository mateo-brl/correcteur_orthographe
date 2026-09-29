from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import needs_grammalecte

from correcteur.cli import build_parser, main

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
