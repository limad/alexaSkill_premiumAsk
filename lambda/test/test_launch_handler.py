"""
E.3 — Tests de LaunchRequestHandler.

Question en attente : jee.jee_state est un QuestionState avec text → Alexa
         prononce la question, session ouverte (ask), hint injecté.
Sans question : Alexa dit NO_QUESTION (ou l'erreur de lecture Jeedom) et
         ferme la session — ce skill ne prend pas de commande libre.

Mocks ask_sdk_core via conftest.py.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

LAMBDA_DIR = Path(__file__).parent.parent
if str(LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(LAMBDA_DIR))


@pytest.fixture
def lambda_mod():
    import lambda_function
    return lambda_function


def _make_handler_input(locale="fr-FR", request_attrs=None):
    hi = MagicMock()
    hi._request_type = "LaunchRequest"
    hi.request_envelope.request.locale = locale
    hi.attributes_manager.session_attributes = {}
    hi.attributes_manager.request_attributes = request_attrs or {
        "_": {
            "HINT_TEXT": "dire oui, non, ou arrête",
            "NO_QUESTION": "Aucune question en attente.",
            "ERROR_CONFIG": "Erreur config.",
        }
    }
    hi.response_builder = MagicMock()
    hi.response_builder.speak.return_value = hi.response_builder
    hi.response_builder.ask.return_value = hi.response_builder
    hi.response_builder.add_directive.return_value = hi.response_builder
    hi.response_builder.set_should_end_session.return_value = hi.response_builder
    hi.response_builder.response = MagicMock(name="response")
    return hi


class TestLaunchModeA:
    """Mode A : alexaAsk.json contient une question."""

    def test_speaks_question_text(self, lambda_mod):
        hi = _make_handler_input()
        question = lambda_mod.QuestionState(text="Voulez-vous allumer la lumière ?", event_id="ev1")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.jee_state = question
            handler = lambda_mod.LaunchRequestHandler()
            handler.handle(hi)
        hi.response_builder.speak.assert_called_once_with("Voulez-vous allumer la lumière ?")

    def test_calls_ask_when_event_id_present(self, lambda_mod):
        hi = _make_handler_input()
        question = lambda_mod.QuestionState(text="Oui ou non ?", event_id="ev42")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.jee_state = question
            lambda_mod.LaunchRequestHandler().handle(hi)
        hi.response_builder.ask.assert_called_once_with("")

    def test_no_ask_when_no_event_id(self, lambda_mod):
        """Sans event_id, Alexa ne doit pas garder la session ouverte avec ask."""
        hi = _make_handler_input()
        question = lambda_mod.QuestionState(text="Info statique", event_id=None)
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.jee_state = question
            lambda_mod.LaunchRequestHandler().handle(hi)
        hi.response_builder.ask.assert_not_called()

    def test_hint_injected_in_mode_a(self, lambda_mod):
        hi = _make_handler_input()
        question = lambda_mod.QuestionState(text="Allumer ?", event_id="ev1")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.jee_state = question
            lambda_mod.LaunchRequestHandler().handle(hi)
        hi.response_builder.add_directive.assert_called_once()

    def test_hint_text_from_data(self, lambda_mod):
        """Le hint doit utiliser la chaîne HINT_TEXT des request_attributes."""
        hi = _make_handler_input(request_attrs={"_": {
            "HINT_TEXT": "custom hint",
            "DIRECT_PROMPT": "Prompt ?",
        }})
        question = lambda_mod.QuestionState(text="Question ?", event_id="ev1")
        with patch.object(lambda_mod, "JeeAsk") as MockJee, \
             patch.object(lambda_mod, "_add_hint") as mock_hint:
            MockJee.return_value.jee_state = question
            lambda_mod.LaunchRequestHandler().handle(hi)
            mock_hint.assert_called_once()
            _, hint_arg = mock_hint.call_args[0]
            assert hint_arg == "custom hint"


class TestLaunchNoQuestion:
    """alexaAsk.json vide ou absent : rien à demander."""

    def test_says_no_question_and_ends_session(self, lambda_mod):
        hi = _make_handler_input()
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.jee_state = None
            lambda_mod.LaunchRequestHandler().handle(hi)
        hi.response_builder.speak.assert_called_once_with("Aucune question en attente.")
        hi.response_builder.set_should_end_session.assert_called_once_with(True)
        hi.response_builder.ask.assert_not_called()

    def test_jeedom_error_is_spoken(self, lambda_mod):
        hi = _make_handler_input()
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            MockJee.return_value.jee_state = lambda_mod.QuestionStateError(text="Jeedom injoignable")
            lambda_mod.LaunchRequestHandler().handle(hi)
        hi.response_builder.speak.assert_called_once_with("Jeedom injoignable")
        hi.response_builder.set_should_end_session.assert_called_once_with(True)
