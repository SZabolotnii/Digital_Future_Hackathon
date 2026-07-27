"""L2a — класичний детермінований аналіз на факт-сторі, не LLM.

Дві перевірки над `FactStore`: виявлення стійких трендів показника (монотонне
зростання/спадання) та порушення референсних меж за останнім виміром.
"""
from __future__ import annotations

from typing import Optional

from ..dto import Signal
from ..reference import range_for
from ..stores import FactStore
from ..units import canonical, same_unit


def _fmt(v: float) -> str:
    """Компактне форматування числа (без зайвих `.0`)."""
    return f"{v:g}"


def _keyword_for(loinc: str, fallback: str, lang: str) -> str:
    """Назва показника мовою відповіді: keyword_en з довідника меж, інакше — з факту."""
    if lang == "en":
        ref = range_for(loinc, None)
        if ref is not None and ref.keyword_en:
            return ref.keyword_en
    return fallback


def detect_trends(store: FactStore, lang: str = "uk") -> list[Signal]:
    """Стійкий монотонний тренд (>=3 вимірювань, всі зростають або всі спадають)."""
    signals: list[Signal] = []
    for loinc in store.loincs():
        series = store.series(loinc)
        if len(series) < 3:
            continue
        # Ряд у різних одиницях — не ряд: 100 mg/dL і 5.5 mmol/L «зростають» лише
        # на папері. Тренд рахуємо тільки на однорідних одиницях.
        if len({canonical(f.unit) for f in series}) > 1:
            continue
        values = [f.value for f in series]
        # Нестрога монотонність із чистою зміною: рівний крок (повторне значення)
        # НЕ скасовує очевидний тренд, але суто рівний ряд трендом не вважається.
        rising = all(a <= b for a, b in zip(values, values[1:])) and values[-1] > values[0]
        falling = all(a >= b for a, b in zip(values, values[1:])) and values[-1] < values[0]
        if not (rising or falling):
            continue
        first, last = series[0], series[-1]
        keyword = _keyword_for(loinc, first.keyword, lang)
        way = "rising" if rising else "falling"
        if lang == "en":
            direction = way
            detail = (
                f"{keyword} {direction} {len(series)} measurements in a row: "
                f"{_fmt(first.value)}→{_fmt(last.value)} "
                f"({first.ts.date().isoformat()}..{last.ts.date().isoformat()})"
            )
        else:
            direction = "зростає" if rising else "знижується"
            detail = (
                f"{keyword} {direction} {len(series)} вимірювань поспіль: "
                f"{_fmt(first.value)}→{_fmt(last.value)} "
                f"({first.ts.date().isoformat()}..{last.ts.date().isoformat()})"
            )
        signals.append(
            Signal(
                layer="L2a",
                kind="trend",
                key=f"trend:{loinc}:{way}",
                severity="moderate",
                detail=detail,
                subjects=[loinc],
                confidence=1.0,
            )
        )
    return signals


def detect_range_breaches(
    store: FactStore, sex: Optional[str] = None, lang: str = "uk"
) -> list[Signal]:
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
        # Порівнювати з межею можна лише в тих самих одиницях. Інакше — не мовчимо,
        # а кажемо прямо: перевірку не виконано (мовчазний пропуск гірший за незгоду).
        if not same_unit(latest.unit, ref.unit):
            keyword = (ref.keyword_en if lang == "en" and ref.keyword_en else ref.keyword)
            got = latest.unit or ("not stated" if lang == "en" else "не вказано")
            detail = (
                f"{keyword} {_fmt(latest.value)} {got}: units do not match the reference "
                f"({ref.unit}) — range check not performed"
                if lang == "en" else
                f"{keyword} {_fmt(latest.value)} {got}: одиниці не збігаються з довідником "
                f"({ref.unit}) — перевірку меж не виконано"
            )
            signals.append(
                Signal(
                    layer="L2a",
                    kind="unit_mismatch",
                    key=f"unit_mismatch:{loinc}",
                    severity="moderate",
                    detail=detail,
                    subjects=[loinc],
                    confidence=1.0,
                )
            )
            continue
        if latest.value < ref.low:
            side = "below"
            direction = "below range" if lang == "en" else "нижче норми"
        elif latest.value > ref.high:
            side = "above"
            direction = "above range" if lang == "en" else "вище норми"
        else:
            continue
        unit = latest.unit or ref.unit or ""
        keyword = (ref.keyword_en if lang == "en" and ref.keyword_en else ref.keyword)
        detail = (
            f"{keyword} {_fmt(latest.value)} {unit} {direction} "
            f"({_fmt(ref.low)}–{_fmt(ref.high)})"
        )
        signals.append(
            Signal(
                layer="L2a",
                kind="range_breach",
                key=f"range_breach:{loinc}:{side}",
                severity="major",
                detail=detail,
                subjects=[loinc],
                confidence=1.0,
            )
        )
    return signals


def run(store: FactStore, sex: Optional[str] = None, lang: str = "uk") -> list[Signal]:
    """Усі сигнали L2a: тренди + порушення референсних меж."""
    return detect_trends(store, lang) + detect_range_breaches(store, sex, lang)
