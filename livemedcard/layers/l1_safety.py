"""Шар TRACE L1: детермінований шар безпеки (взаємодії ліків, межі доз).

Це якір конвеєра: рішення про клінічну небезпеку приймається за перевіреними
правилами відкритих довідкових БД (RxNorm), а не мовною моделлю. Усі сигнали
L1 детерміновані — ``confidence == 1.0``.
"""
from __future__ import annotations

from functools import lru_cache
from itertools import combinations

from ..dto import Signal
from ..fhir import MedicationRequest, rxnorm_of
from ..refmodels import Interaction
from ..reference import load_dose_limits, load_interactions


@lru_cache(maxsize=None)
def _pair_map() -> dict[frozenset[str], Interaction]:
    """Індекс взаємодій за неупорядкованою парою RxNorm (будується один раз)."""
    return {entry.pair(): entry for entry in load_interactions()}


def _display_name(m: MedicationRequest) -> str:
    """Назва препарату для тексту сигналу: text CodeableConcept → display coding → RxNorm-код."""
    cc = m.medicationCodeableConcept
    if cc.text:
        return cc.text
    for c in cc.coding:
        if c.display:
            return c.display
    return rxnorm_of(m) or m.id


def check_interactions(meds: list[MedicationRequest]) -> list[Signal]:
    """Перевірка кожної неупорядкованої пари ліків на відому взаємодію (RxNorm)."""
    pair_map = _pair_map()
    signals: list[Signal] = []
    for m1, m2 in combinations(meds, 2):
        rx1, rx2 = rxnorm_of(m1), rxnorm_of(m2)
        if rx1 is None or rx2 is None:
            continue
        entry = pair_map.get(frozenset((rx1, rx2)))
        if entry is None:
            continue
        name1, name2 = _display_name(m1), _display_name(m2)
        signals.append(
            Signal(
                layer="L1",
                kind="interaction",
                severity=entry.severity,
                detail=f"{name1} + {name2}: {entry.detail}",
                subjects=[f"MedicationRequest/{m1.id}", f"MedicationRequest/{m2.id}"],
                confidence=1.0,
            )
        )
    return signals


def check_dose_limits(meds: list[MedicationRequest]) -> list[Signal]:
    """Перевірка кожних ліків на перевищення максимальної добової дози (RxNorm)."""
    limits = load_dose_limits()
    signals: list[Signal] = []
    for m in meds:
        rx = rxnorm_of(m)
        if rx is None or rx not in limits:
            continue
        if m.dose_mg_per_day is None:
            continue
        limit = limits[rx]
        if m.dose_mg_per_day > limit.max_daily:
            signals.append(
                Signal(
                    layer="L1",
                    kind="dose_limit",
                    severity="major",
                    detail=(
                        f"{limit.name}: добова доза {m.dose_mg_per_day:g} "
                        f"{limit.unit} — перевищено добову межу "
                        f"{limit.max_daily:g} {limit.unit}"
                    ),
                    subjects=[f"MedicationRequest/{m.id}"],
                    confidence=1.0,
                )
            )
    return signals


def run(meds: list[MedicationRequest]) -> list[Signal]:
    """Повний прогін L1: взаємодії + межі доз, у стабільному порядку."""
    return check_interactions(meds) + check_dose_limits(meds)
