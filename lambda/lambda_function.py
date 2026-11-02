# VERSION 0.14.0
# Skill de questions/réponses seulement : un scénario Jeedom pose une question,
# la réponse (oui/non, nombre, durée, date, choix, texte) lui est renvoyée.
# Les commandes vocales libres sont prises en charge par le skill Frida.

CODE_VERS = 0.4
TOKEN = ""          # Token account-linking longue durée (laisser vide pour récupération auto)

# ─── Built-in imports ───────────────────────────────────────────────────────
import json
import logging
import os
import time
from typing import Optional, Union
from xml.sax.saxutils import escape

# ─── 3rd-party imports ──────────────────────────────────────────────────────
import re
import urllib3
from urllib3 import HTTPResponse
from urllib3.util.retry import Retry


# Parseur ISO-8601 duration (remplace isodate) — couvre PT#H#M#S, PxxY/M/W/D, combiné,
# avec décimales (« une demi-heure » peut arriver en PT0.5H).
_N = r"\d+(?:[.,]\d+)?"
_ISO_DUR_RE = re.compile(
    rf"^P(?:(?P<y>{_N})Y)?(?:(?P<mo>{_N})M)?(?:(?P<w>{_N})W)?(?:(?P<d>{_N})D)?"
    rf"(?:T(?:(?P<h>{_N})H)?(?:(?P<m>{_N})M)?(?:(?P<s>{_N})S)?)?$"
)


def _parse_iso_duration_seconds(duration: Optional[str]) -> Optional[float]:
    """Parse un ISO-8601 duration en secondes (années=365j, mois=30j — approximation).
    None si la valeur est absente ou illisible : la question est alors reposée."""
    m = _ISO_DUR_RE.match(duration or "")
    if not duration or not m or duration in ("P", "PT"):
        return None
    g = {k: float(v.replace(",", ".")) if v else 0.0 for k, v in m.groupdict().items()}
    total = 0.0
    total += g["y"] * 365 * 86400
    total += g["mo"] * 30 * 86400
    total += g["w"] * 7 * 86400
    total += g["d"] * 86400
    total += g["h"] * 3600
    total += g["m"] * 60
    total += g["s"]
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
import apl
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

# ─── Réglages (config.py) ───────────────────────────────────────────────────
def load_config(path):
    """Lit config.py sans jamais lever d'exception : une faute de frappe laisse les
    réglages vides, et le skill répond son message d'erreur au lieu de planter."""
    # Lu et exécuté directement (pas d'import) : aucun cache __pycache__ qui pourrait
    # resservir une ancienne version du fichier réécrit dans la même seconde.
    try:
        with open(path, encoding="utf-8") as f:
            values = {}
            exec(compile(f.read(), path, "exec"), values)
        return {k: v for k, v in values.items() if k.isupper()}
    except Exception as exc:  # SyntaxError, fichier absent…
        logging.getLogger(__name__).error("config.py illisible : %s", exc)
        return {}


CONFIG = load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"))
JEEDOM_URL = str(CONFIG.get("JEEDOM_URL") or "").rstrip("/")
# APIKEY : nom écrit par les versions du plugin antérieures à 11/2026
APIKEY = str(CONFIG.get("JEEDOM_APIKEY") or CONFIG.get("APIKEY") or "")
VERIFY_SSL = bool(CONFIG.get("VERIFY_SSL", True))
DEBUG = bool(CONFIG.get("DEBUG", False))

# ─── URLs pré-calculées ─────────────────────────────────────────────────────
QUESTION_URL = f"{JEEDOM_URL}/plugins/alexaapiv2/core/php/askQuestion.php"
RESPONSE_URL = f"{JEEDOM_URL}/plugins/alexaapiv2/core/php/askResponse.php?command=reponseASK"
LOG_URL      = f"{JEEDOM_URL}/plugins/alexaapiv2/core/php/askResponse.php?command=log"
# APIKEY est passée en header Authorization (cf. _get_headers ci-dessous), plus en query string

# ─── Logging ────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

