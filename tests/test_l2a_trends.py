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


def test_range_breach_suppressed_when_units_disagree():
    """Глюкоза 100 mg/dL проти меж 3.9–6.1 mmol/L — це НЕ «вище норми», а неспівставні
    одиниці. Замість хибного сигналу L2a має сказати прямо, що перевірку не виконано."""
    store = FactStore()
    store.add_observation(
        _obs("g1", "2345-7", 100.0, "2024-10-15", keyword="Глюкоза", unit="mg/dL")
    )
    signals = detect_range_breaches(store, "female")
    kinds = [s.kind for s in signals]
    assert "range_breach" not in kinds
    assert kinds == ["unit_mismatch"]
    assert "одиниці не збігаються" in signals[0].detail


def test_range_breach_still_fires_on_matching_units():
    store = FactStore()
    store.add_observation(
        _obs("g2", "2345-7", 9.1, "2024-10-15", keyword="Глюкоза", unit="mmol/L")
    )
    signals = detect_range_breaches(store, "female")
    assert [s.kind for s in signals] == ["range_breach"]


def test_micro_sign_and_cyrillic_units_are_the_same_unit():
    """µmol/L (micro sign), μmol/L (грецька мю) і «мкмоль/л» — одна одиниця."""
    store = FactStore()
    store.add_observation(_obs("c1", "2160-0", 135.0, "2024-10-15", unit="мкмоль/л"))
    assert [s.kind for s in detect_range_breaches(store, "female")] == ["range_breach"]


def test_trend_not_computed_across_mixed_units():
    """Ряд у різних одиницях — не ряд: 5.5 mmol/L → 100 mg/dL «зростає» лише на папері."""
    store = FactStore()
    store.add_observation(_obs("m1", "2345-7", 5.5, "2024-01-01", unit="mmol/L"))
    store.add_observation(_obs("m2", "2345-7", 5.8, "2024-02-01", unit="mmol/L"))
    store.add_observation(_obs("m3", "2345-7", 100.0, "2024-03-01", unit="mg/dL"))
    assert detect_trends(store) == []
