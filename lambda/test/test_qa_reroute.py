"""
Tests du reroutage des handlers de réponse Q/A (Select / String) quand ils sont
déclenchés HORS contexte Q/A.

Bug d'origine : "Alexa, demande à jeedom la température du thermostat" était routé
par le NLU vers l'intent Select (sample générique "la {Selections}") alors qu'aucune
question Jeedom n'était en attente. Le skill répondait "Vous avez choisi température
du thermostat" au lieu de traiter la demande comme une commande/question vocale.

Fix : si jee.jee_state n'est PAS une QuestionState (= pas de question active),
Select/String redispatchent la phrase via _dispatch_voice_query (→ voiceRouter).
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


def _make_handler_input(intent_name="Select", slots=None, locale="fr-FR",
                        session_attrs=None, request_attrs=None):
    hi = MagicMock()
    hi._intent_name = intent_name
    hi._slots = slots or {}
    hi.request_envelope.request.locale = locale
    hi.attributes_manager.session_attributes = session_attrs if session_attrs is not None else {}
    hi.attributes_manager.request_attributes = request_attrs or {"_": {"NO_MATCH": "Aucune commande.",
                                                                       "SELECTED": "Vous avez choisi {}"}}
    hi.request_envelope.context.system.device.device_id = "amzn1.test.device"
    hi.request_envelope.context.system.person = None
    hi.response_builder = MagicMock()
    hi.response_builder.speak.return_value = hi.response_builder
    hi.response_builder.ask.return_value = hi.response_builder
    hi.response_builder.set_should_end_session.return_value = hi.response_builder
    hi.response_builder.response = MagicMock(name="response")
    return hi


def _mock_jee(MockJee, *, jee_state, selection=None, voice_result=None):
    """Configure le mock JeeAsk : jee_state + retours des helpers."""
    inst = MockJee.return_value
    inst.jee_state = jee_state
    inst.get_value_for_slot.return_value = selection
    inst.post_voice_command.return_value = voice_result or {
        "reply": "Il fait 21 degrés.", "matched": True, "ambiguous": False, "options": [],
    }
    inst.post_jee_event.return_value = "Vous avez choisi température du thermostat"
    return inst


# ────────────────────────────────────────────────────────────────────────────
# Select HORS contexte Q/A → redispatch vocal
# ────────────────────────────────────────────────────────────────────────────

class TestSelectRerouteWhenNoQuestion:

    def test_no_question_redispatches_as_voice(self, lambda_mod):
        hi = _make_handler_input(slots={"Selections": "température du thermostat"})
        err = lambda_mod.QuestionStateError(text="pas de question")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            _mock_jee(MockJee, jee_state=err, selection="température du thermostat")
            lambda_mod.SelectIntentHandler().handle(hi)
            inst = MockJee.return_value
            # La phrase est forwardée à voiceRouter (post_voice_command), PAS postée
            # comme réponse Q/A (post_jee_event).
            inst.post_voice_command.assert_called_once()
            assert "thermostat" in inst.post_voice_command.call_args[0][0]

    def test_no_question_does_not_post_jee_event(self, lambda_mod):
        hi = _make_handler_input(slots={"Selections": "température du thermostat"})
        err = lambda_mod.QuestionStateError(text="pas de question")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            _mock_jee(MockJee, jee_state=err, selection="température du thermostat")
            lambda_mod.SelectIntentHandler().handle(hi)
            MockJee.return_value.post_jee_event.assert_not_called()

    def test_no_question_speaks_voice_reply_not_selected(self, lambda_mod):
        hi = _make_handler_input(slots={"Selections": "température du thermostat"})
        err = lambda_mod.QuestionStateError(text="pas de question")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            _mock_jee(MockJee, jee_state=err, selection="température du thermostat",
                      voice_result={"reply": "Il fait 21 degrés.", "matched": True,
                                    "ambiguous": False, "options": []})
            lambda_mod.SelectIntentHandler().handle(hi)
            spoken = hi.response_builder.speak.call_args[0][0]
            assert spoken == "Il fait 21 degrés."
            assert "choisi" not in spoken

    def test_no_question_no_selection_says_no_match(self, lambda_mod):
        hi = _make_handler_input(slots={})
        err = lambda_mod.QuestionStateError(text="pas de question")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            _mock_jee(MockJee, jee_state=err, selection=None)
            # get_slot_value renvoie None aussi (slot absent)
            lambda_mod.SelectIntentHandler().handle(hi)
            MockJee.return_value.post_voice_command.assert_not_called()
            MockJee.return_value.post_jee_event.assert_not_called()


# ────────────────────────────────────────────────────────────────────────────
# Select AVEC question active → comportement Q/A normal préservé
# ────────────────────────────────────────────────────────────────────────────

class TestSelectNormalWhenQuestionActive:

    def test_active_question_posts_jee_event(self, lambda_mod):
        hi = _make_handler_input(slots={"Selections": "salon"})
        q = lambda_mod.QuestionState(text="Quelle pièce ?", event_id="e1",
                                     suppress_confirmation=False,
                                     deviceSerialNumber="SN", textBrut="")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, jee_state=q, selection="salon")
            # post_jee_event ne change pas jee_state (pas de multi-tour)
            lambda_mod.SelectIntentHandler().handle(hi)
            inst.post_jee_event.assert_called_once()
            inst.post_voice_command.assert_not_called()

    def test_active_question_speaks_selected_confirmation(self, lambda_mod):
        hi = _make_handler_input(slots={"Selections": "salon"})
        q = lambda_mod.QuestionState(text="Quelle pièce ?", event_id="e1",
                                     suppress_confirmation=False,
                                     deviceSerialNumber="SN", textBrut="")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, jee_state=q, selection="salon")
            lambda_mod.SelectIntentHandler().handle(hi)
            spoken = hi.response_builder.speak.call_args[0][0]
            assert "Vous avez choisi" in spoken


# ────────────────────────────────────────────────────────────────────────────
# String HORS contexte Q/A → redispatch vocal
# ────────────────────────────────────────────────────────────────────────────

class TestStringRerouteWhenNoQuestion:

    def test_no_question_redispatches_as_voice(self, lambda_mod):
        hi = _make_handler_input(intent_name="String", slots={"Strings": "allume le salon"})
        err = lambda_mod.QuestionStateError(text="pas de question")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, jee_state=err)
            inst.get_value_for_slot.return_value = None  # String n'utilise pas get_value_for_slot
            lambda_mod.StringIntentHandler().handle(hi)
            inst.post_voice_command.assert_called_once()
            assert "salon" in inst.post_voice_command.call_args[0][0]
            inst.post_jee_event.assert_not_called()

    def test_active_question_posts_jee_event(self, lambda_mod):
        hi = _make_handler_input(intent_name="String", slots={"Strings": "rouge"})
        q = lambda_mod.QuestionState(text="Quelle couleur ?", event_id="e1",
                                     suppress_confirmation=False,
                                     deviceSerialNumber="SN", textBrut="")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, jee_state=q)
            lambda_mod.StringIntentHandler().handle(hi)
            inst.post_jee_event.assert_called_once()
            inst.post_voice_command.assert_not_called()


# ────────────────────────────────────────────────────────────────────────────
# Guard anti-crash : Date / Number / Duration hors contexte Q/A
# (slot typé → pas de phrase brute à rerouter → NO_MATCH propre, jamais de crash)
# ────────────────────────────────────────────────────────────────────────────

class TestTypedQaGuardWhenNoQuestion:

    @pytest.mark.parametrize("intent, handler_attr, slots", [
        ("Date",     "DateTimeIntentHandler", {"Dates": None, "Times": None}),
        ("Number",   "NumericIntentHandler",  {"Numbers": None}),
        ("Duration", "DurationIntentHandler", {"Durations": None}),
    ])
    def test_no_question_says_no_match_without_posting(self, lambda_mod, intent, handler_attr, slots):
        hi = _make_handler_input(
            intent_name=intent, slots=slots,
            request_attrs={"_": {"NO_MATCH": "Je n'ai pas compris votre demande.",
                                 "ERROR_CONFIG": "Erreur config."}},
        )
        err = lambda_mod.QuestionStateError(text="pas de question")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, jee_state=err)
            inst.get_value_for_slot.return_value = None
            getattr(lambda_mod, handler_attr)().handle(hi)
            # Pas de post (ni event ni voice) — slot typé n'a pas la phrase brute.
            inst.post_jee_event.assert_not_called()
            # Réponse parlée non vide (pas de crash "réponse sans speech").
            assert hi.response_builder.speak.called
            spoken = hi.response_builder.speak.call_args[0][0]
            assert spoken == "Je n'ai pas compris votre demande."

    def test_date_with_active_question_still_posts(self, lambda_mod):
        hi = _make_handler_input(intent_name="Date", slots={"Dates": "2026-06-01", "Times": None})
        q = lambda_mod.QuestionState(text="Quelle date ?", event_id="e1",
                                     suppress_confirmation=False,
                                     deviceSerialNumber="SN", textBrut="")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, jee_state=q)
            inst.get_value_for_slot.return_value = None
            inst.post_jee_event.return_value = "Date enregistrée"
            lambda_mod.DateTimeIntentHandler().handle(hi)
            inst.post_jee_event.assert_called_once()
