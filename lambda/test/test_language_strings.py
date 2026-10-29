"""
E.1 — Vérifie que language_strings.json contient toutes les clés de prompts.py
dans toutes les locales de base (en, de, fr, it, pt, es).

Si une clé est ajoutée à prompts.py et oubliée dans une locale, ce test échoue.
"""

import json
from pathlib import Path

LAMBDA_DIR = Path(__file__).parent.parent
STRINGS_FILE = LAMBDA_DIR / "language_strings.json"
PROMPTS_FILE = LAMBDA_DIR / "prompts.py"

BASE_LOCALES = ["en", "de", "fr", "it", "pt", "es"]


def _load_prompt_keys():
    """Extrait les noms de constantes de prompts.py (lignes KEY = "KEY")."""
    keys = []
    for line in PROMPTS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("=", 1)
        if len(parts) == 2:
            keys.append(parts[0].strip())
    return keys


def _load_strings():
    return json.loads(STRINGS_FILE.read_text())


class TestLanguageStringsCompleteness:
    def test_all_prompt_keys_exist_in_all_base_locales(self):
        """Chaque constante de prompts.py doit exister dans chaque locale de base."""
        prompt_keys = _load_prompt_keys()
        strings = _load_strings()

        missing = {}
        for locale in BASE_LOCALES:
            locale_data = strings.get(locale, {})
            absent = [k for k in prompt_keys if k not in locale_data]
            if absent:
                missing[locale] = absent

        assert not missing, (
            "Clés manquantes dans language_strings.json :\n"
            + "\n".join(f"  [{loc}] {', '.join(keys)}" for loc, keys in missing.items())
        )

    def test_no_extra_locales_missing_base_keys(self):
        """Toutes les locales de base sont bien présentes dans le fichier."""
        strings = _load_strings()
        absent_locales = [loc for loc in BASE_LOCALES if loc not in strings]
        assert not absent_locales, f"Locales manquantes dans le fichier : {absent_locales}"

    def test_strings_file_is_valid_json(self):
        """Le fichier est du JSON valide (vérifie que le parse ne lève pas d'exception)."""
        data = _load_strings()
        assert isinstance(data, dict)

    def test_prompt_keys_are_non_empty(self):
        """prompts.py expose au moins les clés historiques minimales."""
        keys = _load_prompt_keys()
        required = {"ERROR_CONFIG", "HELP_MESSAGE", "SKILL_NAME", "HINT_TEXT", "NO_MATCH"}
        assert required.issubset(set(keys)), f"Clés manquantes dans prompts.py : {required - set(keys)}"
