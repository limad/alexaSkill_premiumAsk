# Schemas d'état question/réponse — dataclasses stdlib (plus de dépendance pydantic).
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class QuestionStateError:
    text: str
    error: bool = True


@dataclass
class QuestionState:
    text: str
    event_id: Optional[str] = None
    suppress_confirmation: bool = False
    deviceSerialNumber: Optional[str] = None
    textBrut: str = ""
    error: bool = False
