"""
Tests structurels des modèles d'interaction (interactionModels/custom/*.json).

Objectifs :
- Garantir que chaque locale supportée définit tous les intents Voice* + Disambiguation
- Vérifier la cohérence (slots typés, samples non vides, carrier phrases pour SearchQuery)
- Détecter les régressions silencieuses lors de l'ajout d'une locale ou d'un intent

Aucune dépendance externe — pure stdlib + pytest.
"""

import json
import os
import re
from pathlib import Path

import pytest

# Locales supportées en production
SUPPORTED_LOCALES = ["en-AU", "en-CA", "en-GB", "en-IN", "en-US", "es-ES", "fr-CA", "fr-FR"]

# Intents que CHAQUE locale doit définir
REQUIRED_INTENTS = {
    # Built-in Amazon
    "AMAZON.HelpIntent",
    "AMAZON.CancelIntent",
    "AMAZON.StopIntent",
    "AMAZON.FallbackIntent",
    "AMAZON.RepeatIntent",
    "AMAZON.YesIntent",
    "AMAZON.NoIntent",
    # Custom Q/A
    "String",
    "Select",
    "Number",
    # Voice control direct
    "VoiceLaunch",
    "VoiceTurnOn",
    "VoiceTurnOff",
    "VoiceSet",
    "VoiceQuery",
    # Disambiguation (item 2.3)
    "DisambiguationIntent",
}

MODELS_DIR = Path(__file__).parent.parent.parent / "interactionModels" / "custom"


