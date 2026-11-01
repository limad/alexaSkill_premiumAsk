"""
Tests du dialog multi-tour (item 3.2).

Couvre :
- post_jee_event lit `next_question` du body de réponse askResponse.php
- Si next_question présent → self.jee_state devient un QuestionState frais
- _handle_qa_response : si jee_state est QuestionState → session ouverte +
  speak la nouvelle question
- Q/A handlers (Yes/No/String/etc) chaînent correctement

Mocks ask_sdk_core via conftest.py.
"""

import json
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


def _fake_response(body_dict):
    rsp = MagicMock()
    rsp.data = json.dumps(body_dict).encode("utf-8")
    rsp.status = 200
    return rsp


def _make_handler_input(intent_name="AMAZON.YesIntent", slots=None):
    hi = MagicMock()
    hi._intent_name = intent_name
    hi._slots = slots or {}
    hi.request_envelope.request.locale = "fr-FR"
    hi.attributes_manager.session_attributes = {}
    hi.attributes_manager.request_attributes = {"_": {"OKAY": "Ok", "ERROR_CONFIG": "Erreur"}}
    hi.request_envelope.context.system.device.device_id = "amzn1.test.device"
    hi.request_envelope.context.system.person = None
    hi.response_builder = MagicMock()
    hi.response_builder.speak.return_value = hi.response_builder
    hi.response_builder.ask.return_value = hi.response_builder
    hi.response_builder.set_should_end_session.return_value = hi.response_builder
    hi.response_builder.response = MagicMock(name="response")
    return hi


def _build_jee_with_state(lambda_mod, response_payload, initial_question=True):
    """Construit un JeeAsk avec un QuestionState courant + _request mocké."""
    hi = _make_handler_input()
    jee = lambda_mod.JeeAsk(hi, fetch_question=False)

    # État initial : Jeedom a posé une question
    if initial_question:
        jee.jee_state = lambda_mod.QuestionState(
            event_id="evt-1",
            suppress_confirmation=False,
            text="Quelle valeur ?",
            deviceSerialNumber="dev-sn",
            textBrut="quelle valeur",
        )

    jee._request = MagicMock(return_value=_fake_response(response_payload))
    jee.language_strings = {"OKAY": "Ok", "ERROR_CONFIG": "Erreur"}
    return jee, hi


# ────────────────────────────────────────────────────────────────────────────
# post_jee_event — extraction de next_question
# ────────────────────────────────────────────────────────────────────────────

class TestPostJeeEventNextQuestion:
    def test_no_next_question_clears_state(self, lambda_mod):
        """Réponse standard sans next_question → jee_state remis à None."""
        jee, _ = _build_jee_with_state(lambda_mod, {"ok": True})
        result = jee.post_jee_event("oui", "ResponseYes")
        assert jee.jee_state is None
        assert result == "Ok"

    def test_next_question_creates_new_state(self, lambda_mod):
        """next_question dans body → nouveau QuestionState avec event_id différent."""
        next_q = {
            "event": "evt-2",
            "text": "À quelle heure ?",
            "deviceSerialNumber": "dev-sn",
            "textBrut": "à quelle heure",
            "suppress_confirmation": False,
        }
        jee, _ = _build_jee_with_state(lambda_mod, {"ok": True, "next_question": next_q})
        result = jee.post_jee_event("oui", "ResponseYes")
        assert isinstance(jee.jee_state, lambda_mod.QuestionState)
        assert jee.jee_state.event_id == "evt-2"
        assert jee.jee_state.text == "À quelle heure ?"
        # Le retour est la nouvelle question (pour que le handler la speak)
        assert result == "À quelle heure ?"

    def test_next_question_without_text_ignored(self, lambda_mod):
        """next_question sans text → ignoré, flow standard."""
        jee, _ = _build_jee_with_state(lambda_mod, {
            "ok": True, "next_question": {"event": "evt-2"}  # text manquant
        })
        result = jee.post_jee_event("oui", "ResponseYes")
        assert jee.jee_state is None  # remis à None par flow standard
        assert result == "Ok"

    def test_next_question_invalid_type_ignored(self, lambda_mod):
        """next_question pas un dict → ignoré."""
        jee, _ = _build_jee_with_state(lambda_mod, {
            "ok": True, "next_question": "broken"
        })
        result = jee.post_jee_event("oui", "ResponseYes")
        assert jee.jee_state is None
        assert result == "Ok"


# ────────────────────────────────────────────────────────────────────────────
# _handle_qa_response — décision session ouverte/fermée
# ────────────────────────────────────────────────────────────────────────────

class TestHandleQaResponse:
    def test_question_state_keeps_session_open(self, lambda_mod):
        hi = _make_handler_input()
        jee = MagicMock()
        jee.jee_state = lambda_mod.QuestionState(
            event_id="evt-2",
            suppress_confirmation=False,
            text="Question suivante ?",
            deviceSerialNumber="x",
            textBrut="question",
        )
        lambda_mod._handle_qa_response(hi, jee, "Ok")
        # Doit avoir parlé la nouvelle question
        hi.response_builder.speak.assert_called_with("Question suivante ?")
        hi.response_builder.set_should_end_session.assert_called_with(False)

    def test_no_state_falls_back_to_handle_response(self, lambda_mod):
        hi = _make_handler_input()
        jee = MagicMock()
        jee.jee_state = None
        lambda_mod._handle_qa_response(hi, jee, "Ok")
        # Speak normal, pas de set_should_end_session(False)
        hi.response_builder.speak.assert_called_with("Ok")
        # set_should_end_session ne doit PAS avoir été appelé avec False
        # (helper _handle_response ne set pas explicitement la fin de session)
        for call in hi.response_builder.set_should_end_session.call_args_list:
            assert call.args != (False,)

    def test_question_state_error_falls_back(self, lambda_mod):
        """QuestionStateError ≠ QuestionState → flow standard."""
        hi = _make_handler_input()
        jee = MagicMock()
        jee.jee_state = lambda_mod.QuestionStateError(text="Erreur")
        lambda_mod._handle_qa_response(hi, jee, "Réponse Jeedom")
        hi.response_builder.speak.assert_called_with("Réponse Jeedom")


# ────────────────────────────────────────────────────────────────────────────
# Intégration : YesIntent enchaînant un multi-tour
# ────────────────────────────────────────────────────────────────────────────

class TestYesIntentMultiTurn:
    def test_yes_intent_chains_next_question(self, lambda_mod):
        """User dit oui → Jeedom dispatche → renvoie une nouvelle question."""
        hi = _make_handler_input(intent_name="AMAZON.YesIntent")

        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = MockJee.return_value
            # Simule l'état du multi-tour : post_jee_event a MAJ jee_state
            inst.jee_state = lambda_mod.QuestionState(
                event_id="evt-2",
                suppress_confirmation=False,
                text="Et la suite ?",
                deviceSerialNumber="x",
                textBrut="suite",
            )
            inst.post_jee_event.return_value = "Et la suite ?"

            handler = lambda_mod.YesIntentHandler()
            handler.handle(hi)

            inst.post_jee_event.assert_called_once()
            hi.response_builder.speak.assert_called_with("Et la suite ?")
            hi.response_builder.set_should_end_session.assert_called_with(False)