# ─── HTTP pool GLOBAL (réutilisé entre invocations chaudes — évite handshake TLS) ──
# Timeouts courts : Alexa coupe à 8 s, mieux vaut échouer vite sur réseau lent.
# Retry : 1 essai supplémentaire sur 5xx (reload plugin Jeedom temporaire), GET seulement.
# Jamais de relance d'un POST : Jeedom a pu déjà exécuter la commande (un LLM à 4 s
# dépasse le read timeout de 3 s → la même phrase repartait une 2e fois).
# Compat urllib3 : `allowed_methods` (≥1.26) / `method_whitelist` (1.25.x).
# Relance sur code 5xx seulement (connect=0, read=0) : un délai dépassé n'est jamais
# relancé, sinon il compterait deux fois dans le budget de la requête.
_RETRY_KW = dict(
    total=1,
    connect=0,
    read=0,
    backoff_factor=0.3,
    status_forcelist=(500, 502, 503, 504),
)
try:
    _RETRY = Retry(allowed_methods=frozenset(["GET"]), **_RETRY_KW)
except TypeError:
    _RETRY = Retry(method_whitelist=frozenset(["GET"]), **_RETRY_KW)

# Budget total d'une requête Alexa (lecture de la question + envoi de la réponse) :
# Alexa coupe à 8 s, on garde une marge pour construire la réponse.
REQUEST_BUDGET = 7.0
GET_CAP = 3.0       # lecture de alexaAskQuestion.php (rapide : un fichier)
LOG_CAP = 1.0       # journal best-effort, jamais aux dépens de la réponse
MIN_LEFT = 0.5      # en dessous, on ne tente plus d'appel

