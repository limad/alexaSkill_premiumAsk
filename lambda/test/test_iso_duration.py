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
    import lambda_function  # SDK remplacé par les stubs de conftest.py en CI
    return lambda_function._parse_iso_duration_seconds


@pytest.mark.parametrize("inp,expected", [
    (None, None),           # absente ou illisible : question reposée
    ("", None),
    ("garbage", None),
    ("P", None),
    ("PT", None),
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
    ("P2W", 14 * 86400.0),          # « deux semaines » (donnait 0)
    ("P1W2D", 9 * 86400.0),
])
def test_parse_iso_duration(parse_dur, inp, expected):
    assert parse_dur(inp) == expected


def test_parse_with_decimal_seconds(parse_dur):
    assert parse_dur("PT1.5S") == 1.5


@pytest.mark.parametrize("inp,expected", [
    ("PT0.5H", 1800.0),     # « une demi-heure » (donnait 0)
    ("PT1,5H", 5400.0),
    ("P0.5D", 43200.0),
    ("PT2.5M", 150.0),
])
def test_parse_decimal_units(parse_dur, inp, expected):
    assert parse_dur(inp) == expected


def test_year_month_approximation(parse_dur):
    # Approximation documentée : Y=365j, M=30j
    assert parse_dur("P1Y") == 365 * 86400.0
    assert parse_dur("P1M") == 30 * 86400.0
