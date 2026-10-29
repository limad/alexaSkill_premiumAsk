"""
Tests du dispatch voice control : _handle_voice_intent + DisambiguationIntentHandler.

Stratégie : mock le HTTP via JeeAsk._request, mock handler_input avec un objet
factice, vérifier que les bonnes branches du flow sont prises (matched / ambiguous).

Ces tests exercent la logique applicative du fichier lambda_function.py, pas le
vrai dispatch Alexa.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ajoute lambda/ au path pour que `import lambda_function` fonctionne
LAMBDA_DIR = Path(__file__).parent.parent
if str(LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(LAMBDA_DIR))


@pytest.fixture
def lambda_mod():
    """Charge lambda_function après que conftest.py ait stub-é ask_sdk_core."""
    import lambda_function
    return lambda_function


def _fake_response(body_dict):
    """Simule une urllib3.HTTPResponse."""
    rsp = MagicMock()
    rsp.data = json.dumps(body_dict).encode("utf-8")
    rsp.status = 200
    return rsp


def _make_handler_input(intent_name="VoiceTurnOn", slots=None, locale="fr-FR",
                         session_attrs=None, request_attrs=None):
    hi = MagicMock()
    hi._intent_name = intent_name
    hi._slots = slots or {}
    hi._slot_objects = {}
    for name, value in hi._slots.items():
        slot = MagicMock()
        slot.value = value
        slot.resolutions = None
        hi._slot_objects[name] = slot
    hi.request_envelope.request.locale = locale

    # Session attributes (mutable dict)
    sess = session_attrs if session_attrs is not None else {}
    hi.attributes_manager.session_attributes = sess
    hi.attributes_manager.request_attributes = request_attrs or {"_": {}}

    # Context system / device / person
    hi.request_envelope.context.system.device.device_id = "amzn1.test.device"
    hi.request_envelope.context.system.person = None  # voix non identifiée par défaut

    # response_builder chainable
    hi.response_builder = MagicMock()
    hi.response_builder.speak.return_value = hi.response_builder
    hi.response_builder.ask.return_value = hi.response_builder
    hi.response_builder.set_should_end_session.return_value = hi.response_builder
    hi.response_builder.response = MagicMock(name="response")
    return hi


# ────────────────────────────────────────────────────────────────────────────
# JeeAsk.post_voice_command — parsing des 3 réponses possibles
# ────────────────────────────────────────────────────────────────────────────

class TestPostVoiceCommandParsing:
    def _build_jee(self, lambda_mod, response_payload):
        """Instancie JeeAsk avec _request retournant un payload contrôlé."""
        hi = _make_handler_input()
        jee = lambda_mod.JeeAsk(hi, fetch_question=False)
        # patch _request sur l'instance pour les appels suivants
        jee._request = MagicMock(return_value=_fake_response(response_payload))
        # language_strings mocké
        jee.language_strings = {"ERROR_CONFIG": "Je n'ai pas compris."}
        return jee

    def test_matched_response(self, lambda_mod):
        jee = self._build_jee(lambda_mod, {
            "reply": "Salon allumé", "matched": True, "query": "allumer le salon"
        })
        result = jee.post_voice_command("allumer le salon")
        assert result["reply"] == "Salon allumé"
        assert result["matched"] is True
        assert result["ambiguous"] is False
        assert result["options"] == []

    def test_ambiguous_response(self, lambda_mod):
        jee = self._build_jee(lambda_mod, {
            "reply": "1 : allume le salon, 2 : allume le salon TV. Laquelle ?",
            "matched": False, "ambiguous": True,
            "options": ["allume le salon", "allume le salon TV"],
            "query": "allumer le salon"
        })
        result = jee.post_voice_command("allumer le salon")
        assert result["matched"] is False
        assert result["ambiguous"] is True
        assert len(result["options"]) == 2
        assert "allume le salon" in result["options"]

    def test_empty_query(self, lambda_mod):
        jee = self._build_jee(lambda_mod, {})
        result = jee.post_voice_command("")
        assert result["matched"] is False
        assert result["ambiguous"] is False
        assert result["reply"]  # fallback non vide

    def test_force_exact_passes_through(self, lambda_mod):
        jee = self._build_jee(lambda_mod, {"reply": "Ok", "matched": True})
        jee.post_voice_command("xyz", force_exact=True)
        # Vérifie que forceExact a bien été envoyé
        call_kwargs = jee._request.call_args
        assert call_kwargs is not None
        body = call_kwargs.kwargs.get("body") or (call_kwargs.args[2] if len(call_kwargs.args) > 2 else None)
        assert body is not None
        assert body.get("forceExact") is True


# ────────────────────────────────────────────────────────────────────────────
# _handle_voice_intent — préfixage verbe + branche ambiguous
# ────────────────────────────────────────────────────────────────────────────

class TestHandleVoiceIntent:
    def test_prefix_verb_french(self, lambda_mod):
        hi = _make_handler_input(slots={"Command": "le salon"}, locale="fr-FR")

        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = MockJee.return_value
            inst.post_voice_command.return_value = {
                "reply": "Salon allumé", "matched": True,
                "ambiguous": False, "options": [],
            }
            lambda_mod._handle_voice_intent(hi, "VoiceTurnOn")
            # Le verbe "allumer" doit avoir été préfixé
            inst.post_voice_command.assert_called_once_with("allumer le salon")

    def test_prefix_verb_english(self, lambda_mod):
        hi = _make_handler_input(slots={"Command": "the lights"}, locale="en-US")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = MockJee.return_value
            inst.post_voice_command.return_value = {
                "reply": "Done", "matched": True, "ambiguous": False, "options": [],
            }
            lambda_mod._handle_voice_intent(hi, "VoiceTurnOn")
            inst.post_voice_command.assert_called_once_with("turn on the lights")

    def test_ambiguous_stores_options_in_session(self, lambda_mod):
        hi = _make_handler_input(slots={"Command": "le salon"}, locale="fr-FR",
                                  session_attrs={})
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = MockJee.return_value
            inst.post_voice_command.return_value = {
                "reply": "1 : A, 2 : B. Laquelle ?",
                "matched": False, "ambiguous": True,
                "options": ["allume le salon A", "allume le salon B"],
            }
            lambda_mod._handle_voice_intent(hi, "VoiceTurnOn")
            # Options stockées dans la session
            sess = hi.attributes_manager.session_attributes
            assert "pending_voice_options" in sess
            assert len(sess["pending_voice_options"]) == 2
            # Session doit rester ouverte (set_should_end_session(False))
            hi.response_builder.set_should_end_session.assert_called_with(False)

    def test_matched_ends_session(self, lambda_mod):
        hi = _make_handler_input(slots={"Command": "le salon"}, locale="fr-FR")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = MockJee.return_value
            inst.post_voice_command.return_value = {
                "reply": "Ok", "matched": True, "ambiguous": False, "options": [],
            }
            lambda_mod._handle_voice_intent(hi, "VoiceTurnOn")
            hi.response_builder.set_should_end_session.assert_called_with(True)


# ────────────────────────────────────────────────────────────────────────────
# Date intent mal routé — fallback vers Jeedom voiceRouter
# ────────────────────────────────────────────────────────────────────────────

class TestMisroutedDateIntent:
    def test_date_without_slots_forwards_raw_text_to_jeedom(self, lambda_mod):
        hi = _make_handler_input(
            intent_name="Date",
            slots={},
            locale="fr-FR",
        )
        hi.request_envelope.request.input_transcript = "eteindre le four"

        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = MockJee.return_value
            inst.post_voice_command.return_value = {
                "reply": "Le four est éteint",
                "matched": True,
                "ambiguous": False,
                "options": [],
                "http_error": False,
            }
            handler = lambda_mod.DateTimeIntentHandler()
            handler.handle(hi)

        MockJee.assert_called_once_with(hi, fetch_question=False)
        inst.post_voice_command.assert_called_once_with("eteindre le four")
        hi.response_builder.speak.assert_called_once_with("Le four est éteint")
        hi.response_builder.set_should_end_session.assert_called_with(True)

    def test_date_without_raw_text_logs_trace_and_no_match(self, lambda_mod):
        hi = _make_handler_input(
            intent_name="Date",
            slots={},
            locale="fr-FR",
            request_attrs={"_": {"NO_MATCH": "Aucune commande."}},
        )

        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = MockJee.return_value
            handler = lambda_mod.DateTimeIntentHandler()
            handler.handle(hi)

        inst.post_voice_command.assert_not_called()
        inst.post_jee_log.assert_called_once()
        hi.response_builder.speak.assert_called_once_with("Aucune commande.")
        hi.response_builder.set_should_end_session.assert_called_with(True)


# ────────────────────────────────────────────────────────────────────────────
# DisambiguationIntentHandler — sélection 1-N
# ────────────────────────────────────────────────────────────────────────────

class TestDisambiguationHandler:
    def test_choice_valid_replays_with_force_exact(self, lambda_mod):
        hi = _make_handler_input(
            intent_name="DisambiguationIntent",
            slots={"Choice": "2"},
            session_attrs={"pending_voice_options": ["phrase A", "phrase B", "phrase C"]},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = MockJee.return_value
            inst.post_voice_command.return_value = {
                "reply": "B exécuté", "matched": True, "ambiguous": False, "options": [],
            }
            handler = lambda_mod.DisambiguationIntentHandler()
            handler.handle(hi)
            # post_voice_command appelé avec phrase B + force_exact=True
            inst.post_voice_command.assert_called_once_with("phrase B", force_exact=True)
        # Session nettoyée
        assert "pending_voice_options" not in hi.attributes_manager.session_attributes

    def test_choice_out_of_range_re_prompts(self, lambda_mod):
        hi = _make_handler_input(
            intent_name="DisambiguationIntent",
            slots={"Choice": "5"},
            session_attrs={"pending_voice_options": ["a", "b"]},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            handler = lambda_mod.DisambiguationIntentHandler()
            handler.handle(hi)
        # Pas d'appel post_voice_command
        MockJee.return_value.post_voice_command.assert_not_called()
        # Session reste ouverte (re-prompt)
        hi.response_builder.set_should_end_session.assert_called_with(False)
        # Options préservées (l'user va re-choisir)
        assert hi.attributes_manager.session_attributes.get("pending_voice_options") == ["a", "b"]

    def test_no_pending_options_ends_gracefully(self, lambda_mod):
        hi = _make_handler_input(
            intent_name="DisambiguationIntent",
            slots={"Choice": "1"},
            session_attrs={},  # pas d'options en attente
            request_attrs={"_": {"NO_MATCH": "Aucune commande."}},
        )
        handler = lambda_mod.DisambiguationIntentHandler()
        handler.handle(hi)
        hi.response_builder.speak.assert_called_with("Aucune commande.")
        hi.response_builder.set_should_end_session.assert_called_with(True)

    def test_invalid_choice_value_re_prompts(self, lambda_mod):
        hi = _make_handler_input(
            intent_name="DisambiguationIntent",
            slots={"Choice": "blabla"},
            session_attrs={"pending_voice_options": ["a", "b"]},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            handler = lambda_mod.DisambiguationIntentHandler()
            handler.handle(hi)
            MockJee.return_value.post_voice_command.assert_not_called()
        hi.response_builder.set_should_end_session.assert_called_with(False)


# ────────────────────────────────────────────────────────────────────────────
# post_voice_command — flag http_error
# ────────────────────────────────────────────────────────────────────────────

class TestPostVoiceCommandHttpError:
    def _build_jee(self, lambda_mod):
        hi = _make_handler_input()
        jee = lambda_mod.JeeAsk(hi, fetch_question=False)
        jee.language_strings = {"ERROR_CONFIG": "Erreur config.", "NO_MATCH": "Aucune commande."}
        return jee

    def test_http_error_flag_true_when_request_fails(self, lambda_mod):
        jee = self._build_jee(lambda_mod)
        jee._request = MagicMock(return_value=None)
        result = jee.post_voice_command("allumer le salon")
        assert result["http_error"] is True
        assert result["matched"] is False

    def test_http_error_flag_false_on_success(self, lambda_mod):
        jee = self._build_jee(lambda_mod)
        jee._request = MagicMock(return_value=_fake_response(
            {"reply": "Salon allumé", "matched": True}
        ))
        result = jee.post_voice_command("allumer le salon")
        assert result["http_error"] is False
        assert result["matched"] is True

    def test_http_error_flag_false_when_no_match(self, lambda_mod):
        """Jeedom répond 200 mais matched=False → http_error=False (pas une erreur technique)."""
        jee = self._build_jee(lambda_mod)
        jee._request = MagicMock(return_value=_fake_response(
            {"reply": "", "matched": False, "ambiguous": False}
        ))
        result = jee.post_voice_command("baisser les volets")
        assert result["http_error"] is False
        assert result["matched"] is False
        assert result["reply"] == ""

    def test_http_error_flag_true_on_invalid_json(self, lambda_mod):
        jee = self._build_jee(lambda_mod)
        bad_rsp = MagicMock()
        bad_rsp.data = b"not json"
        bad_rsp.status = 200
        jee._request = MagicMock(return_value=bad_rsp)
        result = jee.post_voice_command("test")
        assert result["http_error"] is True


# ────────────────────────────────────────────────────────────────────────────
# _handle_voice_intent — branches http_error et no_match
# ────────────────────────────────────────────────────────────────────────────

class TestHandleVoiceIntentErrorBranches:
    def test_http_error_speaks_error_config(self, lambda_mod):
        hi = _make_handler_input(
            slots={"Command": "les volets"},
            locale="fr-FR",
            request_attrs={"_": {"ERROR_CONFIG": "Jeedom injoignable.", "NO_MATCH": "Aucune commande."}},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "Jeedom injoignable.", "matched": False,
                "ambiguous": False, "options": [], "http_error": True,
            }
            lambda_mod._handle_voice_intent(hi, "VoiceSet")
        hi.response_builder.speak.assert_called_once_with("Jeedom injoignable.")

    def test_no_match_speaks_no_match_message(self, lambda_mod):
        hi = _make_handler_input(
            slots={"Command": "les volets"},
            locale="fr-FR",
            request_attrs={"_": {"NO_MATCH": "Aucune commande trouvée.", "ERROR_CONFIG": "Erreur."}},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "", "matched": False,
                "ambiguous": False, "options": [], "http_error": False,
            }
            lambda_mod._handle_voice_intent(hi, "VoiceSet")
        hi.response_builder.speak.assert_called_once_with("Aucune commande trouvée.")

    def test_no_match_ends_session(self, lambda_mod):
        hi = _make_handler_input(
            slots={"Command": "les volets"},
            locale="fr-FR",
            request_attrs={"_": {"NO_MATCH": "Aucune commande trouvée."}},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "", "matched": False,
                "ambiguous": False, "options": [], "http_error": False,
            }
            lambda_mod._handle_voice_intent(hi, "VoiceSet")
        hi.response_builder.set_should_end_session.assert_called_with(True)

    def test_http_error_does_not_speak_no_match(self, lambda_mod):
        """http_error=True → ERROR_CONFIG, pas NO_MATCH."""
        hi = _make_handler_input(
            slots={"Command": "les volets"},
            locale="fr-FR",
            request_attrs={"_": {
                "ERROR_CONFIG": "Erreur config.",
                "NO_MATCH": "Aucune commande.",
            }},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "Erreur config.", "matched": False,
                "ambiguous": False, "options": [], "http_error": True,
            }
            lambda_mod._handle_voice_intent(hi, "VoiceSet")
        spoken = hi.response_builder.speak.call_args[0][0]
        assert spoken != "Aucune commande."