HTTP = urllib3.PoolManager(
    cert_reqs="CERT_REQUIRED" if VERIFY_SSL else "CERT_NONE",
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


def _ssml(text) -> str:
    """Texte venu de Jeedom ou de l'utilisateur, prêt pour <speak> : &, < et > en entités."""
    return escape(str(text or ""))


def _no_question_response(handler_input, jee):
    """
    Réponse quand aucune question Jeedom n'attend : le texte d'erreur si Jeedom
    n'a pas pu être lu, sinon « aucune question en attente » (ce skill ne prend
    pas de commande libre : c'est le rôle du skill Frida). Ferme la session.
    """
    data = handler_input.attributes_manager.request_attributes.get("_", {})
    if isinstance(jee.jee_state, QuestionStateError) and jee.jee_state.text:
        text = jee.jee_state.text
    else:
        text = data.get(prompts.NO_QUESTION, "Aucune question en attente.")
    return handler_input.response_builder.speak(text).set_should_end_session(True).response


def _qa_guard_no_question(handler_input, jee, label: str):
    """
    Les intents de réponse n'ont de sens que si Jeedom a posé une question.
    Retourne la Response « aucune question » hors Q/A, sinon None (flow normal).
    """
    if isinstance(jee.jee_state, QuestionState):
        return None
    logger.info("%s sans question en attente", label)
    return _no_question_response(handler_input, jee)


def _reask(handler_input, jee, label: str):
    """
    Réponse incomprise (slot vide, valeur illisible, intention inconnue) : on repose la
    question au lieu d'envoyer « pas de réponse » au scénario. Si l'utilisateur ne
    répond toujours pas, Alexa relance puis envoie SessionEndedRequest
    (EXCEEDED_MAX_REPROMPTS) et le scénario reçoit alors « pas de réponse ».
    """
    if not isinstance(jee.jee_state, QuestionState) or not jee.jee_state.text:
        return _no_question_response(handler_input, jee)
    logger.info("%s incompris : question reposée", label)
    data = handler_input.attributes_manager.request_attributes.get("_", {})
    template = data.get(prompts.ERROR_ACOUSTIC, "{}")
    builder = (handler_input.response_builder
               .speak(template.format(jee.jee_state.text))
               .ask(jee.jee_state.text)
               .set_should_end_session(False))
    _add_hint(builder, data.get(prompts.HINT_TEXT, "dire arrête"))
    return builder.response


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
        self._deadline = time.monotonic() + REQUEST_BUDGET

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
        if st != 401:  # clé refusée : le journal serait refusé lui aussi
            self.post_jee_log(speak)
        return speak

    def _left(self) -> float:
        return self._deadline - time.monotonic()

    def _timeout(self, cap: Optional[float] = None) -> Optional[urllib3.Timeout]:
        """Délai total (connexion comprise) pris sur le budget restant, None s'il est épuisé."""
        left = self._left()
        if cap is not None:
            left = min(left, cap)
        if left < MIN_LEFT:
            return None
        return urllib3.Timeout(total=left, connect=min(2.0, left / 2.0))

    def _request(self, method: str, url: str, *, body: Optional[dict] = None, extra_headers: Optional[dict] = None,
                 cap: Optional[float] = None):
        """Exécute une requête HTTP dans le budget restant (relance 5xx héritée du pool)."""
        headers = self._get_headers()
        if extra_headers:
            headers.update(extra_headers)  # in-place, NE PAS réassigner (dict.update() renvoie None)
        timeout = self._timeout(cap)
        if timeout is None:
            logger.error("Jeedom HTTP : budget de %.1f s épuisé avant %s", REQUEST_BUDGET, method)
            self.jee_state = QuestionStateError(text=self.language_strings.get(prompts.ERROR_CONFIG, "Jeedom error"))
            return None
        try:
            kwargs = {"headers": headers, "timeout": timeout}
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
        response = self._request("GET", QUESTION_URL, cap=GET_CAP)
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
            return  # jee_state reste None : « aucune question en attente »

        try:
            state = json.loads(state_str)
        except json.JSONDecodeError:
            logger.error("alexaAsk.json.state mal formé")
            self._set_jee_error(prompts.ERROR_CONFIG)
            return

        text = state.get("text") or ""
        if not text:
            logger.info("Question Jeedom sans texte")
            return

        self.jee_state = QuestionState(
            event_id=state.get("event"),
            suppress_confirmation=_string_to_bool(state.get("suppress_confirmation")),
            text=_ssml(text),  # dit tel quel dans <speak> : « Volets & lumières ? » sinon rejeté
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
        # askResponse.php attend jusqu'à 2,5 s une question suivante avant de répondre :
        # le POST reçoit tout le budget restant (Alexa coupe à 8 s).
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
                text=_ssml(next_q["text"]),
                deviceSerialNumber=next_q.get("deviceSerialNumber"),
                textBrut=next_q.get("textBrut") or "",
            )
            # Retourne la question elle-même comme "réponse" — le handler la speak
            return self.jee_state.text

        # Pas de question suivante : flow standard
        if not suppress:
            self.jee_state = None
            return self.language_strings.get(prompts.OKAY, "Ok")

        self.jee_state = None
        return ""

    # ── log push vers Jeedom ────────────────────────────────────────────────
    def post_jee_log(self, log_text: str, **kwargs):
        """Envoie un log au plugin côté Jeedom (askResponse.php?command=log). Best-effort."""
        timeout = self._timeout(LOG_CAP)
        if timeout is None or self._left() < LOG_CAP + 2 * MIN_LEFT:
            return  # pas le temps : la réponse à l'utilisateur passe avant
        body = {"log": log_text, "code_version": CODE_VERS}
        body.update(kwargs)
        try:
            HTTP.request(
                "POST", LOG_URL,
                headers=self._get_headers(),
                body=json.dumps(body).encode("utf-8"),
                timeout=timeout,
                retries=False,
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
    Le plugin ouvre le skill quand un scénario pose une question (alexaAsk.json) :
    on la prononce et on attend la réponse. Sans question en attente, on le dit
    et on ferme la session.
    """
    def can_handle(self, handler_input):
        return is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        jee = JeeAsk(handler_input)
        if isinstance(jee.jee_state, QuestionState) and jee.jee_state.text:
            data = handler_input.attributes_manager.request_attributes.get("_", {})
            builder = handler_input.response_builder.speak(jee.jee_state.text)
            if jee.jee_state.event_id:
                builder.ask("")
            _add_hint(builder, data.get(prompts.HINT_TEXT, "dire arrête"))
            return builder.response
        return _no_question_response(handler_input, jee)


class YesIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.YesIntent")(handler_input)

    def handle(self, handler_input):
        logger.info("Yes Intent")
        jee = JeeAsk(handler_input)
        guard = _qa_guard_no_question(handler_input, jee, "Yes")
        if guard is not None:
            return guard
        return _handle_qa_response(handler_input, jee, jee.post_jee_event(RESPONSE_YES, RESPONSE_YES))


class NoIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.NoIntent")(handler_input)

    def handle(self, handler_input):
        logger.info("No Intent")
        jee = JeeAsk(handler_input)
        guard = _qa_guard_no_question(handler_input, jee, "No")
        if guard is not None:
            return guard
        return _handle_qa_response(handler_input, jee, jee.post_jee_event(RESPONSE_NO, RESPONSE_NO))


class NumericIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("Number")(handler_input)

    def handle(self, handler_input):
        logger.info("Numeric Intent")
        jee = JeeAsk(handler_input)
        guard = _qa_guard_no_question(handler_input, jee, "Numeric")
        if guard is not None:
            return guard
        # « douze » (Numbers) ou « le deuxième » (Ordinal, AMAZON.Ordinal → "2")
        number = get_slot_value(handler_input, "Numbers") or get_slot_value(handler_input, "Ordinal")
        if number in (None, "", "?"):
            return _reask(handler_input, jee, "Numeric")
        return _handle_qa_response(handler_input, jee, jee.post_jee_event(number, RESPONSE_NUMERIC))


class StringIntentHandler(AbstractRequestHandler):
    """Réponse libre (« rouge », « le code est 1234 »…)."""
    def can_handle(self, handler_input):
        return is_intent_name("String")(handler_input)

    def handle(self, handler_input):
        logger.info("String Intent")
        jee = JeeAsk(handler_input)
        guard = _qa_guard_no_question(handler_input, jee, "String")
        if guard is not None:
            return guard
        strings = (get_slot_value(handler_input, "Strings") or "").strip()
        if not strings:
            return _reask(handler_input, jee, "String")
        return _handle_qa_response(handler_input, jee, jee.post_jee_event(strings, RESPONSE_STRING))


class SelectIntentHandler(AbstractRequestHandler):
    """Choix parmi des propositions (« le salon », « je choisis la cuisine »)."""
    def can_handle(self, handler_input):
        return is_intent_name("Select")(handler_input)

    def handle(self, handler_input):
        logger.info("Select Intent")
        jee = JeeAsk(handler_input)
        guard = _qa_guard_no_question(handler_input, jee, "Select")
        if guard is not None:
            return guard
        data = handler_input.attributes_manager.request_attributes.get("_", {})
        selection = jee.get_value_for_slot("Selections") or get_slot_value(handler_input, "Selections")
        if not selection:
            return _reask(handler_input, jee, "Select")

        suppress = jee.jee_state.suppress_confirmation
        spoken = jee.post_jee_event(selection, RESPONSE_SELECT)
        if isinstance(jee.jee_state, QuestionStateError):
            # Envoi refusé ou Jeedom injoignable : on le dit, jamais « vous avez choisi ».
            return _handle_response(handler_input, spoken)
        confirmation = "" if suppress else data.get(prompts.SELECTED, "{}").format(_ssml(selection))
        # Nouvelle question enchaînée par le scénario (multi-tour)
        if isinstance(jee.jee_state, QuestionState) and jee.jee_state.text:
            return (handler_input.response_builder
                    .speak((confirmation + " " + jee.jee_state.text).strip())
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
        seconds = _parse_iso_duration_seconds(get_slot_value(handler_input, "Durations"))
        if seconds is None:
            return _reask(handler_input, jee, "Duration")
        speak_output = jee.post_jee_event(seconds, RESPONSE_DURATION)
        return _handle_qa_response(handler_input, jee, speak_output)


class DateTimeIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("Date")(handler_input)

    def handle(self, handler_input):
        logger.info("Date/Time Intent")
        date = get_slot_value(handler_input, "Dates")
        time = get_slot_value(handler_input, "Times")

        jee = JeeAsk(handler_input)
        guard = _qa_guard_no_question(handler_input, jee, "Date")
        if guard is not None:
            return guard
        if not date and not time:
            return _reask(handler_input, jee, "Date")
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

    # AMAZON.TIME : « hh:mm » ou un moment de la journée (matin, après-midi, soir, nuit).
    TIME_PERIODS = ("MO", "AF", "EV", "NI")

    @staticmethod
    def _parse_time(time: Optional[str]) -> dict:
        out = {"seconds": None, "minute": None, "hour": None}
        if not time:
            return out
        if time.upper() in DateTimeIntentHandler.TIME_PERIODS:
            out["period"] = time.upper()  # pas d'heure précise : le scénario choisit
            return out
        parts = time.split(":")
        n = len(parts)
        out["hour"]    = parts[0] if n >= 1 else None
        out["minute"]  = parts[1] if n >= 2 else None
        out["seconds"] = parts[2] if n >= 3 else None
        return out


class RepeatIntentHandler(AbstractRequestHandler):
    """User dit 'répète' / 'redis' / 'pardon' → on reprononce la dernière question Ask.
    Sans question en attente, le dit et ferme la session."""
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.RepeatIntent")(handler_input)

    def handle(self, handler_input):
        logger.info("Repeat Intent")
        jee = JeeAsk(handler_input)  # re-fetch alexaAsk.json
        if isinstance(jee.jee_state, QuestionState) and jee.jee_state.text:
            return handler_input.response_builder.speak(jee.jee_state.text).ask("").response
        return _no_question_response(handler_input, jee)


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
    """« Stop », « annule », « accueil » : ferme la session. Une question en attente reçoit
    « pas de réponse » ici : quand le skill ferme lui-même la session, Alexa n'envoie pas
    de SessionEndedRequest."""
    INTENTS = ("AMAZON.CancelIntent", "AMAZON.StopIntent", "AMAZON.NavigateHomeIntent")

    def can_handle(self, handler_input):
        return any(is_intent_name(name)(handler_input) for name in self.INTENTS)

    def handle(self, handler_input):
        logger.info("Cancel/Stop Intent")
        jee = JeeAsk(handler_input)
        if isinstance(jee.jee_state, QuestionState):
            jee.post_jee_event(RESPONSE_NONE, RESPONSE_NONE)
        data = handler_input.attributes_manager.request_attributes.get("_", {})
        return (handler_input.response_builder
                .speak(data.get(prompts.STOP_MESSAGE, "Au revoir"))
                .set_should_end_session(True)
                .response)


class FallbackHandler(AbstractRequestHandler):
    """AMAZON.FallbackIntent, et toute intention sans handler (enregistré en dernier) :
    la question en attente est reposée, rien n'est envoyé au scénario."""
    def can_handle(self, handler_input):
        return is_request_type("IntentRequest")(handler_input)

    def handle(self, handler_input):
        logger.info("Fallback (%s)", get_intent_name(handler_input))
        return _reask(handler_input, JeeAsk(handler_input), "Fallback")


class SessionEndedRequestHandler(AbstractRequestHandler):
    """N'appelle PAS askQuestion.php — la session est finie, plus besoin de récupérer une question."""
    def can_handle(self, handler_input):
        return is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        logger.info("Session Ended")
        reason = handler_input.request_envelope.request.reason
        if reason in (SessionEndedReason.EXCEEDED_MAX_REPROMPTS, SessionEndedReason.USER_INITIATED):
            # Recharge la question en attente : sans elle, rien n'est envoyé et le
            # scénario ne reçoit jamais « pas de réponse » (comportement de l'original).
            jee = JeeAsk(handler_input)
            if isinstance(jee.jee_state, QuestionState):
                jee.post_jee_event(RESPONSE_NONE, RESPONSE_NONE)
        return handler_input.response_builder.response


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
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_request_handler(FallbackHandler())  # en dernier : toute intention restante

sb.add_exception_handler(CatchAllExceptionHandler())
sb.add_global_request_interceptor(LocalizationInterceptor())
sb.add_global_response_interceptor(apl.AplResponseInterceptor())

lambda_handler = sb.lambda_handler()
