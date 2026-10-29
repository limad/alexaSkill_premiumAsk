"""
Mocks de ask_sdk_core pour permettre l'import de lambda_function.py en CI
sans installer la lib (lourde + spécifique runtime Lambda Alexa-hosted).

Les tests qui ont besoin du vrai SDK doivent skipper si non installé.
"""

import sys
import types
from unittest.mock import MagicMock


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# True si on a monté des stubs (SDK absent, ex: CI). Sert à décider si le patch
# de compatibilité get_slot doit s'activer (il ne vaut que pour le VRAI SDK).
_ASK_SDK_STUBBED = False


def _ensure_ask_sdk_mocks():
    """Si ask_sdk_core n'est pas installé, monte des stubs minimaux."""
    global _ASK_SDK_STUBBED
    try:
        import ask_sdk_core  # noqa: F401
        return  # vrai SDK dispo, on n'écrase pas
    except ImportError:
        pass

    _ASK_SDK_STUBBED = True

    # Modules requis par les imports du Lambda
    pkgs = [
        "ask_sdk_core",
        "ask_sdk_core.dispatch_components",
        "ask_sdk_core.skill_builder",
        "ask_sdk_core.utils",
        "ask_sdk_core.handler_input",
        "ask_sdk_core.attributes_manager",
        "ask_sdk_model",
        "ask_sdk_model.slu",
        "ask_sdk_model.slu.entityresolution",
    ]
    for p in pkgs:
        _make_module(p)

    # Classes stubs — héritage neutre
    class _Stub:
        def __init__(self, *a, **kw): pass
        def __call__(self, *a, **kw): return self
        def __getattr__(self, _): return _Stub()

    sys.modules["ask_sdk_core.dispatch_components"].AbstractRequestHandler = _Stub
    sys.modules["ask_sdk_core.dispatch_components"].AbstractRequestInterceptor = _Stub
    sys.modules["ask_sdk_core.dispatch_components"].AbstractExceptionHandler = _Stub
    sys.modules["ask_sdk_core.skill_builder"].SkillBuilder = _Stub

    utils = sys.modules["ask_sdk_core.utils"]
    utils.is_intent_name = lambda name: (lambda hi: getattr(hi, "_intent_name", None) == name)
    utils.is_request_type = lambda name: (lambda hi: getattr(hi, "_request_type", None) == name)
    utils.get_slot_value = lambda hi, slot_name: (getattr(hi, "_slots", None) or {}).get(slot_name) if isinstance(getattr(hi, "_slots", None), dict) else None

    def _stub_get_slot(hi, slot_name):
        """Fabrique un slot object depuis hi._slots (comme le vrai SDK retourne
        un Slot avec .value/.resolutions). _get_resolved_slot s'en sert."""
        slots = getattr(hi, "_slots", None)
        if not isinstance(slots, dict):
            return None
        val = slots.get(slot_name)
        if val is None:
            return None
        slot = MagicMock()
        slot.value = val
        slot.resolutions = None  # pas d'entity resolution en test → fallback .value
        return slot

    utils.get_slot = _stub_get_slot
    utils.get_intent_name = lambda hi: getattr(hi, "_intent_name", None)
    utils.get_account_linking_access_token = lambda hi: getattr(hi, "_access_token", None)

    handler_input = sys.modules["ask_sdk_core.handler_input"]
    handler_input.HandlerInput = _Stub

    er = sys.modules["ask_sdk_model.slu.entityresolution"]
    class _StatusCode:
        ER_SUCCESS_MATCH = "ER_SUCCESS_MATCH"
    er.StatusCode = _StatusCode

    # ask_sdk_model — SessionEndedReason
    class _SessionEndedReason:
        ERROR = "ERROR"
        EXCEEDED_MAX_REPROMPTS = "EXCEEDED_MAX_REPROMPTS"
        USER_INITIATED = "USER_INITIATED"
    sys.modules["ask_sdk_model"].SessionEndedReason = _SessionEndedReason

    # ask_sdk_model.interfaces.display — HintDirective / PlainTextHint
    for p in ["ask_sdk_model.interfaces", "ask_sdk_model.interfaces.display"]:
        _make_module(p)
    sys.modules["ask_sdk_model.interfaces.display"].HintDirective = _Stub
    sys.modules["ask_sdk_model.interfaces.display"].PlainTextHint = _Stub


_ensure_ask_sdk_mocks()


# ---------------------------------------------------------------------------
# Compatibilité mock handler_input quand le vrai SDK est installé
#
# get_slot() et get_slot_value() du SDK lèvent TypeError si le request
# n'est pas un IntentRequest réel. Les tests utilisent des MagicMock — on
# patche les références dans lambda_function pour qu'elles retombent sur
# hi._slots en cas de TypeError, sans toucher le comportement sur vrais objets.
# ---------------------------------------------------------------------------
import pytest


@pytest.fixture(autouse=True)
def _compat_slot_utils():
    # Le patch ne sert QUE pour le vrai SDK (get_slot lève TypeError sur MagicMock).
    # En mode stub (CI sans ask-sdk), les stubs du conftest gèrent déjà tout —
    # patcher par-dessus casserait get_slot_value. On ne fait donc rien.
    if _ASK_SDK_STUBBED:
        yield
        return

    try:
        import lambda_function as _lf
    except ImportError:
        yield
        return

    from unittest.mock import patch, MagicMock as _MM

    _orig_get_slot       = _lf.get_slot
    _orig_get_slot_value = _lf.get_slot_value

    def _get_slot(handler_input, slot_name):
        try:
            return _orig_get_slot(handler_input, slot_name)
        except TypeError:
            val = getattr(handler_input, "_slots", {}).get(slot_name)
            if val is None:
                return None
            m = _MM()
            m.value = val
            m.resolutions = None
            return m

    def _get_slot_value(handler_input, slot_name):
        slot = _get_slot(handler_input, slot_name)
        return slot.value if slot else None

    # is_intent_name et is_request_type utilisent isinstance(request, IntentRequest)
    # qui échoue sur MagicMock. On les remplace par des versions qui tombent sur
    # _intent_name / _request_type comme les stubs du mode sans SDK.
    def _is_intent_name(name):
        return lambda hi: getattr(hi, "_intent_name", None) == name

    def _is_request_type(name):
        return lambda hi: getattr(hi, "_request_type", None) == name

    def _get_intent_name(hi):
        return getattr(hi, "_intent_name", None)

    with patch.object(_lf, "get_slot",        _get_slot), \
         patch.object(_lf, "get_slot_value",   _get_slot_value), \
         patch.object(_lf, "is_intent_name",   _is_intent_name), \
         patch.object(_lf, "is_request_type",  _is_request_type), \
         patch.object(_lf, "get_intent_name",  _get_intent_name):
        yield