def _load_model(locale: str) -> dict:
    path = MODELS_DIR / f"{locale}.json"
    assert path.exists(), f"Modèle absent : {path}"
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _intents_by_name(model: dict) -> dict:
    return {it["name"]: it for it in model["interactionModel"]["languageModel"]["intents"]}


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_model_loads_and_has_required_keys(locale):
    model = _load_model(locale)
    assert "interactionModel" in model
    lm = model["interactionModel"]["languageModel"]
    assert "invocationName" in lm
    assert lm["invocationName"]
    assert "intents" in lm and isinstance(lm["intents"], list)


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_invocation_name_lowercase_alphanumeric(locale):
    """Amazon exige invocation name en minuscules sans caractères spéciaux."""
    inv = _load_model(locale)["interactionModel"]["languageModel"]["invocationName"]
    assert inv == inv.lower(), f"{locale}: invocationName doit être en minuscules"
    assert re.match(r"^[a-z0-9 ]+$", inv), f"{locale}: invocationName chars invalides"


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_all_required_intents_present(locale):
    model = _load_model(locale)
    intents = _intents_by_name(model)
    missing = REQUIRED_INTENTS - set(intents.keys())
    assert not missing, f"{locale}: intents manquants {missing}"


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_voice_intents_have_searchquery_carrier_phrases(locale):
    """
    AMAZON.SearchQuery exige une carrier phrase (verbe avant le slot).
    Échec courant : sample = "{Command}" seul → rejet par Amazon au build.
    """
    intents = _intents_by_name(_load_model(locale))
    for name in ("VoiceLaunch", "VoiceTurnOn", "VoiceTurnOff", "VoiceSet"):
        intent = intents[name]
        assert intent.get("slots"), f"{locale}/{name}: pas de slot"
        slot = intent["slots"][0]
        assert slot["type"] == "AMAZON.SearchQuery", f"{locale}/{name}: slot doit être SearchQuery"
        assert slot["name"] == "Command"
        assert intent["samples"], f"{locale}/{name}: aucun sample"
        for sample in intent["samples"]:
            assert "{Command}" in sample, f"{locale}/{name}: sample sans {{Command}}: {sample}"
            # Carrier phrase = au moins 1 mot avant le slot
            before = sample.split("{Command}")[0].strip()
            assert before, f"{locale}/{name}: sample sans verbe carrier: '{sample}'"


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_voice_query_intent_uses_query_slot(locale):
    """VoiceQuery doit utiliser le slot Query lu par lambda_function.py."""
    intents = _intents_by_name(_load_model(locale))
    intent = intents["VoiceQuery"]
    assert intent.get("slots"), f"{locale}/VoiceQuery: pas de slot"
    slot = intent["slots"][0]
    assert slot["type"] == "AMAZON.SearchQuery", f"{locale}/VoiceQuery: slot doit être SearchQuery"
    assert slot["name"] == "Query"
    assert intent["samples"], f"{locale}/VoiceQuery: aucun sample"
    for sample in intent["samples"]:
        assert "{Query}" in sample, f"{locale}/VoiceQuery: sample sans {{Query}}: {sample}"
        before = sample.split("{Query}")[0].strip()
        assert before, f"{locale}/VoiceQuery: sample sans carrier phrase: '{sample}'"


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_disambiguation_intent_uses_amazon_number(locale):
    """DisambiguationIntent doit avoir un slot Choice typé AMAZON.NUMBER."""
    intents = _intents_by_name(_load_model(locale))
    intent = intents["DisambiguationIntent"]
    assert intent["slots"], f"{locale}: DisambiguationIntent sans slot"
    slot = intent["slots"][0]
    assert slot["name"] == "Choice", f"{locale}: slot doit s'appeler Choice"
    assert slot["type"] == "AMAZON.NUMBER", f"{locale}: slot Choice doit être AMAZON.NUMBER"
    assert intent["samples"], f"{locale}: aucun sample pour DisambiguationIntent"
    # Au moins un sample doit utiliser le slot
    assert any("{Choice}" in s for s in intent["samples"]), \
        f"{locale}: aucun sample utilisant {{Choice}}"
    assert "{Choice}" not in intent["samples"], \
        f"{locale}: sample nu {{Choice}} trop large, risque de faux match"


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_no_duplicate_samples_within_intent(locale):
    """Détection régression : doublons de samples dans un même intent (build warning)."""
    intents = _intents_by_name(_load_model(locale))
    for name, intent in intents.items():
        samples = intent.get("samples", [])
        if not samples:
            continue
        dups = [s for s in samples if samples.count(s) > 1]
        assert not dups, f"{locale}/{name}: samples dupliqués {set(dups)}"


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_yes_no_samples_not_overlapping(locale):
    """
    Détection régression : un sample ne peut pas être à la fois Yes et No.
    AMAZON.YesIntent / AMAZON.NoIntent : on accepte qu'ils ne définissent pas
    de samples custom (Amazon fournit les samples natifs), mais s'ils en ont,
    ils ne doivent pas se chevaucher.
    """
    intents = _intents_by_name(_load_model(locale))
    yes = intents.get("AMAZON.YesIntent", {})
    no = intents.get("AMAZON.NoIntent", {})
    yes_samples = set(s.lower().strip() for s in yes.get("samples", []) or [])
    no_samples = set(s.lower().strip() for s in no.get("samples", []) or [])
    overlap = yes_samples & no_samples
    assert not overlap, f"{locale}: samples ambigus Yes/No: {overlap}"


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_voice_intent_samples_distinct_across_intents(locale):
    """
    Les verbes carriers de Voice* doivent être différenciés sinon collision NLU
    (ex: "allume X" doit être seulement dans VoiceTurnOn, pas aussi VoiceLaunch).
    """
    intents = _intents_by_name(_load_model(locale))
    voice_names = ("VoiceLaunch", "VoiceTurnOn", "VoiceTurnOff", "VoiceSet")
    samples_by_intent = {n: set(s.lower() for s in intents[n].get("samples", [])) for n in voice_names}
    for n1 in voice_names:
        for n2 in voice_names:
            if n1 >= n2:
                continue
            overlap = samples_by_intent[n1] & samples_by_intent[n2]
            assert not overlap, f"{locale}: samples partagés entre {n1} et {n2}: {overlap}"
