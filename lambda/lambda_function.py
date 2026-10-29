# VERSION 0.13.0
# Refactor : suppression du pattern Borg (fuite cross-user), PoolManager global,
# timeouts courts + retry, fix des bugs headers.update / raise nus / CatchAll.

CODE_VERS = 0.4
TOKEN = ""          # Token account-linking longue durée (laisser vide pour récupération auto)
CAN_POST = True

# ─── Built-in imports ───────────────────────────────────────────────────────
import json
import logging
from typing import Optional, Union

# ─── 3rd-party imports ──────────────────────────────────────────────────────
import re
import urllib3
from urllib3 import HTTPResponse
from urllib3.util.retry import Retry


# Parseur ISO-8601 duration (remplace isodate) — couvre PT#H#M#S, PxxY/M/D, combiné
_ISO_DUR_RE = re.compile(
    r"^P(?:(?P<y>\d+)Y)?(?:(?P<mo>\d+)M)?(?:(?P<d>\d+)D)?"
    r"(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)


def _parse_iso_duration_seconds(duration: Optional[str]) -> float:
    """Parse un ISO-8601 duration en secondes (années=365j, mois=30j — approximation)."""
    if not duration:
        return 0.0
    m = _ISO_DUR_RE.match(duration)
    if not m:
        return 0.0
    g = m.groupdict()
    total = 0.0
    total += int(g["y"] or 0) * 365 * 86400
    total += int(g["mo"] or 0) * 30 * 86400
    total += int(g["d"] or 0) * 86400
    total += int(g["h"] or 0) * 3600
    total += int(g["m"] or 0) * 60
    total += float(g["s"] or 0)
    return total

from ask_sdk_core.dispatch_components import (
    AbstractExceptionHandler,
    AbstractRequestHandler,
    AbstractRequestInterceptor,
)
from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.utils import (
    get_account_linking_access_token,
    get_intent_name,
    get_slot,
    get_slot_value,
    is_intent_name,
    is_request_type,
)
from ask_sdk_model import SessionEndedReason
from ask_sdk_model.interfaces.display import HintDirective, PlainTextHint
from ask_sdk_model.slu.entityresolution import StatusCode

# ─── Local imports ──────────────────────────────────────────────────────────
import prompts
from config import APIKEY, DEBUG, JEEDOM_URL, VERIFY_SSL
from const import (
    RESPONSE_DATE_TIME,
    RESPONSE_DURATION,
    RESPONSE_NO,
    RESPONSE_NONE,
    RESPONSE_NUMERIC,
    RESPONSE_SELECT,
    RESPONSE_STRING,
    RESPONSE_YES,
)
from schemas import QuestionState, QuestionStateError

# ─── URLs pré-calculées ─────────────────────────────────────────────────────
JEEDOM_URL = JEEDOM_URL.rstrip("/")
QUESTION_URL = f"{JEEDOM_URL}/plugins/alexaapiv2/core/php/askQuestion.php"
RESPONSE_URL = f"{JEEDOM_URL}/plugins/alexaapiv2/core/php/askResponse.php?command=reponseASK"
LOG_URL      = f"{JEEDOM_URL}/plugins/alexaapiv2/core/php/askResponse.php?command=log"
VOICE_ROUTER_URL = f"{JEEDOM_URL}/plugins/alexaapiv2/core/php/voiceRouter.php"
# APIKEY est passée en header Authorization (cf. _get_headers ci-dessous), plus en query string

# ─── Logging ────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

# ─── HTTP pool GLOBAL (réutilisé entre invocations chaudes — évite handshake TLS) ──
# Timeouts courts : Alexa coupe à 8 s, mieux vaut échouer vite sur réseau lent.
# Retry : 1 essai supplémentaire sur 5xx (reload plugin Jeedom temporaire).
# Compat urllib3 : `allowed_methods` (≥1.26) / `method_whitelist` (1.25.x).
_RETRY_KW = dict(
    total=1,
    backoff_factor=0.3,
    status_forcelist=(500, 502, 503, 504),
)
try:
    _RETRY = Retry(allowed_methods=frozenset(["GET", "POST"]), **_RETRY_KW)
except TypeError:
    try:
        _RETRY = Retry(method_whitelist=frozenset(["GET", "POST"]), **_RETRY_KW)
    except TypeError:
        _RETRY = Retry(**_RETRY_KW)

HTTP = urllib3.PoolManager(
    cert_reqs="CERT_REQUIRED" if VERIFY_SSL else "CERT_NONE",
    timeout=urllib3.Timeout(connect=2.0, read=3.0),
    retries=_RETRY,
    maxsize=4,
)

# ─── i18n : chargé une seule fois au cold-start (vs à chaque requête) ───────
try:
    with open("language_strings.json", encoding="utf-8") as _f:
        LANGUAGE_DATA = json.load(_f)
except Exception as _e:
    logger.error("Impossible de charger language_strings.json: %s", _e)
    LANGUAGE_DATA = {"en": {}}


def _string_to_bool(value, default: bool = False) -> bool:
    """Convertit un str 'true'/'false' en bool; fallback `default` sinon."""
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return default
    v = value.strip().lower()
    if v == "true":
        return True
    if v == "false":
        return False
    return default


def _get_resolved_slot(handler_input, slot_name: str) -> Optional[str]:
    """
    Retourne la valeur résolue via entity resolution Alexa si disponible
    (cas slot JeeCommand → valeur canonique Jeedom), sinon la valeur brute.
    Fallback transparent : si aucune résolution, se comporte comme get_slot_value().
    """
    slot = get_slot(handler_input, slot_name)
    if slot and slot.resolutions:
        try:
            for auth in (slot.resolutions.resolutions_per_authority or []):
                if auth.status and auth.status.code and auth.status.code.value == "ER_SUCCESS_MATCH":
                    if auth.values:
                        resolved = auth.values[0].value.name
                        if resolved:
                            logger.debug("slot %s resolved: '%s' → '%s'", slot_name, slot.value, resolved)
                            return resolved
        except (AttributeError, IndexError, TypeError):
            pass
    return slot.value if slot else None


def _locale_short(handler_input) -> str:
    """Code langue court (ex: 'fr') depuis la locale Echo, 'fr' par défaut."""
    try:
        return (handler_input.request_envelope.request.locale or "fr")[:2].lower()
    except AttributeError:
        return "fr"


def _qa_guard_no_question(handler_input, jee, label: str):
    """
    Garde anti-crash pour les handlers de réponse Q/A à slot TYPÉ (Number/Duration/
    Date) : ces intents n'ont de sens que pendant un dialogue Q/A. Si le NLU les
    déclenche hors contexte (ex: "allumer le four" capté par l'intent Date via le
    slot AMAZON.DATE), aucune QuestionState n'est active et le slot ne contient pas
    la phrase brute. On répond proprement NO_MATCH au lieu de POSTer un event vide
    → évite la réponse sans speech qu'Alexa rejette
    ("Désolé, j'ai quelques problèmes").

    Retourne une Response si hors contexte Q/A, sinon None (flow normal).
    """
    if isinstance(jee.jee_state, QuestionState):
        return None
    data = handler_input.attributes_manager.request_attributes.get("_", {})
    logger.info("%s hors contexte Q/A → réponse no-match", label)
    return _handle_response(handler_input, data.get(prompts.NO_MATCH, "Je n'ai pas compris votre demande."))


def _add_hint(response_builder, text: str):
    """Écrase le hint système Alexa par un texte personnalisé (appareils à écran)."""
    try:
        response_builder.add_directive(HintDirective(hint=PlainTextHint(text=text)))
    except Exception:
        pass


def _handle_response(handler, speak_out: Optional[str]):
    """Renvoie la Response Alexa avec ou sans speech (permet de laisser Jeedom parler)."""
    if speak_out:
        return handler.response_builder.speak(speak_out).response
    return handler.response_builder.response


def _get_request_text(handler_input, slot_names=()):
    """Retourne le texte brut si présent, sinon concatène les slots utiles."""
    request = getattr(getattr(handler_input, "request_envelope", None), "request", None)
    if request:
        for attr in ("input_transcript", "inputTranscript", "raw_text", "rawText", "utterance"):
            value = getattr(request, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
    values = []
    for slot_name in slot_names:
        value = (_get_resolved_slot(handler_input, slot_name) or "").strip()
        if value and value != "?":
            values.append(value)
    return " ".join(values).strip()


def _handle_qa_response(handler_input, jee, speak_out: Optional[str]):
    """
    Helper Q/A avec support dialog multi-tour.
    Après un post_jee_event, jee.jee_state peut contenir une nouvelle question
    (pollée par askResponse.php pendant 1.5s). Dans ce cas on garde la session
    ouverte et on prononce la nouvelle question. Sinon flow standard.
    """
    if isinstance(jee.jee_state, QuestionState) and jee.jee_state.text:
        # Multi-tour : nouvelle question Jeedom dans la même session Alexa
        logger.info("Dialog multi-tour: enchaînement question")
        data = handler_input.attributes_manager.request_attributes.get("_", {})
        builder = (handler_input.response_builder
                   .speak(jee.jee_state.text)
                   .ask(jee.jee_state.text)
                   .set_should_end_session(False))
        _add_hint(builder, data.get(prompts.HINT_TEXT, "dire arrête"))
        return builder.response
    # Flow standard : 1 réponse utilisateur → fin
    return _handle_response(handler_input, speak_out)


class JeeAsk:
    """Wrapper Jeedom — instance par invocation Lambda (pas de state partagé)."""

    jee_state: Optional[Union[QuestionState, QuestionStateError]]

    def __init__(self, handler_input=None, fetch_question: bool = True):
        self.handler_input = handler_input
        self.jee_state = None
        self.language_strings = {}

        if handler_input is not None:
            self.language_strings = handler_input.attributes_manager.request_attributes.get("_", {})
            self.token = self._fetch_token() if TOKEN == "" else TOKEN
            if fetch_question:
                self.get_jeeQuestion()
        else:
            self.token = TOKEN

    # ── auth ────────────────────────────────────────────────────────────────
    def _fetch_token(self):
        if DEBUG:
            logger.debug("Fetching Jeedom token from Alexa account linking")
        try:
            return get_account_linking_access_token(self.handler_input)
        except Exception:
            return ""

    def _get_headers(self) -> dict:
        """
        Headers HTTP communs. Authorization = token Jeedom (Bearer).
        Priorité : token OAuth Account Linking (self.token) → APIKEY statique (fallback).
        Évite de mettre le token en query string où il finirait en clair dans les logs.
        """
        return {
            "Authorization": f"Bearer {self.token or APIKEY}",
            "Content-Type": "application/json",
        }

    # ── helpers ─────────────────────────────────────────────────────────────
    def _set_jee_error(self, prompt_key: str):
        self.jee_state = QuestionStateError(text=self.language_strings.get(prompt_key, "Error"))

    def _check_response_errors(self, response: HTTPResponse) -> Optional[str]:
        """Retourne None si OK, sinon le texte d'erreur à dire."""
        st = response.status
        if st < 400:
            return None
        key = {401: prompts.ERROR_401, 404: prompts.ERROR_404}.get(st, prompts.ERROR_400)
        speak = f"Error {st}, {self.language_strings.get(key, 'Jeedom error')}"
        logger.error("Jeedom HTTP %s — see CloudWatch for details", st)
        if DEBUG:
            logger.debug("error body: %s", response.data)
        self.post_jee_log(speak)
        return speak

    def _request(self, method: str, url: str, *, body: Optional[dict] = None, extra_headers: Optional[dict] = None):
        """Exécute une requête HTTP avec retry/timeout hérités du pool global."""
        headers = self._get_headers()
        if extra_headers:
            headers.update(extra_headers)  # in-place, NE PAS réassigner (dict.update() renvoie None)
        try:
            kwargs = {"headers": headers}
            if body is not None:
                kwargs["body"] = json.dumps(body).encode("utf-8")
            response = HTTP.request(method, url, **kwargs)
        except urllib3.exceptions.MaxRetryError as e:
            logger.error("Jeedom HTTP MaxRetry: %s", e)
            self.jee_state = QuestionStateError(text=self.language_strings.get(prompts.ERROR_CONFIG, "Jeedom unreachable"))
            return None
        except Exception as e:
            logger.error("Jeedom HTTP error: %s", e)
            self.jee_state = QuestionStateError(text=self.language_strings.get(prompts.ERROR_CONFIG, "Jeedom error"))
            return None

        err = self._check_response_errors(response)
        if err is not None:
            self.jee_state = QuestionStateError(text=err)
            return None
        return response

    # ── pull question (GET askQuestion.php) ─────────────────────────────────
    def get_jeeQuestion(self):
        response = self._request("GET", QUESTION_URL)
        if response is None:
            return

        try:
            outer = json.loads(response.data.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            logger.error("Jeedom renvoie un JSON invalide")
            self._set_jee_error(prompts.ERROR_CONFIG)
            return

        state_str = outer.get("state")
        if not state_str:
            logger.info("Pas de question Jeedom en attente (alexaAsk.json vide)")
            self._set_jee_error(prompts.ERROR_CONFIG)
            return

        try:
            state = json.loads(state_str)
        except json.JSONDecodeError:
            logger.error("alexaAsk.json.state mal formé")
            self._set_jee_error(prompts.ERROR_CONFIG)
            return

        text = state.get("text") or ""
        if not text:
            self._set_jee_error(prompts.ERROR_CONFIG)
            return

        self.jee_state = QuestionState(
            event_id=state.get("event"),
            suppress_confirmation=_string_to_bool(state.get("suppress_confirmation")),
            text=text,
            deviceSerialNumber=state.get("deviceSerialNumber"),
            textBrut=state.get("textBrut") or "",
        )

    # ── push event (POST askResponse.php) ───────────────────────────────────
    def post_jee_event(self, response_value, response_type: str, **kwargs) -> Optional[str]:
        if not isinstance(self.jee_state, QuestionState):
            logger.warning("post_jee_event sans QuestionState valide (state=%s)", type(self.jee_state).__name__)
            return self.language_strings.get(prompts.ERROR_CONFIG, "")

        body = {
            "event_id": self.jee_state.event_id,
            "event_response": response_value,
            "event_response_type": response_type,
            "deviceSerialNumber": self.jee_state.deviceSerialNumber,
            "textBrut": self.jee_state.textBrut,
            "code_version": CODE_VERS,
        }
        body.update(kwargs)

        # Injection person_id si Alexa Voice ID identifie l'utilisateur
        try:
            person = self.handler_input.request_envelope.context.system.person
            if person:
                body["event_person_id"] = person.person_id
        except AttributeError:
            pass

        # Sauvegarder le flag avant l'appel (car `_request` peut remplacer jee_state par une erreur)
        suppress = self.jee_state.suppress_confirmation
        response = self._request("POST", RESPONSE_URL, body=body)
        if response is None:
            # `_request` a mis un QuestionStateError → on dit son text à l'utilisateur
            if isinstance(self.jee_state, QuestionStateError):
                return self.jee_state.text
            return ""

        # ── Dialog multi-tour ────────────────────────────────────────────────
        # Si askResponse.php a polled alexaAsk.json et trouvé une nouvelle
        # question, on l'extrait et on remplace self.jee_state. Le handler
        # appelant (Yes/No/String/Number/etc) inspectera self.jee_state après
        # post_jee_event et pourra décider de garder la session ouverte.
        next_q = None
        try:
            data = json.loads(response.data.decode("utf-8") or "{}")
            next_q = data.get("next_question") if isinstance(data, dict) else None
        except (json.JSONDecodeError, AttributeError):
            pass

        if isinstance(next_q, dict) and next_q.get("text"):
            logger.info("Dialog multi-tour: nouvelle question reçue (event=%s)", next_q.get("event"))
            self.jee_state = QuestionState(
                event_id=next_q.get("event"),
                suppress_confirmation=_string_to_bool(next_q.get("suppress_confirmation")),
                text=next_q["text"],
                deviceSerialNumber=next_q.get("deviceSerialNumber"),
                textBrut=next_q.get("textBrut") or "",
            )
            # Retourne la question elle-même comme "réponse" — le handler la speak
            return next_q["text"]

        # Pas de question suivante : flow standard
        if not suppress:
            self.jee_state = None
            return self.language_strings.get(prompts.OKAY, "Ok")

        self.jee_state = None
        return ""

    # ── voice control (mode direct — user commande Jeedom directement) ──────
    def post_voice_command(self, query: str, force_exact: bool = False) -> dict:
        """
        Envoie la commande au routeur vocal Jeedom (voiceRouter.php).
        Le routeur décide lui-même entre voiceControl et voiceQuery selon la config
        Jeedom en temps réel — plus de redéploiement Lambda pour changer de mode.
        """
        fallback = self.language_strings.get(prompts.NO_MATCH, "Je n'ai pas compris.")
        if not query or not query.strip():
            return {"reply": fallback, "matched": False, "ambiguous": False, "options": []}

        device_sn = ""
        person_id = ""
        try:
            device_sn = self.handler_input.request_envelope.context.system.device.device_id or ""
        except AttributeError:
            pass
        try:
            person = self.handler_input.request_envelope.context.system.person
            if person:
                person_id = person.person_id or ""
        except AttributeError:
            pass

        body = {
            "query": query.strip(),
            "deviceSerialNumber": device_sn,
            "personId": person_id,
            "forceExact": bool(force_exact),
            "code_version": CODE_VERS,
        }
        response = self._request("POST", VOICE_ROUTER_URL, body=body)
        if response is None:
            return {"reply": self.language_strings.get(prompts.ERROR_CONFIG, "Jeedom ne répond pas."),
                    "matched": False, "ambiguous": False, "options": [], "http_error": True}
        try:
            data = json.loads(response.data.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            logger.error("voiceRouter: réponse JSON invalide")
            return {"reply": self.language_strings.get(prompts.ERROR_CONFIG, "Réponse invalide."),
                    "matched": False, "ambiguous": False, "options": [], "http_error": True}
        logger.info("voiceRouter: route=%s matched=%s elapsed=%sms",
                    data.get("route"), data.get("matched"), data.get("elapsed_ms"))
        return {
            "reply":      data.get("reply") or "",
            "matched":    bool(data.get("matched")),
            "ambiguous":  bool(data.get("ambiguous")),
            "options":    data.get("options") or [],
            "query":      data.get("query", query.strip()),
            "http_error": False,
        }

    # ── log push vers Jeedom ────────────────────────────────────────────────
    def post_jee_log(self, log_text: str, **kwargs):
        """Envoie un log au plugin côté Jeedom (askResponse.php?command=log). Best-effort."""
        body = {"log": log_text, "code_version": CODE_VERS}
        body.update(kwargs)
        try:
            HTTP.request(
                "POST", LOG_URL,
                headers=self._get_headers(),
                body=json.dumps(body).encode("utf-8"),
            )
        except Exception:
            pass  # best-effort — on ne veut JAMAIS que le logging casse le skill

    # ── slot resolution helper ──────────────────────────────────────────────
    def get_value_for_slot(self, slot_name: str) -> Optional[str]:
        """Retourne la valeur canonique du slot (resolved), sinon None."""
        slot = get_slot(self.handler_input, slot_name=slot_name)
        if not (slot and slot.resolutions and slot.resolutions.resolutions_per_authority):
            return None
        for resolution in slot.resolutions.resolutions_per_authority:
            if resolution.status.code == StatusCode.ER_SUCCESS_MATCH:
                for v in resolution.values:
                    if v.value and v.value.name:
                        return v.value.name
        return None


# ════════════════════════════════════════════════════════════════════════════
# Handlers
# ════════════════════════════════════════════════════════════════════════════

class LaunchRequestHandler(AbstractRequestHandler):
    """
    Deux modes :
      A) Q/A (initiative Jeedom) — alexaAsk.json contient une question.
         Lambda parle la question et attend la réponse.
      B) Contrôle direct (initiative user) — alexaAsk.json vide ou absent.
         Lambda invite l'user à donner une commande ("Que puis-je pour vous ?").
    """
    def can_handle(self, handler_input):
        return is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        jee = JeeAsk(handler_input)
        data = handler_input.attributes_manager.request_attributes.get("_", {})
        hint_text = data.get(prompts.HINT_TEXT, "dire arrête")

        # Mode A : Q/A classique (Jeedom a posé une question)
        if isinstance(jee.jee_state, QuestionState) and jee.jee_state.text:
            builder = handler_input.response_builder.speak(jee.jee_state.text)
            if jee.jee_state.event_id:
                builder.ask("")
            _add_hint(builder, hint_text)
            return builder.response

        # Mode B : contrôle direct — prompt court et clair (i18n via DIRECT_PROMPT)
        prompt_txt = data.get(prompts.DIRECT_PROMPT, "Que puis-je pour vous ?")
        builder = handler_input.response_builder.speak(prompt_txt).ask(prompt_txt)
        _add_hint(builder, hint_text)
        return builder.response


"""
Verbes Voice* par locale (langue Echo).
Ces verbes sont préfixés à la phrase reçue avant POST vers Jeedom — l'utilisateur
doit avoir des interactions Jeedom configurées dans la même langue que son Echo.
"""
VOICE_VERBS = {
    "fr": {"VoiceLaunch": "activer", "VoiceTurnOn": "allumer",     "VoiceTurnOff": "éteindre",    "VoiceSet": "régler",   "VoiceQuery": ""},
    "en": {"VoiceLaunch": "activate","VoiceTurnOn": "turn on",     "VoiceTurnOff": "turn off",    "VoiceSet": "set",      "VoiceQuery": ""},
    "es": {"VoiceLaunch": "activar", "VoiceTurnOn": "encender",    "VoiceTurnOff": "apagar",      "VoiceSet": "ajustar",  "VoiceQuery": ""},
    "de": {"VoiceLaunch": "starten", "VoiceTurnOn": "einschalten", "VoiceTurnOff": "ausschalten", "VoiceSet": "stellen",  "VoiceQuery": ""},
    "it": {"VoiceLaunch": "attivare","VoiceTurnOn": "accendere",   "VoiceTurnOff": "spegnere",    "VoiceSet": "impostare","VoiceQuery": ""},
    "pt": {"VoiceLaunch": "ativar",  "VoiceTurnOn": "ligar",       "VoiceTurnOff": "desligar",    "VoiceSet": "definir",  "VoiceQuery": ""},
}


def _handle_voice_intent(handler_input, intent_name: str):
    """
    Handler générique pour les intents Voice* : reconstruit la phrase en préfixant
    le verbe (qu'Alexa a consommé dans le carrier), traduit selon la locale Echo,
    puis forwarde à Jeedom. Si Jeedom détecte plusieurs interactions proches,
    on stocke les options en session et on demande à l'utilisateur de choisir.
    """
    jee = JeeAsk(handler_input, fetch_question=False)
    # VoiceQuery utilise le slot "Query" (AMAZON.SearchQuery, open-ended pour le LLM)
    # Les autres Voice* utilisent "Command" (JeeCommand, NLU entraîné sur les équipements Jeedom)
    slot_name = "Query" if intent_name == "VoiceQuery" else "Command"
    query = (_get_resolved_slot(handler_input, slot_name) or "").strip()
    if not query:
        data = handler_input.attributes_manager.request_attributes.get("_", {})
        return _handle_response(handler_input, data.get(prompts.NO_MATCH, "Je n'ai pas compris votre commande."))
    # Verbe locale-aware (fallback fr si locale inconnue)
    locale_short = _locale_short(handler_input)
    verb = VOICE_VERBS.get(locale_short, VOICE_VERBS["fr"]).get(intent_name, "")
    full_query = f"{verb} {query}".strip()
    logger.info("VoiceCommand[%s]: %s", locale_short, full_query)
    return _dispatch_voice_query(handler_input, full_query, locale_short)


def _dispatch_voice_query(handler_input, full_query: str, locale_short: str = "fr"):
    """
    Forwarde une phrase vocale déjà reconstruite à Jeedom (voiceRouter) et gère
    le résultat : erreur HTTP, désambiguïsation, no-match, succès.

    Réutilisé par _handle_voice_intent ET par les handlers de réponse Q/A
    (Select/String/…) quand ils sont déclenchés HORS contexte Q/A : le NLU peut
    router "demande à jeedom la température du thermostat" vers l'intent Select
    (sample générique "la {Selections}") alors qu'aucune question n'est en attente.
    Dans ce cas on traite la phrase comme une commande/question vocale normale.
    """
    jee  = JeeAsk(handler_input, fetch_question=False)
    data = handler_input.attributes_manager.request_attributes.get("_", {})
    result = jee.post_voice_command(full_query)

    # ── Erreur HTTP : Jeedom injoignable ou réponse invalide ─────────────────
    if result.get("http_error"):
        return _handle_response(handler_input, data.get(prompts.ERROR_CONFIG, "Jeedom ne répond pas."))

    # ── Disambiguation : 2+ matches proches → on stocke et on demande ────────
    if result.get("ambiguous") and result.get("options"):
        sess = handler_input.attributes_manager.session_attributes
        sess["pending_voice_options"] = result["options"]
        logger.info("VoiceCommand[%s]: ambiguous (%d options) → eliciting choice",
                    locale_short, len(result["options"]))
        builder = (handler_input.response_builder
                   .speak(result["reply"])
                   .ask(result["reply"])
                   .set_should_end_session(False))
        _add_hint(builder, data.get(prompts.HINT_TEXT, "dire arrête"))
        return builder.response

    # ── Aucune interaction trouvée ────────────────────────────────────────────
    if not result.get("matched"):
        no_match = data.get(prompts.NO_MATCH, "Aucune commande trouvée pour cette demande.")
        logger.info("VoiceCommand[%s]: no match for '%s'", locale_short, full_query)
        return handler_input.response_builder.speak(no_match).set_should_end_session(True).response

    # ── Succès ────────────────────────────────────────────────────────────────
    return handler_input.response_builder.speak(result["reply"]).set_should_end_session(True).response


def _forward_misrouted_intent(handler_input, intent_name: str, slot_names=()):
    data = handler_input.attributes_manager.request_attributes.get("_", {})
    locale_short = _locale_short(handler_input)
    query = _get_request_text(handler_input, slot_names)
    if not query:
        jee = JeeAsk(handler_input, fetch_question=False)
        logger.info("%s misrouted without usable text", intent_name)
        jee.post_jee_log("Intent Alexa sans texte exploitable", intent=intent_name)
        no_match = data.get(prompts.NO_MATCH, "Aucune commande trouvée pour cette demande.")
        return handler_input.response_builder.speak(no_match).set_should_end_session(True).response

    logger.info("%s misrouted -> VoiceCommand[%s]: %s", intent_name, locale_short, query)
    return _dispatch_voice_query(handler_input, query, locale_short)


class DisambiguationIntentHandler(AbstractRequestHandler):
    """
    Active si l'utilisateur a reçu un prompt "1 : phrase A, 2 : phrase B, lequel ?"
    et répond "le deuxième" ou "deux". Slot Choice = AMAZON.NUMBER.
    Re-poste la phrase choisie à Jeedom avec forceExact=true (skip re-disambiguation).
    """
    def can_handle(self, handler_input):
        return is_intent_name("DisambiguationIntent")(handler_input)

    def handle(self, handler_input):
        sess = handler_input.attributes_manager.session_attributes
        options = sess.get("pending_voice_options") or []
        data = handler_input.attributes_manager.request_attributes.get("_", {})
        choice_raw = (get_slot_value(handler_input, "Choice") or "").strip()

        # Plus d'options en attente → l'user a parlé hors contexte
        if not options:
            logger.info("DisambiguationIntent sans choix en attente (choice=%s)", choice_raw)
            msg = data.get(prompts.NO_MATCH, "Aucune commande trouvée pour cette demande.")
            return handler_input.response_builder.speak(msg).set_should_end_session(True).response

        try:
            idx = int(choice_raw) - 1
        except (ValueError, TypeError):
            idx = -1
        if idx < 0 or idx >= len(options):
            msg = "Choix invalide. " + (sess.get("last_disambig_prompt") or "Reformule.")
            return (handler_input.response_builder.speak(msg).ask(msg)
                    .set_should_end_session(False).response)

        # Nettoie la session avant relance
        chosen = options[idx]
        sess.pop("pending_voice_options", None)

        jee = JeeAsk(handler_input, fetch_question=False)
        logger.info("DisambiguationIntent: chosen #%d → '%s'", idx + 1, chosen)
        result = jee.post_voice_command(chosen, force_exact=True)
        return handler_input.response_builder.speak(result["reply"]).set_should_end_session(True).response


class VoiceLaunchIntentHandler(AbstractRequestHandler):
    """lance/launch/lanza/etc {X} → 'activer X' (locale-aware)."""
    def can_handle(self, handler_input):
        return is_intent_name("VoiceLaunch")(handler_input)

    def handle(self, handler_input):
        return _handle_voice_intent(handler_input, "VoiceLaunch")


class VoiceTurnOnIntentHandler(AbstractRequestHandler):
    """allume/turn on/enciende/etc {X} → 'allumer X' (locale-aware)."""
    def can_handle(self, handler_input):
        return is_intent_name("VoiceTurnOn")(handler_input)

    def handle(self, handler_input):
        return _handle_voice_intent(handler_input, "VoiceTurnOn")


class VoiceTurnOffIntentHandler(AbstractRequestHandler):
    """éteins/turn off/apaga/etc {X} → 'éteindre X' (locale-aware)."""
    def can_handle(self, handler_input):
        return is_intent_name("VoiceTurnOff")(handler_input)

    def handle(self, handler_input):
        return _handle_voice_intent(handler_input, "VoiceTurnOff")


class VoiceSetIntentHandler(AbstractRequestHandler):
    """mets/set/pon/etc {X} → 'régler X' (locale-aware)."""
    def can_handle(self, handler_input):
        return is_intent_name("VoiceSet")(handler_input)

    def handle(self, handler_input):
        return _handle_voice_intent(handler_input, "VoiceSet")


class VoiceQueryIntentHandler(AbstractRequestHandler):
    """quelle est/what is/cuál es {X} → lecture valeur capteur Jeedom (locale-aware)."""
    def can_handle(self, handler_input):
        return is_intent_name("VoiceQuery")(handler_input)

    def handle(self, handler_input):
        return _handle_voice_intent(handler_input, "VoiceQuery")


class YesIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.YesIntent")(handler_input)

    def handle(self, handler_input):
        logger.info("Yes Intent")
        jee = JeeAsk(handler_input)
        speak_output = jee.post_jee_event(RESPONSE_YES, RESPONSE_YES)
        return _handle_qa_response(handler_input, jee, speak_output)


class NoIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.NoIntent")(handler_input)

    def handle(self, handler_input):
        logger.info("No Intent")
        jee = JeeAsk(handler_input)
        speak_output = jee.post_jee_event(RESPONSE_NO, RESPONSE_NO)
        return _handle_qa_response(handler_input, jee, speak_output)


class NumericIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("Number")(handler_input)

    def handle(self, handler_input):
        logger.info("Numeric Intent")
        jee = JeeAsk(handler_input)
        guard = _qa_guard_no_question(handler_input, jee, "Numeric")
        if guard is not None:
            return guard
        number = get_slot_value(handler_input, "Numbers")
        if number in (None, "", "?"):
            jee.post_jee_event(RESPONSE_NONE, RESPONSE_NONE)
            data = handler_input.attributes_manager.request_attributes.get("_", {})
            return _handle_response(handler_input, data.get(prompts.ERROR_CONFIG, "Valeur invalide"))
        speak_output = jee.post_jee_event(number, RESPONSE_NUMERIC)
        return _handle_qa_response(handler_input, jee, speak_output)


class StringIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("String")(handler_input)

    def handle(self, handler_input):
        logger.info("String Intent")
        jee = JeeAsk(handler_input)
        strings = get_slot_value(handler_input, "Strings") or ""

        # Hors contexte Q/A : aucune question active → le NLU a routé une commande
        # vocale (sample "c'est {Strings}", "je dis {Strings}") vers String. On la
        # traite comme une commande vocale au lieu de la POSTer comme réponse Q/A.
        if not isinstance(jee.jee_state, QuestionState):
            if strings:
                logger.info("String hors Q/A → redispatch vocal: '%s'", strings)
                return _dispatch_voice_query(handler_input, strings, _locale_short(handler_input))
            data = handler_input.attributes_manager.request_attributes.get("_", {})
            return _handle_response(handler_input, data.get(prompts.NO_MATCH, "Je n'ai pas compris."))

        speak_output = jee.post_jee_event(strings, RESPONSE_STRING)
        return _handle_qa_response(handler_input, jee, speak_output)


class SelectIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("Select")(handler_input)

    def handle(self, handler_input):
        logger.info("Select Intent")
        jee = JeeAsk(handler_input)
        selection = jee.get_value_for_slot("Selections") or get_slot_value(handler_input, "Selections")

        # Hors contexte Q/A : aucune question Jeedom en attente. Le NLU a routé une
        # commande/question vocale (ex: "la température du thermostat") vers Select
        # à cause du sample générique "la {Selections}". On la traite comme une
        # commande vocale normale au lieu de répondre "Vous avez choisi …".
        if not isinstance(jee.jee_state, QuestionState):
            if selection:
                logger.info("Select hors Q/A → redispatch vocal: '%s'", selection)
                return _dispatch_voice_query(handler_input, selection, _locale_short(handler_input))
            data = handler_input.attributes_manager.request_attributes.get("_", {})
            return _handle_response(handler_input, data.get(prompts.NO_MATCH, "Je n'ai pas compris."))

        if not selection:
            jee.post_jee_event(RESPONSE_NONE, RESPONSE_NONE)
            data = handler_input.attributes_manager.request_attributes.get("_", {})
            return _handle_response(handler_input, data.get(prompts.NO_MATCH, "Je n'ai pas compris votre sélection."))

        jee.post_jee_event(selection, RESPONSE_SELECT)
        data = handler_input.attributes_manager.request_attributes.get("_", {})
        template = data.get(prompts.SELECTED, "{}")
        # post_jee_event a peut-être MAJ jee.jee_state (multi-tour) — on speak la
        # confirmation "Vous avez choisi X" puis si nouvelle question, on la chaîne
        confirmation = template.format(selection)
        if isinstance(jee.jee_state, QuestionState) and jee.jee_state.text:
            return (handler_input.response_builder
                    .speak(confirmation + " " + jee.jee_state.text)
                    .ask(jee.jee_state.text)
                    .set_should_end_session(False)
                    .response)
        return _handle_response(handler_input, confirmation)


class DurationIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("Duration")(handler_input)

    def handle(self, handler_input):
        logger.info("Duration Intent")
        jee = JeeAsk(handler_input)
        guard = _qa_guard_no_question(handler_input, jee, "Duration")
        if guard is not None:
            return guard
        duration = get_slot_value(handler_input, "Durations")
        if not duration:
            jee.post_jee_event(RESPONSE_NONE, RESPONSE_NONE)
            data = handler_input.attributes_manager.request_attributes.get("_", {})
            return _handle_response(handler_input, data.get(prompts.ERROR_CONFIG, "Durée invalide"))

        seconds = _parse_iso_duration_seconds(duration)
        speak_output = jee.post_jee_event(seconds, RESPONSE_DURATION)
        return _handle_qa_response(handler_input, jee, speak_output)


class DateTimeIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("Date")(handler_input)

    def handle(self, handler_input):
        logger.info("Date/Time Intent")
        date = get_slot_value(handler_input, "Dates")
        time = get_slot_value(handler_input, "Times")

        if not date and not time:
            return _forward_misrouted_intent(handler_input, "Date", ("Dates", "Times"))

        jee = JeeAsk(handler_input)
        guard = _qa_guard_no_question(handler_input, jee, "Date")
        if guard is not None:
            return guard
        payload = json.dumps({**self._parse_date(date), **self._parse_time(time)})
        speak_output = jee.post_jee_event(payload, RESPONSE_DATE_TIME)
        return _handle_qa_response(handler_input, jee, speak_output)

    @staticmethod
    def _parse_date(date: Optional[str]) -> dict:
        out = {"day": None, "month": None, "year": None}
        if not date:
            return out
        parts = date.split("-")
        n = len(parts)
        out["year"]  = parts[0] if n >= 1 else None
        out["month"] = parts[1] if n >= 2 else None
        out["day"]   = parts[2] if n >= 3 else None
        return out

    @staticmethod
    def _parse_time(time: Optional[str]) -> dict:
        out = {"seconds": None, "minute": None, "hour": None}
        if not time:
            return out
        low = time.lower()
        if "s" in low:
            out["seconds"] = low.replace("s", "")
            return out
        if "m" in low:
            out["minute"] = low.replace("m", "")
            return out
        if "h" in low:
            out["hour"] = low.replace("h", "")
            return out
        parts = time.split(":")
        n = len(parts)
        out["hour"]    = parts[0] if n >= 1 else None
        out["minute"]  = parts[1] if n >= 2 else None
        out["seconds"] = parts[2] if n >= 3 else None
        return out


class RepeatIntentHandler(AbstractRequestHandler):
    """User dit 'répète' / 'redis' / 'pardon' → on reprononce la dernière question Ask.
    Si pas de question en cache, fallback prompt direct."""
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.RepeatIntent")(handler_input)

    def handle(self, handler_input):
        logger.info("Repeat Intent")
        jee = JeeAsk(handler_input)  # re-fetch alexaAsk.json
        if isinstance(jee.jee_state, QuestionState) and jee.jee_state.text:
            return handler_input.response_builder.speak(jee.jee_state.text).ask("").response
        # Pas de question en cache → prompt direct
        data = handler_input.attributes_manager.request_attributes.get("_", {})
        prompt_txt = data.get(prompts.DIRECT_PROMPT, "Que puis-je pour vous ?")
        return handler_input.response_builder.speak(prompt_txt).ask(prompt_txt).response


class HelpIntentHandler(AbstractRequestHandler):
    """Répond à AMAZON.HelpIntent avec le message d'aide localisé."""
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        logger.info("Help Intent")
        data = handler_input.attributes_manager.request_attributes.get("_", {})
        speak = data.get(prompts.HELP_MESSAGE, "Posez moi votre question.")
        return handler_input.response_builder.speak(speak).ask(speak).response


class CancelOrStopIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return (
            is_intent_name("AMAZON.CancelIntent")(handler_input)
            or is_intent_name("AMAZON.StopIntent")(handler_input)
        )

    def handle(self, handler_input):
        logger.info("Cancel/Stop Intent")
        data = handler_input.attributes_manager.request_attributes.get("_", {})
        return _handle_response(handler_input, data.get(prompts.STOP_MESSAGE, "Au revoir"))


class FallbackHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.FallbackIntent")(handler_input)

    def handle(self, handler_input):
        logger.info("Fallback")
        jee = JeeAsk(handler_input)
        jee.post_jee_event(RESPONSE_NONE, RESPONSE_NONE)
        return handler_input.response_builder.response


class SessionEndedRequestHandler(AbstractRequestHandler):
    """N'appelle PAS askQuestion.php — la session est finie, plus besoin de récupérer une question."""
    def can_handle(self, handler_input):
        return is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        logger.info("Session Ended")
        reason = handler_input.request_envelope.request.reason
        if reason in (SessionEndedReason.EXCEEDED_MAX_REPROMPTS, SessionEndedReason.USER_INITIATED):
            jee = JeeAsk(handler_input, fetch_question=False)
            jee.post_jee_event(RESPONSE_NONE, RESPONSE_NONE)
        return handler_input.response_builder.response


class IntentReflectorHandler(AbstractRequestHandler):
    """Dev-only : répète l'intent reçu (utile pour tester le modèle)."""
    def can_handle(self, handler_input):
        return is_request_type("IntentRequest")(handler_input)

    def handle(self, handler_input):
        intent_name = get_intent_name(handler_input)
        return handler_input.response_builder.speak(f"You just triggered {intent_name}.").response


class CatchAllExceptionHandler(AbstractExceptionHandler):
    """Capture tout ce qui échappe aux handlers. Ne refait PAS de GET Jeedom."""
    def can_handle(self, handler_input, exception):
        return True

    def handle(self, handler_input, exception):
        logger.error("Unhandled exception: %s", exception, exc_info=True)
        data = handler_input.attributes_manager.request_attributes.get("_", {}) if handler_input else {}
        msg = data.get(prompts.ERROR_CONFIG, "Une erreur est survenue.")
        return (
            handler_input.response_builder
            .speak(msg)
            .set_should_end_session(True)
            .response
        )


class LocalizationInterceptor(AbstractRequestInterceptor):
    """Sélectionne la locale depuis LANGUAGE_DATA (chargé au cold-start)."""
    def process(self, handler_input):
        try:
            locale = handler_input.request_envelope.request.locale or "en"
        except AttributeError:
            locale = "en"
        if DEBUG:
            logger.debug("Locale: %s", locale)

        short = locale[:2]
        data = dict(LANGUAGE_DATA.get(short, LANGUAGE_DATA.get("en", {})))
        # Override par locale exacte (fr-FR, fr-CA, …) si présente
        if locale in LANGUAGE_DATA:
            data.update(LANGUAGE_DATA[locale])
        handler_input.attributes_manager.request_attributes["_"] = data


# ════════════════════════════════════════════════════════════════════════════
# Skill builder
# ════════════════════════════════════════════════════════════════════════════
sb = SkillBuilder()

sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(VoiceLaunchIntentHandler())
sb.add_request_handler(VoiceTurnOnIntentHandler())
sb.add_request_handler(VoiceTurnOffIntentHandler())
sb.add_request_handler(VoiceSetIntentHandler())
sb.add_request_handler(VoiceQueryIntentHandler())
sb.add_request_handler(DisambiguationIntentHandler())
sb.add_request_handler(YesIntentHandler())
sb.add_request_handler(NoIntentHandler())
sb.add_request_handler(StringIntentHandler())
sb.add_request_handler(SelectIntentHandler())
sb.add_request_handler(NumericIntentHandler())
sb.add_request_handler(DurationIntentHandler())
sb.add_request_handler(DateTimeIntentHandler())
sb.add_request_handler(RepeatIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(FallbackHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_request_handler(IntentReflectorHandler())

sb.add_exception_handler(CatchAllExceptionHandler())
sb.add_global_request_interceptor(LocalizationInterceptor())

lambda_handler = sb.lambda_handler()
