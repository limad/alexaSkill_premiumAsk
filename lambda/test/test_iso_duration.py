"""
Tests du parser ISO-8601 duration (_parse_iso_duration_seconds).

Le parser remplace isodate (3rd-party) par une regex stdlib pour rester
sur le runtime Lambda Alexa-hosted (pas de pip install possible runtime).
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def parse_dur():
    """
    Charge _parse_iso_duration_seconds depuis lambda_function.py SANS exécuter
    les imports ask_sdk_core (lourd + non installé en CI).
    Astuce : on lit le fichier, on extrait la fonction + sa regex en isolation.
    """
    src = (Path(__file__).parent.parent / "lambda_function.py").read_text(encoding="utf-8")
    # Récupère le bloc « _ISO_DUR_RE = ... » + « def _parse_iso_duration_seconds »
    import re
    m = re.search(
        r"(_ISO_DUR_RE = re\.compile\([^)]+\)[^\n]*(?:\n[^\n]+)*?)\n\n",
        src, re.DOTALL,
    )
    assert m, "Bloc _ISO_DUR_RE introuvable dans lambda_function.py"
    regex_block = m.group(1)
    fn_match = re.search(
        r"def _parse_iso_duration_seconds\(.*?\n    return total\n",
        src, re.DOTALL,
    )
    assert fn_match, "Fonction _parse_iso_duration_seconds introuvable"

    ns: dict = {}
    exec("import re\nfrom typing import Optional\n" + regex_block + "\n" + fn_match.group(0), ns)
    return ns["_parse_iso_duration_seconds"]


@pytest.mark.parametrize("inp,expected", [
    (None, 0.0),
    ("", 0.0),
    ("garbage", 0.0),
    ("PT0S", 0.0),
    ("PT5S", 5.0),
    ("PT30S", 30.0),
    ("PT1M", 60.0),
    ("PT1M30S", 90.0),
    ("PT1H", 3600.0),
    ("PT1H30M", 5400.0),
    ("PT2H30M15S", 9015.0),
    ("P1D", 86400.0),
    ("P1DT12H", 86400.0 + 12 * 3600),
    ("P7D", 7 * 86400.0),
])
def test_parse_iso_duration(parse_dur, inp, expected):
    assert parse_dur(inp) == expected


def test_parse_with_decimal_seconds(parse_dur):
    assert parse_dur("PT1.5S") == 1.5


def test_year_month_approximation(parse_dur):
    # Approximation documentée : Y=365j, M=30j
    assert parse_dur("P1Y") == 365 * 86400.0
    assert parse_dur("P1M") == 30 * 86400.0
