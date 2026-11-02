"""
Correctifs de la revue du 02/11/2026 : texte SSML échappé, heure « le matin »,
choix non confirmé si l'envoi échoue, réponse incomprise reposée, stop/accueil
pendant une question, budget de 8 s.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from .test_no_question import _make_handler_input, _mock_jee, _question


@pytest.fixture
def lambda_mod():
    import lambda_function
    return lambda_function


def _jee(lambda_mod):
    """JeeAsk réel, sans appel réseau à la création."""
    hi = _make_handler_input("AMAZON.YesIntent")
    with patch.object(lambda_mod.JeeAsk, "get_jeeQuestion"):
        return lambda_mod.JeeAsk(hi, fetch_question=False)


def _http_response(status=200, data=None):
    rsp = MagicMock()
    rsp.status = status
    rsp.data = json.dumps(data or {}).encode("utf-8")
    return rsp


class TestSsmlEscape:
    def test_question_text_escaped(self, lambda_mod):
        jee = _jee(lambda_mod)
        state = json.dumps({"text": "Volets & lumières < 5 ?", "event": "e1"})
        with patch.object(jee, "_request", return_value=_http_response(data={"state": state})):
            jee.get_jeeQuestion()
        assert jee.jee_state.text == "Volets &amp; lumières &lt; 5 ?"

    def test_next_question_escaped(self, lambda_mod):
        jee = _jee(lambda_mod)
        jee.jee_state = _question(lambda_mod)
        nxt = {"next_question": {"text": "Salon & cuisine ?", "event": "e2"}}
        with patch.object(jee, "_request", return_value=_http_response(data=nxt)):
            spoken = jee.post_jee_event("ResponseYes", "ResponseYes")
        assert spoken == jee.jee_state.text == "Salon &amp; cuisine ?"

    def test_selection_escaped_in_confirmation(self, lambda_mod):
        hi = _make_handler_input("Select", {"Selections": "salon & cuisine"})
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, _question(lambda_mod))
            inst.post_jee_event.side_effect = lambda *a: setattr(inst, "jee_state", None) or "Ok"
            lambda_mod.SelectIntentHandler().handle(hi)
        hi.response_builder.speak.assert_called_once_with("Vous avez choisi salon &amp; cuisine")


class TestTimePeriods:
    @pytest.mark.parametrize("value", ["MO", "AF", "EV", "NI"])
    def test_period_sent_without_fake_hour(self, lambda_mod, value):
        out = lambda_mod.DateTimeIntentHandler._parse_time(value)
        assert out == {"seconds": None, "minute": None, "hour": None, "period": value}

    def test_clock_time_unchanged(self, lambda_mod):
        assert lambda_mod.DateTimeIntentHandler._parse_time("07:45") == {
            "seconds": None, "minute": "45", "hour": "07"}


class TestSelectResult:
    def test_failed_post_is_spoken_not_confirmed(self, lambda_mod):
        hi = _make_handler_input("Select", {"Selections": "salon"})
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, _question(lambda_mod))

            def fail(*args):
                inst.jee_state = lambda_mod.QuestionStateError(text="Error 500, Jeedom")
                return "Error 500, Jeedom"
            inst.post_jee_event.side_effect = fail
            lambda_mod.SelectIntentHandler().handle(hi)
        hi.response_builder.speak.assert_called_once_with("Error 500, Jeedom")

    def test_suppress_confirmation(self, lambda_mod):
        hi = _make_handler_input("Select", {"Selections": "salon"})
        question = _question(lambda_mod)
        question.suppress_confirmation = True
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, question)
            inst.post_jee_event.side_effect = lambda *a: setattr(inst, "jee_state", None) or ""
            lambda_mod.SelectIntentHandler().handle(hi)
        hi.response_builder.speak.assert_not_called()


class TestFallbackAndUnknown:
    @pytest.mark.parametrize("intent", ["AMAZON.FallbackIntent", "AMAZON.ScrollDownIntent"])
    def test_question_asked_again_nothing_posted(self, lambda_mod, intent):
        hi = _make_handler_input(intent)
        hi._request_type = "IntentRequest"
        handler = lambda_mod.FallbackHandler()
        assert handler.can_handle(hi)
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, _question(lambda_mod))
            handler.handle(hi)
        inst.post_jee_event.assert_not_called()
        hi.response_builder.ask.assert_called_once_with("Quelle pièce ?")

    def test_no_question(self, lambda_mod):
        hi = _make_handler_input("AMAZON.FallbackIntent")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            _mock_jee(MockJee, None)
            lambda_mod.FallbackHandler().handle(hi)
        hi.response_builder.speak.assert_called_once_with("Aucune question en attente.")
        hi.response_builder.set_should_end_session.assert_called_once_with(True)

    def test_no_intent_reflector(self, lambda_mod):
        assert not hasattr(lambda_mod, "IntentReflectorHandler")


class TestStop:
    @pytest.mark.parametrize("intent", ["AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"])
    def test_pending_question_gets_none_and_session_ends(self, lambda_mod, intent):
        hi = _make_handler_input(intent)
        handler = lambda_mod.CancelOrStopIntentHandler()
        assert handler.can_handle(hi)
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, _question(lambda_mod))
            handler.handle(hi)
        inst.post_jee_event.assert_called_once_with(lambda_mod.RESPONSE_NONE, lambda_mod.RESPONSE_NONE)
        hi.response_builder.speak.assert_called_once_with("Au revoir")
        hi.response_builder.set_should_end_session.assert_called_once_with(True)

    def test_without_question_nothing_posted(self, lambda_mod):
        hi = _make_handler_input("AMAZON.StopIntent")
        with patch.object(lambda_mod, "JeeAsk") as MockJee:
            inst = _mock_jee(MockJee, None)
            lambda_mod.CancelOrStopIntentHandler().handle(hi)
        inst.post_jee_event.assert_not_called()


class TestBudget:
    def test_get_capped_post_gets_the_rest(self, lambda_mod):
        jee = _jee(lambda_mod)
        with patch.object(lambda_mod, "HTTP") as http:
            http.request.return_value = _http_response(data={})
            jee._request("GET", "u", cap=lambda_mod.GET_CAP)
            jee._request("POST", "u", body={})
        get_t = http.request.call_args_list[0][1]["timeout"]
        post_t = http.request.call_args_list[1][1]["timeout"]
        assert get_t.total == pytest.approx(lambda_mod.GET_CAP)
        assert lambda_mod.REQUEST_BUDGET - 0.5 < post_t.total <= lambda_mod.REQUEST_BUDGET
        assert get_t.connect_timeout == 1.5 and post_t.connect_timeout == 2.0

    def test_exhausted_budget_no_request(self, lambda_mod):
        jee = _jee(lambda_mod)
        jee._deadline -= lambda_mod.REQUEST_BUDGET
        with patch.object(lambda_mod, "HTTP") as http:
            assert jee._request("POST", "u", body={}) is None
        http.request.assert_not_called()
        assert isinstance(jee.jee_state, lambda_mod.QuestionStateError)

    def test_retry_never_on_timeouts(self, lambda_mod):
        retry = lambda_mod._RETRY
        assert retry.connect == 0 and retry.read == 0 and retry.total == 1

    def test_log_skipped_on_401_and_low_budget(self, lambda_mod):
        jee = _jee(lambda_mod)
        with patch.object(lambda_mod, "HTTP") as http:
            jee._check_response_errors(_http_response(status=401))
            http.request.assert_not_called()
            jee._deadline -= lambda_mod.REQUEST_BUDGET - 1.5
            jee._check_response_errors(_http_response(status=500))
            http.request.assert_not_called()

    def test_log_has_short_timeout_without_retry(self, lambda_mod):
        jee = _jee(lambda_mod)
        with patch.object(lambda_mod, "HTTP") as http:
            jee._check_response_errors(_http_response(status=500))
        kwargs = http.request.call_args[1]
        assert kwargs["timeout"].total == lambda_mod.LOG_CAP and kwargs["retries"] is False
