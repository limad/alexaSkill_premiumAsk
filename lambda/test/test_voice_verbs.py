"""
Tests de cohérence du dictionnaire VOICE_VERBS dans lambda_function.py.

VOICE_VERBS définit le verbe ajouté en préfixe à la phrase utilisateur avant
de poster à Jeedom — un par couple (locale, intent). Une omission silencieuse
casse la commande vocale dans cette locale.

Le dict est extrait du fichier source par parsing AST pour ne pas charger
ask_sdk_core (non installé en CI light).
"""

import ast
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def voice_verbs():
    src = (Path(__file__).parent.parent / "lambda_function.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "VOICE_VERBS":
                    return ast.literal_eval(node.value)
    raise RuntimeError("VOICE_VERBS introuvable dans lambda_function.py")


REQUIRED_INTENT_KEYS = {"VoiceLaunch", "VoiceTurnOn", "VoiceTurnOff", "VoiceSet", "VoiceQuery"}
EXPECTED_LOCALES = {"fr", "en", "es", "de", "it", "pt"}


def test_voice_verbs_has_all_locales(voice_verbs):
    missing = EXPECTED_LOCALES - set(voice_verbs.keys())
    assert not missing, f"Locales manquantes : {missing}"


@pytest.mark.parametrize("locale", sorted(EXPECTED_LOCALES))
def test_each_locale_has_all_intents(voice_verbs, locale):
    keys = set(voice_verbs[locale].keys())
    missing = REQUIRED_INTENT_KEYS - keys
    assert not missing, f"Locale '{locale}' : intents manquants {missing}"


INTENTS_EMPTY_VERB_ALLOWED = {"VoiceQuery"}  # passe le slot tel quel à Jeedom


@pytest.mark.parametrize("locale", sorted(EXPECTED_LOCALES))
def test_each_verb_is_non_empty_string(voice_verbs, locale):
    for intent, verb in voice_verbs[locale].items():
        assert isinstance(verb, str), f"{locale}/{intent}: type {type(verb)}"
        if intent not in INTENTS_EMPTY_VERB_ALLOWED:
            assert verb.strip(), f"{locale}/{intent}: verbe vide"


def test_french_verbs_canonical():
    """Garantit que les verbes français sont les bons (régression)."""
    src = (Path(__file__).parent.parent / "lambda_function.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    voice_verbs = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "VOICE_VERBS":
                    voice_verbs = ast.literal_eval(node.value)
    assert voice_verbs["fr"]["VoiceLaunch"] == "activer"
    assert voice_verbs["fr"]["VoiceTurnOn"] == "allumer"
    assert voice_verbs["fr"]["VoiceTurnOff"] == "éteindre"
    assert voice_verbs["fr"]["VoiceSet"] == "régler"
    assert voice_verbs["fr"]["VoiceQuery"] == ""  # VoiceQuery passe le slot tel quel
