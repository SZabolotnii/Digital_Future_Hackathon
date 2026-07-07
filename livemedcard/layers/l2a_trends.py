"""L2a — класичний детермінований аналіз на факт-сторі, не LLM.

Дві перевірки над `FactStore`: виявлення стійких трендів показника (монотонне
зростання/спадання) та порушення референсних меж за останнім виміром.
"""
from __future__ import annotations

from typing import Optional

from ..dto import Signal
from ..reference import range_for
from ..stores import FactStore


def _fmt(v: float) -> str:
    """Компактне форматування числа (без зайвих `.0`)."""
    return f"{v:g}"


def detect_trends(store: FactStore) -> list[Signal]:
    """Стійкий монотонний тренд (>=3 вимірювань, всі зростають або всі спадають)."""
    signals: list[Signal] = []
    for loinc in store.loincs():
        series = store.series(loinc)
        if len(series) < 3:
            continue
        values = [f.value for f in series]
        # Нестрога монотонність із чистою зміною: рівний крок (повторне значення)
        # НЕ скасовує очевидний тренд, але суто рівний ряд трендом не вважається.
        rising = all(a <= b for a, b in zip(values, values[1:])) and values[-1] > values[0]
        falling = all(a >= b for a, b in zip(values, values[1:])) and values[-1] < values[0]
        if not (rising or falling):
            continue
        direction = "зростає" if rising else "знижується"
        first, last = series[0], series[-1]
        detail = (
            f"{first.keyword} {direction} {len(series)} вимірювань поспіль: "
            f"{_fmt(first.value)}→{_fmt(last.value)} "
            f"({first.ts.date().isoformat()}..{last.ts.date().isoformat()})"
        )
        signals.append(
            Signal(
                layer="L2a",
                kind="trend",
                severity="moderate",
                detail=detail,
                subjects=[loinc],
                confidence=1.0,
            )
        )
    return signals


def detect_range_breaches(store: FactStore, sex: Optional[str] = None) -> list[Signal]:
    """Останнє значення показника поза референсною межею (з урахуванням статі)."""
    signals: list[Signal] = []
    for loinc in store.loincs():
        ref = range_for(loinc, sex)
        if ref is None:
            continue
        series = store.series(loinc)
        if not series:
            continue
        # series відсортований за часом; беремо ОСТАННІЙ (при рівних мітках —
        # останній доданий), щоб L2a і Q&A узгоджено бачили те саме значення.
        latest = series[-1]
        if latest.value < ref.low:
            direction = "нижче норми"
        elif latest.value > ref.high:
            direction = "вище норми"
        else:
            continue
        unit = latest.unit or ref.unit or ""
        detail = (
            f"{ref.keyword} {_fmt(latest.value)} {unit} {direction} "
            f"({_fmt(ref.low)}–{_fmt(ref.high)})"
        )
        signals.append(
            Signal(
                layer="L2a",
                kind="range_breach",
                severity="major",
                detail=detail,
                subjects=[loinc],
                confidence=1.0,
            )
        )
    return signals


def run(store: FactStore, sex: Optional[str] = None) -> list[Signal]:
    """Усі сигнали L2a: тренди + порушення референсних меж."""
    return detect_trends(store) + detect_range_breaches(store, sex)
