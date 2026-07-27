"""L2a — детерміновані сигнали над темпоральною проєкцією картки.

Три речі, які видно лише в часі й не видно у зрізі «останнє значення»:

1. **Поліфармація** — скільки препаратів приймається ОДНОЧАСНО. Кожен окремо
   може бути доречним; проблему створює їхня кількість разом.
2. **Розрив моніторингу** — показник поза межею, який давно не перемірювали.
   Для ніші Aging & Longevity це, мабуть, найцінніший темпоральний сигнал: не
   «щось не так», а «щось не так, і про це забули».
3. **Ненадійність таймлайну** — коли більшість фактів датовано днем завантаження,
   а не документом, тренди й розриви рахувати нема на чому. Система каже це
   вголос замість того, щоб малювати впевнений графік на вигаданих датах.

Межа детермінізму: сигнали рахуються з фактів і FHIR-ресурсів, а не обходом
графа генеративною моделлю. `temporal.py` лишається описовим і `Signal` не
імпортує взагалі.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..config import (
    DATED_FRACTION_FLOOR,
    MONITORING_GAP_DAYS,
    POLYPHARMACY_THRESHOLD,
)
from ..dto import Signal
from ..reference import range_for
from ..stores import FactStore
from ..temporal import TemporalGraph
from ..units import same_unit

__all__ = ["run", "detect_polypharmacy", "detect_monitoring_gaps", "detect_timeline_uncertainty"]


def _aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def detect_polypharmacy(
    graph: TemporalGraph,
    lang: str = "uk",
    threshold: int = POLYPHARMACY_THRESHOLD,
) -> list[Signal]:
    """≥N препаратів, активних одночасно.

    Рахуємо максимальний перетин інтервалів активності, а не довжину списку:
    п'ять призначень за три роки — не поліфармація, п'ять одночасних — так.
    """
    metrics = graph.metrics()
    n = metrics.max_concurrent_medications
    if n < threshold:
        return []
    active = graph.concurrent_medications()
    names = sorted({e.label for e in active}) or sorted(
        {e.label for e in graph.of_kind("drug")}
    )
    listed = ", ".join(names)
    detail = (
        f"{n} medications active at the same time (threshold {threshold}): {listed}. "
        f"Each may be appropriate on its own — the count together is what warrants review."
        if lang == "en"
        else
        f"{n} препаратів приймаються одночасно (поріг {threshold}): {listed}. "
        f"Кожен окремо може бути доречним — перегляду потребує саме їхня кількість разом."
    )
    return [
        Signal(
            layer="L2a",
            kind="polypharmacy",
            key="polypharmacy",
            severity="moderate",
            detail=detail,
            subjects=[e.key for e in active] or [e.key for e in graph.of_kind("drug")],
            confidence=1.0,
        )
    ]


def detect_monitoring_gaps(
    store: FactStore,
    graph: TemporalGraph,
    sex: Optional[str] = None,
    lang: str = "uk",
    *,
    now: Optional[datetime] = None,
    gap_days: float = MONITORING_GAP_DAYS,
) -> list[Signal]:
    """Показник поза референсною межею, не переміряний довше за `gap_days`.

    Точка відліку — горизонт картки (найсвіжіше свідчення), а не wall-clock:
    картка знає лише те, що в неї подали, тож «застарілість» вимірюється
    відносно найновішого, що вона бачила. `now=` задає відлік явно.
    """
    reference = _aware(now) if now is not None else graph.horizon
    if reference is None:
        return []
    signals: list[Signal] = []
    for loinc in store.loincs():
        ref = range_for(loinc, sex)
        series = store.series(loinc)
        if ref is None or not series:
            continue
        latest = series[-1]
        # Порівнювати з межею можна лише в тих самих одиницях (як у range_breach).
        if not same_unit(latest.unit, ref.unit):
            continue
        if ref.low <= latest.value <= ref.high:
            continue
        age_days = (reference - _aware(latest.ts)).total_seconds() / 86400
        if age_days < gap_days:
            continue
        keyword = (ref.keyword_en or ref.keyword) if lang == "en" else ref.keyword
        unit = latest.unit or ref.unit or ""
        direction = (
            ("above" if latest.value > ref.high else "below")
            if lang == "en"
            else ("вище" if latest.value > ref.high else "нижче")
        )
        detail = (
            f"{keyword} was {direction} the reference range ({latest.value:g} {unit}, "
            f"{latest.ts.date().isoformat()}) and has not been re-measured for "
            f"{age_days:.0f} days"
            if lang == "en"
            else
            f"{keyword} був {direction} норми ({latest.value:g} {unit}, "
            f"{latest.ts.date().isoformat()}) і не перемірювався {age_days:.0f} днів"
        )
        signals.append(
            Signal(
                layer="L2a",
                kind="monitoring_gap",
                key=f"monitoring_gap:{loinc}",
                severity="moderate",
                detail=detail,
                subjects=[loinc],
                confidence=1.0,
            )
        )
    return signals


def detect_timeline_uncertainty(
    graph: TemporalGraph,
    lang: str = "uk",
    *,
    floor: float = DATED_FRACTION_FLOOR,
    min_facts: int = 3,
) -> list[Signal]:
    """Більшість фактів ні до чого не прив'язана в часі.

    Ловимо саме НЕПРИВ'ЯЗАНІСТЬ, а не відсутність дати в тексті. Лабораторний
    бланк без дати всередині, але поданий із власною датою документа, датований
    коректно — сигналу нема. Фікція — коли обидві осі часу показують момент
    обробки: тоді точка стоїть там, де її поставила система, а не клініка.

    Сигнал не клінічний, а епістемічний: він каже, що трендам у цій картці поки
    вірити не варто. Мовчазний неправильний таймлайн гірший за озвучений сумнів.
    """
    metrics = graph.metrics()
    total = sum(e.occurrences for e in graph.entities.values())
    if total < min_facts or metrics.anchored_fraction >= floor:
        return []
    pct = metrics.anchored_fraction * 100
    detail = (
        f"Only {pct:.0f}% of facts are anchored to a real date — the rest are dated by "
        f"the moment of processing. Trends and gaps over this timeline are unreliable; "
        f"check the dates in the originals."
        if lang == "en"
        else
        f"Лише {pct:.0f}% фактів прив'язані до реальної дати — решта датовані моментом "
        f"обробки. Трендам і розривам на такому таймлайні вірити не варто; варто "
        f"звірити дати в оригіналах."
    )
    return [
        Signal(
            layer="L2a",
            kind="temporal_uncertainty",
            key="temporal_uncertainty",
            severity="info",
            detail=detail,
            subjects=[e.key for e in graph.entities.values() if e.unanchored],
            confidence=1.0,
        )
    ]


def run(
    store: FactStore,
    graph: TemporalGraph,
    sex: Optional[str] = None,
    lang: str = "uk",
    *,
    now: Optional[datetime] = None,
) -> list[Signal]:
    """Усі темпоральні сигнали L2a."""
    return (
        detect_polypharmacy(graph, lang)
        + detect_monitoring_gaps(store, graph, sex, lang, now=now)
        + detect_timeline_uncertainty(graph, lang)
    )
