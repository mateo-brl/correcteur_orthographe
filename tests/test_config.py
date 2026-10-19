import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from correcteur.config import PersonalDictionary, Settings, SettingsStore, config_dir


def test_settings_roundtrip(home):
    store = SettingsStore()
    settings = Settings()
    settings.general.typography = "stricte"
    settings.languagetool.mode = "local"
    settings.ignored_rules = ["REGLE_1"]
    store.save(settings)
    loaded = store.load()
    assert loaded.general.typography == "stricte"
    assert loaded.languagetool.mode == "local"
    assert loaded.ignored_rules == ["REGLE_1"]
    assert loaded.strict_typography


def test_unknown_keys_and_bad_types_are_ignored(home):
    store = SettingsStore()
    store.path.write_text(json.dumps({
        "general": {"language": "en-US", "live_check": "oui", "inconnu": 1},
        "languagetool": {"local_port": "8081", "picky": False},
        "ai": {"enabled": True},
    }), encoding="utf-8")
    loaded = store.load()
    assert loaded.general.language == "en-US"
    assert loaded.general.live_check is True          # "oui" n'est pas un booléen : valeur par défaut
    assert loaded.languagetool.local_port == 8081     # chaîne refusée : valeur par défaut
    assert loaded.languagetool.picky is False


def test_corrupted_settings_fall_back_to_defaults(home):
    store = SettingsStore()
    store.path.write_text("{ pas du json", encoding="utf-8")
    assert store.load() == Settings()
    assert store.path.with_suffix(".json.corrompu").exists()


def test_settings_copy_is_independent():
    a = Settings()
    b = a.copy()
    b.general.language = "auto"
    b.ignored_rules.append("X")
    assert a.general.language == "fr" and a.ignored_rules == []


def test_personal_dictionary_case_rules(home):
    d = PersonalDictionary()
    d.add("stp")
    d.add("Mateo")
    assert "stp" in d and "Stp" in d and "STP" in d
    assert "Mateo" in d and "mateo" not in d
    d.add("aujourd’hui")
    assert "aujourd'hui" in d
    # Persistance
    again = PersonalDictionary()
    assert "Mateo" in again and "stp" in again
    again.remove("stp")
    assert "stp" not in PersonalDictionary()


def test_dictionary_set_words(home):
    d = PersonalDictionary()
    d.set_words(["  un ", "", "Deux"])
    assert d.words() == ["Deux", "un"]


def test_dictionary_not_in_utf8_keeps_every_word(home):
    """Fichier modifié avec un éditeur en Latin-1 / Windows-1252 : plus de plantage au démarrage, et aucun mot
    perdu (une ligne illisible en UTF-8 est relue en Windows-1252)."""
    d = PersonalDictionary()
    d.path.write_bytes("Zorglub\ncafé\nmaïs\n".encode("utf-8") + "crème\nœuvrette\n".encode("cp1252"))
    d.reload()
    assert {"Zorglub", "café", "maïs", "crème", "œuvrette"} <= set(d.words())
    d.add("nouveau")  # réécrit en UTF-8
    assert set(PersonalDictionary().words()) == {"Zorglub", "café", "maïs", "crème", "œuvrette", "nouveau"}
    d.path.write_bytes(b"\xef\xbb\xbfbom\r\nfin\r\n")  # BOM et fins de ligne Windows
    d.reload()
    assert d.words() == ["bom", "fin"]


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_dictionary_in_utf16_is_read_and_rewritten_in_utf8(home, encoding):
    """Fichier enregistré en « Unicode » par le Bloc-notes de Windows (UTF-16 avec marque d'ordre des octets) :
    lu comme de l'UTF-8, il donnait des mots pleins de caractères nuls, réécrits tels quels."""
    d = PersonalDictionary()
    d.path.write_bytes(("\ufeffZorglub\r\ncafé\r\n# commentaire\r\nœuvrette\r\n").encode(encoding))
    d.reload()
    assert d.words() == ["café", "Zorglub", "œuvrette"]
    d.add("nouveau")
    assert d.path.read_bytes().decode("utf-8") == "café\nnouveau\nZorglub\nœuvrette\n"
    assert PersonalDictionary().words() == ["café", "nouveau", "Zorglub", "œuvrette"]


@pytest.mark.skipif(os.name != "posix", reason="droits POSIX")
def test_dictionary_open_to_others_is_made_private_on_load(home):
    """Un dictionnaire.txt créé en 664 par la 0.1.6 le restait tant qu'aucun mot n'était ajouté."""
    path = config_dir() / "dictionnaire.txt"
    path.write_text("Zorglub\n", encoding="utf-8")
    os.chmod(path, 0o664)
    assert "Zorglub" in PersonalDictionary()
    assert (os.stat(path).st_mode & 0o777) == 0o600
    os.chmod(path, 0o400)  # déjà fermé aux autres : pas touché
    PersonalDictionary()
    assert (os.stat(path).st_mode & 0o777) == 0o400


def test_unreadable_dictionary_is_neither_fatal_nor_overwritten(home):
    (config_dir() / "dictionnaire.txt").mkdir()  # illisible comme fichier
    d = PersonalDictionary()
    assert d.words() == []
    d.add("mot")  # gardé pour la session, sans écraser le fichier
    assert "mot" in d and d.path.is_dir()


def test_cli_survives_latin1_dictionary(home):
    (config_dir() / "dictionnaire.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), PYTHONIOENCODING="utf-8")
    out = subprocess.run([sys.executable, "-m", "correcteur", "verifier", "--hors-ligne", "Bonjour."],
                         capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    assert out.returncode != 2 and "codec" not in out.stderr, out.stderr


@pytest.mark.skipif(os.name != "posix", reason="droits POSIX")
def test_dictionary_file_is_private(home):
    """Comme parametres.json : les mots personnels (noms, adresses) ne sont pas lisibles par les autres comptes."""
    d = PersonalDictionary()
    d.add("Zorglub")
    assert stat.S_IMODE(d.path.stat().st_mode) == 0o600


def test_portable_version_keeps_its_data_next_to_it(tmp_path, monkeypatch):
    """M7 : la version portable lisait et écrivait %LOCALAPPDATA%\\Correcteur, comme la version installée."""
    import sys

    from correcteur import config

    monkeypatch.delenv("CORRECTEUR_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Mes essais été" / "Correcteur.exe"))
    (tmp_path / "Mes essais été").mkdir()
    config.portable_dir.cache_clear()
    try:
        assert config.portable_dir() is None  # sans portable.txt : version installée
        (tmp_path / "Mes essais été" / "portable.txt").write_text("portable", encoding="utf-8")
        config.portable_dir.cache_clear()
        data = tmp_path / "Mes essais été" / "Données"
        assert config.portable_dir() == data
        assert config.config_dir() == data and config.data_dir() == data and config.log_path().parent == data
    finally:
        config.portable_dir.cache_clear()
