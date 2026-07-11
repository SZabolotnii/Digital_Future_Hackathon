"""Тести детермінованого шару TRACE L2a (тренди, порушення референсних меж)."""
from __future__ import annotations

import datetime as dt

from livemedcard.fhir import CodeableConcept, Coding, Observation, Quantity, Sys
from livemedcard.layers.l2a_trends import detect_range_breaches, detect_trends, run
from livemedcard.stores import FactStore


def _obs(
    id: str,
    loinc: str,
    value: float,
    date_str: str,
    keyword: str = "Креатинін",
    unit: str = "µmol/L",
) -> Observation:
    return Observation(
        id=id,
        status="final",
        code=CodeableConcept(
            coding=[Coding(system=Sys.LOINC, code=loinc, display=keyword)],
            text=keyword,
        ),
        effectiveDateTime=dt.datetime.fromisoformat(date_str),
        valueQuantity=Quantity(value=value, unit=unit),
    )


def test_rising_trend_detected():
    store = FactStore()
    store.add_observations(
        [
            _obs("o1", "2160-0", 88, "2023-05-01"),
            _obs("o2", "2160-0", 104, "2023-10-01"),
            _obs("o3", "2160-0", 121, "2024-04-09"),
        ]
    )

    signals = detect_trends(store)
    assert len(signals) == 1
    signal = signals[0]
    assert signal.layer == "L2a"
    assert signal.kind == "trend"
    assert signal.severity == "moderate"
    assert signal.subjects == ["2160-0"]
    assert signal.confidence == 1.0
    assert "зростає" in signal.detail
    assert "88" in signal.detail and "121" in signal.detail


def test_range_breach_detected():
    store = FactStore()
    store.add_observations(
        [
            _obs("o1", "2160-0", 88, "2023-05-01"),
            _obs("o2", "2160-0", 104, "2023-10-01"),
            _obs("o3", "2160-0", 121, "2024-04-09"),
        ]
    )

    signals = detect_range_breaches(store)
    assert len(signals) == 1
    signal = signals[0]
    assert signal.layer == "L2a"
    assert signal.kind == "range_breach"
    assert signal.severity == "major"
    assert signal.subjects == ["2160-0"]
    assert signal.confidence == 1.0
    assert "вище норми" in signal.detail
    assert "121" in signal.detail


def test_run_combines_both_checks():
    store = FactStore()
    store.add_observations(
        [
            _obs("o1", "2160-0", 88, "2023-05-01"),
            _obs("o2", "2160-0", 104, "2023-10-01"),
            _obs("o3", "2160-0", 121, "2024-04-09"),
        ]
    )
    signals = run(store)
    kinds = [s.kind for s in signals]
    assert kinds == ["trend", "range_breach"]


def test_two_points_no_trend():
    store = FactStore()
    store.add_observations(
        [
            _obs("o1", "2823-3", 4.0, "2024-01-01", keyword="Калій", unit="mmol/L"),
            _obs("o2", "2823-3", 4.5, "2024-02-01", keyword="Калій", unit="mmol/L"),
        ]
    )
    assert detect_trends(store) == []


def test_flat_within_range_no_trend_no_breach():
    store = FactStore()
    store.add_observations(
        [
            _obs("o1", "4548-4", 5.0, "2024-01-01", keyword="HbA1c", unit="%"),
            _obs("o2", "4548-4", 5.5, "2024-02-01", keyword="HbA1c", unit="%"),
            _obs("o3", "4548-4", 5.0, "2024-03-01", keyword="HbA1c", unit="%"),
        ]
    )
    assert detect_trends(store) == []
    assert detect_range_breaches(store) == []


def test_rising_trend_english_detail():
    store = FactStore()
    store.add_observations(
        [
            _obs("o1", "2160-0", 88, "2023-05-01"),
            _obs("o2", "2160-0", 104, "2023-10-01"),
            _obs("o3", "2160-0", 121, "2024-04-09"),
        ]
    )
    signals = detect_trends(store, lang="en")
    assert len(signals) == 1
    assert "Creatinine rising 3 measurements in a row" in signals[0].detail


def test_range_breach_english_detail():
    store = FactStore()
    store.add_observations([_obs("o1", "2160-0", 121, "2023-05-01")])
    signals = detect_range_breaches(store, lang="en")
    assert len(signals) == 1
    assert "Creatinine 121 µmol/L above range" in signals[0].detail
