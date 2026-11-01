"""
Tests structurels des modèles d'interaction (skill-package/interactionModels/custom/*.json).

Le skill ne sert qu'à répondre aux questions des scénarios Jeedom : chaque
locale définit les intents de réponse (oui/non, nombre, durée, date, choix,
texte) et aucun intent de commande libre (rôle du skill Frida).

Aucune dépendance externe — pure stdlib + pytest.
"""

import json
import re
from pathlib import Path

import pytest

# Locales supportées en production
SUPPORTED_LOCALES = ["de-DE", "en-AU", "en-CA", "en-GB", "en-IN", "en-US", "es-ES", "fr-CA", "fr-FR"]

# Intents que CHAQUE locale doit définir
REQUIRED_INTENTS = {
    "AMAZON.HelpIntent", "AMAZON.CancelIntent", "AMAZON.StopIntent",
    "AMAZON.FallbackIntent", "AMAZON.RepeatIntent", "AMAZON.YesIntent", "AMAZON.NoIntent",
    "Number", "Duration", "Date", "Select", "String",
}

# Slots attendus (noms lus par lambda_function.py)
EXPECTED_SLOTS = {
    "Number": [{"name": "Numbers", "type": "AMAZON.NUMBER"}, {"name": "Ordinal", "type": "AMAZON.Ordinal"}],
    "Duration": [{"name": "Durations", "type": "AMAZON.DURATION"}],
    "Date": [{"name": "Dates", "type": "AMAZON.DATE"}, {"name": "Times", "type": "AMAZON.TIME"}],
    "Select": [{"name": "Selections", "type": "AMAZON.SearchQuery"}],
    "String": [{"name": "Strings", "type": "AMAZON.Person"}],
}

# Réponses nues indispensables (« douze », « dix minutes », « demain », « rouge »)
BARE_SAMPLES = {"Number": "{Numbers}", "Duration": "{Durations}", "Date": "{Dates}", "String": "{Strings}"}

MODELS_DIR = Path(__file__).parent.parent.parent / "skill-package" / "interactionModels" / "custom"


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
def test_qa_intents_only(locale):
    intents = set(_intents_by_name(_load_model(locale)))
    missing = REQUIRED_INTENTS - intents
    assert not missing, f"{locale}: intents manquants {missing}"
    extra = intents - REQUIRED_INTENTS - {"AMAZON.NavigateHomeIntent"}
    assert not extra, f"{locale}: intents en trop (commandes libres → Frida) {extra}"


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_answer_slots(locale):
    intents = _intents_by_name(_load_model(locale))
    for name, slots in EXPECTED_SLOTS.items():
        assert intents[name]["slots"] == slots, f"{locale}/{name}: slots {intents[name]['slots']}"
        for sample in intents[name]["samples"]:
            used = set(re.findall(r"\{(\w+)\}", sample))
            assert used and used <= {s["name"] for s in slots}, f"{locale}/{name}: sample {sample!r}"


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_bare_answers_accepted(locale):
    intents = _intents_by_name(_load_model(locale))
    for name, sample in BARE_SAMPLES.items():
        assert sample in intents[name]["samples"], f"{locale}/{name}: réponse nue {sample} absente"


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_search_query_has_carrier_phrase(locale):
    """AMAZON.SearchQuery exige au moins un mot avant le slot (sinon build refusé)."""
    for sample in _intents_by_name(_load_model(locale))["Select"]["samples"]:
        assert sample.split("{Selections}")[0].strip(), f"{locale}/Select: sample sans amorce {sample!r}"


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
