"""
Tests du VoiceQueryIntentHandler — chemin LLM libre (slot Query, sans préfixe verbe).

Différences clés vs les autres Voice* handlers :
  - Slot "Query" (AMAZON.SearchQuery, open-ended) au lieu de "Command"
  - Pas de verbe préfixé : VOICE_VERBS["*"]["VoiceQuery"] == "" → query envoyée telle quelle
  - Identique pour le reste (matched / no_match / ambiguous / http_error)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

LAMBDA_DIR = Path(__file__).parent.parent
if str(LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(LAMBDA_DIR))


@pytest.fixture
def lambda_mod():
    import lambda_function
    return lambda_function


def _make_handler_input(slots=None, locale="fr-FR",
                        session_attrs=None, request_attrs=None):
    """Mock handler_input compatible avec _compat_slot_utils (conftest)."""
    hi = MagicMock()
    hi._intent_name = "VoiceQuery"
    hi._slots = slots or {}
    hi.request_envelope.request.locale = locale

    hi.attributes_manager.session_attributes = session_attrs if session_attrs is not None else {}
    hi.attributes_manager.request_attributes = request_attrs or {"_": {}}

    hi.request_envelope.context.system.device.device_id = "amzn1.test.device"
    hi.request_envelope.context.system.person = None

    hi.response_builder = MagicMock()
    hi.response_builder.speak.return_value = hi.response_builder
    hi.response_builder.ask.return_value = hi.response_builder
    hi.response_builder.set_should_end_session.return_value = hi.response_builder
    hi.response_builder.response = MagicMock(name="response")
    return hi


# ────────────────────────────────────────────────────────────────────────────
# Slot utilisé : "Query", pas "Command"
# ────────────────────────────────────────────────────────────────────────────

class TestVoiceQueryUsesQuerySlot:

    def test_query_slot_is_read(self, lambda_mod):
        """Le slot "Query" doit être lu, pas "Command"."""
        hi = _make_handler_input(slots={"Query": "quelle température dans le salon"})
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "Il fait 21°C.", "matched": True,
                "ambiguous": False, "options": [],
            }
            lambda_mod._handle_voice_intent(hi, "VoiceQuery")
        called_query = MockJee.return_value.post_voice_command.call_args[0][0]
        assert "température" in called_query

    def test_command_slot_not_used(self, lambda_mod):
        """Si "Query" est rempli et "Command" aussi, c'est "Query" qui gagne."""
        hi = _make_handler_input(slots={
            "Query":   "quelle température dans le salon",
            "Command": "ne doit pas être utilisé",
        })
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "Il fait 21°C.", "matched": True,
                "ambiguous": False, "options": [],
            }
            lambda_mod._handle_voice_intent(hi, "VoiceQuery")
        called_query = MockJee.return_value.post_voice_command.call_args[0][0]
        assert "ne doit pas être utilisé" not in called_query


# ────────────────────────────────────────────────────────────────────────────
# Pas de préfixe verbe (toutes les locales)
# ────────────────────────────────────────────────────────────────────────────

class TestVoiceQueryNoVerbPrefix:

    @pytest.mark.parametrize("locale, query", [
        ("fr-FR", "quel temps fait-il"),
        ("en-US", "what is the temperature"),
        ("es-ES", "qué temperatura hay"),
        ("de-DE", "wie warm ist es"),
        ("it-IT", "che temperatura c'è"),
        ("pt-BR", "que temperatura faz"),
    ])
    def test_no_verb_prefix_for_locale(self, lambda_mod, locale, query):
        """VoiceQuery envoie la query brute sans aucun verbe préfixé."""
        hi = _make_handler_input(slots={"Query": query}, locale=locale)
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "Réponse.", "matched": True,
                "ambiguous": False, "options": [],
            }
            lambda_mod._handle_voice_intent(hi, "VoiceQuery")
        called_query = MockJee.return_value.post_voice_command.call_args[0][0]
        # La query transmise doit être égale à l'input (strips mis à part)
        assert called_query.strip() == query.strip()

    def test_no_prefix_french_imperative_not_added(self, lambda_mod):
        """Contrairement à VoiceTurnOn, VoiceQuery n'ajoute pas "allumer" devant."""
        hi = _make_handler_input(
            slots={"Query": "le salon"},
            locale="fr-FR",
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "Réponse.", "matched": True,
                "ambiguous": False, "options": [],
            }
            lambda_mod._handle_voice_intent(hi, "VoiceQuery")
        called_query = MockJee.return_value.post_voice_command.call_args[0][0]
        assert not called_query.startswith("allumer")
        assert not called_query.startswith("éteindre")


# ────────────────────────────────────────────────────────────────────────────
# Comportements nominaux (identiques aux autres Voice*)
# ────────────────────────────────────────────────────────────────────────────

