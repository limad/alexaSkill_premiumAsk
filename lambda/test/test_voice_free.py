"""
Tests de l'intent fourre-tout VoiceFree (essai fr-FR : sample "{Query}" seul,
slot AMAZON.Person qui reçoit la phrase entière).
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


def _make_handler_input(query=None, session_new=True):
    hi = MagicMock()
    hi._intent_name = "VoiceFree"
    hi._slots = {"Query": query} if query is not None else {}
    hi.request_envelope.request.locale = "fr-FR"
    hi.request_envelope.session.new = session_new
    hi.attributes_manager.session_attributes = {}
    hi.attributes_manager.request_attributes = {"_": {"NO_MATCH": "Aucune commande."}}
    hi.response_builder = MagicMock()
    hi.response_builder.speak.return_value = hi.response_builder
    hi.response_builder.ask.return_value = hi.response_builder
    hi.response_builder.set_should_end_session.return_value = hi.response_builder
    hi.response_builder.response = MagicMock(name="response")
    return hi


def _mock_jee(MockJee, jee_state):
    inst = MockJee.return_value
    inst.jee_state = jee_state
    inst.post_voice_command.return_value = {
        "reply": "Lumière allumée.", "matched": True, "ambiguous": False, "options": [],
    }
    inst.post_jee_event.return_value = "Réponse enregistrée."
    return inst


def test_can_handle(lambda_mod):
    assert lambda_mod.VoiceFreeIntentHandler().can_handle(_make_handler_input("x"))


def test_new_session_dispatches_full_phrase_without_fetching_question(lambda_mod):
    hi = _make_handler_input("allume la lumière du salon", session_new=True)
    with patch.object(lambda_mod, "JeeAsk") as MockJee:
        inst = _mock_jee(MockJee, None)
        lambda_mod.VoiceFreeIntentHandler().handle(hi)
        # Phrase transmise telle quelle, sans verbe ajouté
        inst.post_voice_command.assert_called_once_with("allume la lumière du salon")
        # Aucune lecture de question : seul JeeAsk(fetch_question=False) du dispatch
        for call in MockJee.call_args_list:
            assert call.kwargs.get("fetch_question") is False
        inst.post_jee_event.assert_not_called()


def test_open_session_without_question_dispatches(lambda_mod):
    hi = _make_handler_input("quelle est la température du salon", session_new=False)
    with patch.object(lambda_mod, "JeeAsk") as MockJee:
        inst = _mock_jee(MockJee, lambda_mod.QuestionStateError(text="pas de question"))
        lambda_mod.VoiceFreeIntentHandler().handle(hi)
        inst.post_voice_command.assert_called_once_with("quelle est la température du salon")
        inst.post_jee_event.assert_not_called()


def test_open_session_with_question_posts_string_answer(lambda_mod):
    hi = _make_handler_input("la cuisine", session_new=False)
    q = lambda_mod.QuestionState(text="Quelle pièce ?", event_id="e1")
    with patch.object(lambda_mod, "JeeAsk") as MockJee:
        inst = _mock_jee(MockJee, q)
        lambda_mod.VoiceFreeIntentHandler().handle(hi)
        inst.post_jee_event.assert_called_once_with("la cuisine", lambda_mod.RESPONSE_STRING)
        inst.post_voice_command.assert_not_called()


def test_empty_query_says_no_match(lambda_mod):
    hi = _make_handler_input(None)
    with patch.object(lambda_mod, "JeeAsk") as MockJee:
        inst = _mock_jee(MockJee, None)
        lambda_mod.VoiceFreeIntentHandler().handle(hi)
        inst.post_voice_command.assert_not_called()
        hi.response_builder.speak.assert_called_once_with("Aucune commande.")


# ── Désambiguïsation sans « le {Choice} » : réponse via VoiceFree / Number ──

@pytest.mark.parametrize("text,expected", [
    ("le deuxième", 2), ("la première", 1), ("numéro 3", 3), ("deux", 2),
    ("allume la lumière", None), ("", None),
    ("the second one", 2), ("number 3", 3), ("el segundo", 2), ("la tercera", 3),
    ("turn on the light", None), ("enciende la luz", None),
])
def test_parse_choice(lambda_mod, text, expected):
    assert lambda_mod._parse_choice(text) == expected


def test_voice_free_with_pending_options_resolves_choice(lambda_mod):
    hi = _make_handler_input("le deuxième", session_new=False)
    hi.attributes_manager.session_attributes["pending_voice_options"] = ["allume le salon", "allume le salon TV"]
    with patch.object(lambda_mod, "JeeAsk") as MockJee:
        inst = _mock_jee(MockJee, None)
        lambda_mod.VoiceFreeIntentHandler().handle(hi)
        inst.post_voice_command.assert_called_once_with("allume le salon TV", force_exact=True)
        assert "pending_voice_options" not in hi.attributes_manager.session_attributes


def test_voice_free_with_pending_options_but_no_number_dispatches(lambda_mod):
    hi = _make_handler_input("allume la cuisine", session_new=True)
    hi.attributes_manager.session_attributes["pending_voice_options"] = ["a", "b"]
    with patch.object(lambda_mod, "JeeAsk") as MockJee:
        inst = _mock_jee(MockJee, None)
        lambda_mod.VoiceFreeIntentHandler().handle(hi)
        inst.post_voice_command.assert_called_once_with("allume la cuisine")


def test_number_with_pending_options_resolves_choice(lambda_mod):
    hi = _make_handler_input(None, session_new=False)
    hi._intent_name = "Number"
    hi._slots = {"Numbers": "1"}
    hi.attributes_manager.session_attributes["pending_voice_options"] = ["allume le salon", "allume le salon TV"]
    with patch.object(lambda_mod, "JeeAsk") as MockJee:
        inst = _mock_jee(MockJee, None)
        lambda_mod.NumericIntentHandler().handle(hi)
        inst.post_voice_command.assert_called_once_with("allume le salon", force_exact=True)
        inst.post_jee_event.assert_not_called()


def test_ordinal_with_pending_options_resolves_choice(lambda_mod):
    hi = _make_handler_input(None, session_new=False)
    hi._intent_name = "Number"
    hi._slots = {"Ordinal": "2"}
    hi.attributes_manager.session_attributes["pending_voice_options"] = ["allume le salon", "allume le salon TV"]
    with patch.object(lambda_mod, "JeeAsk") as MockJee:
        inst = _mock_jee(MockJee, None)
        lambda_mod.NumericIntentHandler().handle(hi)
        inst.post_voice_command.assert_called_once_with("allume le salon TV", force_exact=True)


# ── Relance HTTP : jamais sur POST (évite d'exécuter deux fois une commande) ──

def test_retry_never_replays_post(lambda_mod):
    assert lambda_mod._RETRY._is_method_retryable("GET")
    assert not lambda_mod._RETRY._is_method_retryable("POST")
    assert not lambda_mod._RETRY.is_retry("POST", 503)


# ── Délai de lecture : 6 s pour la requête vocale, 3 s ailleurs ──

def test_voice_command_uses_longer_read_timeout(lambda_mod):
    hi = _make_handler_input("x")
    jee = lambda_mod.JeeAsk(hi, fetch_question=False)
    jee._request = MagicMock(return_value=None)
    jee.language_strings = {}
    jee.post_voice_command("quelle heure est-il")
    assert jee._request.call_args.kwargs.get("timeout") is lambda_mod.VOICE_TIMEOUT
    assert lambda_mod.VOICE_TIMEOUT.read_timeout == 6.0


# ── Langue : la locale de l'Echo part avec la phrase ──

def test_voice_command_sends_locale(lambda_mod):
    hi = _make_handler_input("x")
    hi.request_envelope.request.locale = "en-US"
    jee = lambda_mod.JeeAsk(hi, fetch_question=False)
    jee._request = MagicMock(return_value=None)
    jee.language_strings = {}
    jee.post_voice_command("what time is it")
    assert jee._request.call_args.kwargs["body"]["locale"] == "en-US"


def test_invalid_choice_is_translated(lambda_mod):
    hi = _make_handler_input("le neuvième", session_new=False)
    hi.attributes_manager.request_attributes = {"_": {"INVALID_CHOICE": "Invalid choice."}}
    hi.attributes_manager.session_attributes["pending_voice_options"] = ["a", "b"]
    lambda_mod._resolve_voice_choice(hi, 9)
    hi.response_builder.speak.assert_called_once_with("Invalid choice.")


# ── Confirmation « Veux-tu dire : … ? » (action incertaine côté Jeedom) ──────

def test_yes_confirms_single_pending_option(lambda_mod):
    hi = _make_handler_input(None, session_new=False)
    hi._intent_name = "AMAZON.YesIntent"
    hi.attributes_manager.session_attributes["pending_voice_options"] = ["allume la lumière du bureau"]
    with patch.object(lambda_mod, "JeeAsk") as MockJee:
        inst = _mock_jee(MockJee, None)
        lambda_mod.YesIntentHandler().handle(hi)
        inst.post_voice_command.assert_called_once_with("allume la lumière du bureau", force_exact=True)
        inst.post_jee_event.assert_not_called()


def test_yes_with_several_options_asks_for_number(lambda_mod):
    hi = _make_handler_input(None, session_new=False)
    hi._intent_name = "AMAZON.YesIntent"
    hi.attributes_manager.session_attributes["pending_voice_options"] = ["a", "b"]
    with patch.object(lambda_mod, "JeeAsk") as MockJee:
        inst = _mock_jee(MockJee, None)
        lambda_mod.YesIntentHandler().handle(hi)
        inst.post_voice_command.assert_not_called()
        assert hi.attributes_manager.session_attributes["pending_voice_options"] == ["a", "b"]


def test_no_cancels_pending_option(lambda_mod):
    hi = _make_handler_input(None, session_new=False)
    hi._intent_name = "AMAZON.NoIntent"
    hi.attributes_manager.session_attributes["pending_voice_options"] = ["allume la lumière du bureau"]
    with patch.object(lambda_mod, "JeeAsk") as MockJee:
        inst = _mock_jee(MockJee, None)
        lambda_mod.NoIntentHandler().handle(hi)
        inst.post_voice_command.assert_not_called()
        inst.post_jee_event.assert_not_called()
        assert "pending_voice_options" not in hi.attributes_manager.session_attributes
