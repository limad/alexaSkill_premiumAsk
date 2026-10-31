# -*- coding: utf-8 -*-
"""
Affichage APL (Echo Show, Echo Spot, Fire TV) : le texte de chaque réponse
parlée est aussi affiché à l'écran, sous le nom du skill.

Un seul point d'entrée : AplResponseInterceptor, branché en intercepteur de
réponse global ; les handlers n'ont rien à faire.
"""
import html
import re

from ask_sdk_core.dispatch_components import AbstractResponseInterceptor
from ask_sdk_model.interfaces.alexa.presentation.apl import RenderDocumentDirective

TOKEN = "jeedomReply"
TITLE = "premiumAsk"  # nom du skill (fiche identique dans toutes les langues)

DOCUMENT = {
    "type": "APL",
    "version": "2023.3",
    "theme": "dark",
    "mainTemplate": {
        "parameters": ["payload"],
        "items": [{
            "type": "Container",
            "width": "100vw",
            "height": "100vh",
            "justifyContent": "center",
            "paddingLeft": "${viewport.shape == 'round' ? '12vw' : '6vw'}",
            "paddingRight": "${viewport.shape == 'round' ? '12vw' : '6vw'}",
            "items": [
                {
                    "type": "Text",
                    "text": "${payload.reply.title}",
                    "color": "#9fb3c8",
                    "fontSize": "${viewport.width > 700 ? '28dp' : '20dp'}",
                    "maxLines": 2,
                    "textAlign": "${viewport.shape == 'round' ? 'center' : 'left'}",
                    "paddingBottom": "3vh",
                },
                {
                    "type": "Text",
                    "text": "${payload.reply.text}",
                    "color": "#ffffff",
                    "fontSize": "${viewport.width > 700 ? '44dp' : '28dp'}",
                    "fontWeight": "300",
                    "maxLines": 8,
                    "textAlign": "${viewport.shape == 'round' ? 'center' : 'left'}",
                },
            ],
        }],
    },
}


def supports_apl(handler_input) -> bool:
    """L'appareil qui a parlé a-t-il un écran APL ?"""
    try:
        interfaces = handler_input.request_envelope.context.system.device.supported_interfaces
        return getattr(interfaces, "alexa_presentation_apl", None) is not None
    except AttributeError:
        return False


def speech_text(response) -> str:
    """Texte brut de la réponse parlée (SSML retiré)."""
    speech = getattr(response, "output_speech", None)
    raw = (getattr(speech, "ssml", None) or getattr(speech, "text", None) or "") if speech else ""
    if not isinstance(raw, str):
        return ""
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))).strip()


class AplResponseInterceptor(AbstractResponseInterceptor):
    """Ajoute la carte APL à toute réponse parlée sur un appareil à écran."""

    def process(self, handler_input, response):
        if response is None or not supports_apl(handler_input):
            return
        text = speech_text(response)
        if not text:
            return
        directives = list(getattr(response, "directives", None) or [])
        if any(isinstance(d, RenderDocumentDirective) for d in directives):
            return
        directives.append(RenderDocumentDirective(
            token=TOKEN,
            document=DOCUMENT,
            datasources={"reply": {"title": TITLE, "text": text}},
        ))
        response.directives = directives
