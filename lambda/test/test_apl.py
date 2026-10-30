"""Carte APL : texte de la réponse parlée affiché sur les appareils à écran."""
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def apl():
    import lambda_function  # noqa: F401  (stubs SDK + intercepteur branché)
    import apl as mod
    return mod


def _hi(with_screen=True, title=None):
    interfaces = SimpleNamespace(alexa_presentation_apl=SimpleNamespace() if with_screen else None)
    device = SimpleNamespace(supported_interfaces=interfaces)
    envelope = SimpleNamespace(context=SimpleNamespace(system=SimpleNamespace(device=device)))
    attrs = {} if title is None else {"apl_title": title}
    return SimpleNamespace(request_envelope=envelope, attributes_manager=SimpleNamespace(request_attributes=attrs))


def _response(ssml="<speak>Il fait 21 degrés &amp; humide.</speak>", directives=None):
    return SimpleNamespace(output_speech=SimpleNamespace(ssml=ssml), directives=directives)


def test_adds_card_with_speech_and_title(apl):
    rsp = _response()
    apl.AplResponseInterceptor().process(_hi(title="température du salon"), rsp)
    assert len(rsp.directives) == 1
    d = rsp.directives[0]
    assert d.token == apl.TOKEN
    assert d.datasources == {"reply": {"title": "température du salon", "text": "Il fait 21 degrés & humide."}}


def test_default_title(apl):
    rsp = _response()
    apl.AplResponseInterceptor().process(_hi(), rsp)
    assert rsp.directives[0].datasources["reply"]["title"] == "Jeedom"


def test_no_screen_no_card(apl):
    rsp = _response()
    apl.AplResponseInterceptor().process(_hi(with_screen=False), rsp)
    assert rsp.directives is None


def test_no_speech_no_card(apl):
    rsp = SimpleNamespace(output_speech=None, directives=None)
    apl.AplResponseInterceptor().process(_hi(), rsp)
    assert rsp.directives is None


def test_keeps_existing_directives_and_no_duplicate(apl):
    hint = object()
    rsp = _response(directives=[hint])
    inter = apl.AplResponseInterceptor()
    inter.process(_hi(), rsp)
    inter.process(_hi(), rsp)
    assert rsp.directives[0] is hint and len(rsp.directives) == 2


def test_missing_context_is_not_a_screen(apl):
    assert apl.supports_apl(SimpleNamespace(request_envelope=SimpleNamespace(context=None))) is False


def test_document_is_valid_json_and_reads_payload(apl):
    doc = json.dumps(apl.DOCUMENT)
    assert '"type": "APL"' in doc
    assert "${payload.reply.text}" in doc and "${payload.reply.title}" in doc


def test_set_title(apl):
    hi = _hi()
    apl.set_title(hi, "quelle heure est-il")
    assert hi.attributes_manager.request_attributes["apl_title"] == "quelle heure est-il"
