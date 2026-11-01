"""
Intents de réponse avec et sans question Jeedom en attente.

Sans question (alexaAsk.json vide) : aucun envoi à Jeedom, Alexa dit
NO_QUESTION et ferme la session (les commandes libres relèvent du skill Frida).
Avec question : la réponse part avec le bon type (yes/no/numeric/duration/
datetime/select/string).
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

LAMBDA_DIR = Path(__file__).parent.parent
if str(LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(LAMBDA_DIR))

STRINGS = {"NO_QUESTION": "Aucune question en attente.", "NO_MATCH": "Pas compris.",
           "SELECTED": "Vous avez choisi {}", "ERROR_CONFIG": "Erreur config.",
           "HINT_TEXT": "dire arrête"}


@pytest.fixture
def lambda_mod():
    import lambda_function
    return lambda_function


def _make_handler_input(intent_name, slots=None):
    hi = MagicMock()
    hi._intent_name = intent_name
    hi._slots = slots or {}
    hi.request_envelope.request.locale = "fr-FR"
    hi.attributes_manager.session_attributes = {}
    hi.attributes_manager.request_attributes = {"_": dict(STRINGS)}
    hi.response_builder = MagicMock()
    for m in ("speak", "ask", "set_should_end_session", "add_directive"):
        getattr(hi.response_builder, m).return_value = hi.response_builder
    hi.response_builder.response = MagicMock(name="response")
    return hi


def _question(lambda_mod):
    return lambda_mod.QuestionState(text="Quelle pièce ?", event_id="e1", suppress_confirmation=False,
                                    deviceSerialNumber="SN", textBrut="")


def _mock_jee(MockJee, jee_state):
    inst = MockJee.return_value
    inst.jee_state = jee_state
    inst.get_value_for_slot.return_value = None
    inst.post_jee_event.return_value = "Ok"
    return inst


HANDLERS = [
    ("AMAZON.YesIntent", "YesIntentHandler", {}),
    ("AMAZON.NoIntent", "NoIntentHandler", {}),
    ("Number", "NumericIntentHandler", {"Numbers": "12"}),
    ("Duration", "DurationIntentHandler", {"Durations": "PT10M"}),
    ("Date", "DateTimeIntentHandler", {"Dates": "2026-11-01", "Times": "12:00"}),
    ("Select", "SelectIntentHandler", {"Selections": "salon"}),
    ("String", "StringIntentHandler", {"Strings": "rouge"}),
    ("AMAZON.RepeatIntent", "RepeatIntentHandler", {}),
]


class TestWithoutQuestion:

    @pytest.mark.parametrize("intent, handler, slots", HANDLERS)
    def test_says_no_question_without_posting(self, lambda_mod, intent, handler, slots):
        hi = _make_handler_input(intent, slots)
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, None)
            getattr(lambda_mod, handler)().handle(hi)
        inst.post_jee_event.assert_not_called()
        hi.response_builder.speak.assert_called_once_with("Aucune question en attente.")
        hi.response_builder.set_should_end_session.assert_called_once_with(True)

    def test_jeedom_error_is_spoken(self, lambda_mod):
        hi = _make_handler_input("String", {"Strings": "rouge"})
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            _mock_jee(MockJee, lambda_mod.QuestionStateError(text="Error 401"))
            lambda_mod.StringIntentHandler().handle(hi)
        hi.response_builder.speak.assert_called_once_with("Error 401")


class TestWithQuestion:

    @pytest.mark.parametrize("intent, handler, slots, value, rtype", [
        ("AMAZON.YesIntent", "YesIntentHandler", {}, "ResponseYes", "ResponseYes"),
        ("AMAZON.NoIntent", "NoIntentHandler", {}, "ResponseNo", "ResponseNo"),
        ("Number", "NumericIntentHandler", {"Numbers": "12"}, "12", "ResponseNumeric"),
        ("Number", "NumericIntentHandler", {"Ordinal": "2"}, "2", "ResponseNumeric"),
        ("Duration", "DurationIntentHandler", {"Durations": "PT10M"}, 600.0, "ResponseDuration"),
        ("Select", "SelectIntentHandler", {"Selections": "salon"}, "salon", "ResponseSelect"),
        ("String", "StringIntentHandler", {"Strings": "rouge"}, "rouge", "ResponseString"),
    ])
    def test_posts_typed_answer(self, lambda_mod, intent, handler, slots, value, rtype):
        hi = _make_handler_input(intent, slots)
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, _question(lambda_mod))
            getattr(lambda_mod, handler)().handle(hi)
        inst.post_jee_event.assert_called_once_with(value, rtype)

    def test_date_posts_split_datetime(self, lambda_mod):
        hi = _make_handler_input("Date", {"Dates": "2026-11-01", "Times": "12:30"})
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, _question(lambda_mod))
            lambda_mod.DateTimeIntentHandler().handle(hi)
        payload, rtype = inst.post_jee_event.call_args[0]
        assert rtype == "ResponseDateTime"
        assert json.loads(payload) == {"day": "01", "month": "11", "year": "2026",
                                       "hour": "12", "minute": "30", "seconds": None}

    @pytest.mark.parametrize("intent, handler, slots", [
        ("Number", "NumericIntentHandler", {"Numbers": "?"}),
        ("Duration", "DurationIntentHandler", {}),
        ("Date", "DateTimeIntentHandler", {}),
        ("Select", "SelectIntentHandler", {}),
        ("String", "StringIntentHandler", {}),
    ])
    def test_empty_slot_posts_none(self, lambda_mod, intent, handler, slots):
        hi = _make_handler_input(intent, slots)
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, _question(lambda_mod))
            getattr(lambda_mod, handler)().handle(hi)
        inst.post_jee_event.assert_called_once_with("ResponseNone", "ResponseNone")
        assert hi.response_builder.speak.called


class TestSessionEnded:
    """Session fermée sans réponse : le scénario doit recevoir « pas de réponse »."""

    def _ended(self, lambda_mod, reason):
        hi = _make_handler_input(None)
        hi.request_envelope.request.reason = reason
        return hi

    def test_posts_none_when_question_pending(self, lambda_mod):
        hi = self._ended(lambda_mod, lambda_mod.SessionEndedReason.EXCEEDED_MAX_REPROMPTS)
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, _question(lambda_mod))
            lambda_mod.SessionEndedRequestHandler().handle(hi)
        MockJee.assert_called_once_with(hi)  # question rechargée
        inst.post_jee_event.assert_called_once_with(lambda_mod.RESPONSE_NONE, lambda_mod.RESPONSE_NONE)

    def test_nothing_posted_without_question(self, lambda_mod):
        hi = self._ended(lambda_mod, lambda_mod.SessionEndedReason.USER_INITIATED)
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, None)
            lambda_mod.SessionEndedRequestHandler().handle(hi)
        inst.post_jee_event.assert_not_called()

    def test_error_reason_does_not_post(self, lambda_mod):
        hi = self._ended(lambda_mod, lambda_mod.SessionEndedReason.ERROR)
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, _question(lambda_mod))
            lambda_mod.SessionEndedRequestHandler().handle(hi)
        inst.post_jee_event.assert_not_called()
