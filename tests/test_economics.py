"""Тести економіки рішень: поріг ескалації як відношення вартостей.

Головне, що тут фіксується: поріг НЕ підбирається руками. Він падає з формули
c* = 1 - C_human/(π·C_harm), і якщо вартості змінити — поріг зміниться слідом.
"""
from __future__ import annotations

import pytest

from livemedcard import economics


def test_threshold_follows_the_cost_ratio():
    """Дорожча шкода → вища планка довіри. Дешевша перевірка → теж вища."""
    # дефолти: C_human=5, π=0.01, C_harm=15000 → 1 - 5/150 = 0.9667
    assert economics.escalation_threshold("medication") == pytest.approx(0.967, abs=0.001)
    # π=0.05, C_harm=500 → 1 - 5/25 = 0.8
    assert economics.escalation_threshold("measurement") == pytest.approx(0.8, abs=0.001)


def test_threshold_is_derived_not_tuned(monkeypatch):
    """Зміна вартості шкоди зсуває поріг — це і є доказ, що він виведений."""
    monkeypatch.setattr(economics, "_PARAMS", {**economics._PARAMS,
                                               "medication": (0.01, 30_000.0)})
    # 1 - 5/(0.01*30000=300) = 0.9833 → удвічі дорожча шкода піднімає планку
    assert economics.escalation_threshold("medication") == pytest.approx(0.983, abs=0.001)


def test_threshold_never_degenerates(monkeypatch):
    """Поріг 1.0 = «ніколи не довіряти», 0.0 = «ніколи не перевіряти». Обидва
    роблять L3 беззмістовним, тож затиснуто в межі."""
    monkeypatch.setattr(economics, "_PARAMS", {**economics._PARAMS,
                                               "medication": (1.0, 10**9)})
    assert economics.escalation_threshold("medication") <= 0.99
    monkeypatch.setattr(economics, "_PARAMS", {**economics._PARAMS,
                                               "medication": (0.0, 0.0)})
    assert economics.escalation_threshold("medication") >= 0.05


def test_expected_cost_escalation_buys_down_risk():
    """Ескалація коштує C_human; автоматична обробка — очікуваної шкоди.
    Коли витяг невпевнений, перевірка людиною дешевша за ризик — саме тому
    політика й ескалює."""
    unverified = 0.5
    auto = economics.expected_cost(unverified, "medication", escalated=False)
    human = economics.expected_cost(unverified, "medication", escalated=True)
    assert auto == pytest.approx(0.5 * 0.01 * 15000)   # = 75
    assert human == 5.0
    assert human < auto                                 # перевірка вигідніша за ризик

    # А на впевненому витягу показників — навпаки: ескалювати дорожче, ніж ризик
    confident = 0.95
    assert economics.expected_cost(confident, "measurement", escalated=False) < 5.0


def test_hazard_is_read_from_what_was_extracted():
    assert economics.hazard_of(has_medications=True, has_observations=True) == "medication"
    assert economics.hazard_of(has_medications=False, has_observations=True) == "measurement"
    assert economics.hazard_of(has_medications=False, has_observations=False) == "none"


def test_override_wins_and_is_reported(monkeypatch):
    """Адміністративний override лишається можливим — але /metrics мусить сказати,
    що поріг заданий рукою, а не виведений."""
    monkeypatch.setattr(economics, "LOW_CONFIDENCE_OVERRIDE", 0.6)
    assert economics.escalation_threshold("medication") == 0.6
    assert economics.summary()["threshold_source"] == "override"
    monkeypatch.setattr(economics, "LOW_CONFIDENCE_OVERRIDE", None)
    assert economics.summary()["threshold_source"] == "derived"
