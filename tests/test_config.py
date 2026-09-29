import json

from correcteur.config import PersonalDictionary, Settings, SettingsStore


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