class TestVoiceQueryResponseBranches:

    def test_matched_speaks_reply_and_ends_session(self, lambda_mod):
        hi = _make_handler_input(slots={"Query": "température salon"}, locale="fr-FR")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "Il fait 21°C.", "matched": True,
                "ambiguous": False, "options": [],
            }
            lambda_mod._handle_voice_intent(hi, "VoiceQuery")
        hi.response_builder.speak.assert_called_once_with("Il fait 21°C.")
        hi.response_builder.set_should_end_session.assert_called_with(True)

    def test_no_match_speaks_no_match_message(self, lambda_mod):
        hi = _make_handler_input(
            slots={"Query": "phrase inconnue"},
            locale="fr-FR",
            request_attrs={"_": {"NO_MATCH": "Aucune commande trouvée."}},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "", "matched": False,
                "ambiguous": False, "options": [], "http_error": False,
            }
            lambda_mod._handle_voice_intent(hi, "VoiceQuery")
        hi.response_builder.speak.assert_called_once_with("Aucune commande trouvée.")

    def test_no_match_ends_session(self, lambda_mod):
        hi = _make_handler_input(
            slots={"Query": "phrase inconnue"},
            request_attrs={"_": {"NO_MATCH": "Aucune commande."}},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "", "matched": False,
                "ambiguous": False, "options": [], "http_error": False,
            }
            lambda_mod._handle_voice_intent(hi, "VoiceQuery")
        hi.response_builder.set_should_end_session.assert_called_with(True)

    def test_http_error_speaks_error_config(self, lambda_mod):
        hi = _make_handler_input(
            slots={"Query": "température"},
            request_attrs={"_": {"ERROR_CONFIG": "Jeedom injoignable.", "NO_MATCH": "Aucune cmd."}},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "Jeedom injoignable.", "matched": False,
                "ambiguous": False, "options": [], "http_error": True,
            }
            lambda_mod._handle_voice_intent(hi, "VoiceQuery")
        spoken = hi.response_builder.speak.call_args[0][0]
        assert spoken == "Jeedom injoignable."

    def test_http_error_does_not_speak_no_match(self, lambda_mod):
        """http_error=True → ERROR_CONFIG, pas NO_MATCH."""
        hi = _make_handler_input(
            slots={"Query": "température"},
            request_attrs={"_": {"ERROR_CONFIG": "Erreur.", "NO_MATCH": "Aucune."}},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "Erreur.", "matched": False,
                "ambiguous": False, "options": [], "http_error": True,
            }
            lambda_mod._handle_voice_intent(hi, "VoiceQuery")
        spoken = hi.response_builder.speak.call_args[0][0]
        assert spoken != "Aucune."

    def test_ambiguous_stores_options_in_session(self, lambda_mod):
        sess = {}
        hi = _make_handler_input(
            slots={"Query": "la lampe"},
            session_attrs=sess,
            request_attrs={"_": {"HINT_TEXT": "dire arrête"}},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "1 : lampe salon, 2 : lampe bureau. Laquelle ?",
                "matched": False, "ambiguous": True,
                "options": ["lampe salon", "lampe bureau"],
            }
            lambda_mod._handle_voice_intent(hi, "VoiceQuery")
        assert sess.get("pending_voice_options") == ["lampe salon", "lampe bureau"]

    def test_ambiguous_keeps_session_open(self, lambda_mod):
        hi = _make_handler_input(
            slots={"Query": "la lampe"},
            request_attrs={"_": {"HINT_TEXT": "dire arrête"}},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.post_voice_command.return_value = {
                "reply": "Laquelle ?", "matched": False, "ambiguous": True,
                "options": ["lampe salon", "lampe bureau"],
            }
            lambda_mod._handle_voice_intent(hi, "VoiceQuery")
        hi.response_builder.set_should_end_session.assert_called_with(False)


# ────────────────────────────────────────────────────────────────────────────
# Query vide
# ────────────────────────────────────────────────────────────────────────────

class TestVoiceQueryEmptySlot:

    def test_empty_query_speaks_no_match_fallback(self, lambda_mod):
        """Slot Query absent/vide → message NO_MATCH immédiat, sans appel HTTP."""
        hi = _make_handler_input(
            slots={"Query": ""},
            request_attrs={"_": {"NO_MATCH": "Je n'ai pas compris votre commande."}},
        )
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            lambda_mod._handle_voice_intent(hi, "VoiceQuery")
        MockJee.return_value.post_voice_command.assert_not_called()

    def test_none_query_does_not_call_jeedom(self, lambda_mod):
        """Slot Query None → pas d'appel HTTP vers Jeedom."""
        hi = _make_handler_input(slots={})
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            lambda_mod._handle_voice_intent(hi, "VoiceQuery")
        MockJee.return_value.post_voice_command.assert_not_called()


# ────────────────────────────────────────────────────────────────────────────
# VoiceQueryIntentHandler.can_handle
# ────────────────────────────────────────────────────────────────────────────

class TestVoiceQueryHandlerCanHandle:

    def test_can_handle_voice_query_intent(self, lambda_mod):
        hi = _make_handler_input()
        hi._intent_name = "VoiceQuery"
        handler = lambda_mod.VoiceQueryIntentHandler()
        assert handler.can_handle(hi)

    def test_cannot_handle_other_intents(self, lambda_mod):
        for intent in ["VoiceTurnOn", "VoiceTurnOff", "VoiceSet", "VoiceLaunch", "AMAZON.HelpIntent"]:
            hi = _make_handler_input()
            hi._intent_name = intent
            handler = lambda_mod.VoiceQueryIntentHandler()
            assert not handler.can_handle(hi), f"ne devrait pas matcher {intent}"
