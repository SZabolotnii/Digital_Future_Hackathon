"""Шар TRACE L3: stateful політика роутингу/ескалації.

Два незалежні приводи ескалації до людини (L4):

1. **Детермінований сигнал високого ризику** (major/critical з L1/L2a). Такі
   сигнали самі по собі достовірні (confidence = 1.0), тому їхня передача людині
   НЕ залежить від впевненості текстового витягу L2b — реальна небезпека завжди
   потребує рішення людини.
2. **Низька впевненість витягу L2b** — документ погано розпізнано, факти
   ненадійні, тому потрібна перевірка людиною навіть без сигналів.
"""
from __future__ import annotations

from typing import Optional

from ..config import LOW_CONFIDENCE
from ..dto import Decision, Severity, Signal, severity_rank

__all__ = ["route", "LOW_CONFIDENCE"]


def route(signals: list[Signal], extraction_confidence: float, doc_kind: str) -> Decision:
    """Приймає рішення про маршрут документа: "auto" чи "L4_human"."""
    top: Optional[Signal] = max(
        signals, key=lambda s: severity_rank(s.severity), default=None
    )
    high_risk = top is not None and severity_rank(top.severity) >= severity_rank(
        _MAJOR
    )
    low_confidence = extraction_confidence < LOW_CONFIDENCE
    escalate = high_risk or low_confidence

    reasons: list[str] = []
    if high_risk and top is not None:
        reasons.append(
            f"Детермінований сигнал рівня «{top.severity}» ({top.kind}) — "
            f"рішення за людиною незалежно від впевненості витягу"
        )
    if low_confidence:
        reasons.append(
            f"Низька впевненість витягу ({extraction_confidence:.2f}) — "
            f"потрібна перевірка людиною"
        )
    if not escalate:
        reasons.append(
            "Немає сигналів високого ризику, витяг впевнений — автоматична обробка"
        )
    reasons.append(f"Тип документа: {doc_kind}")

    return Decision(
        route_to="L4_human" if escalate else "auto",
        escalate=escalate,
        reasons=reasons,
        signals=signals,
    )


_MAJOR: Severity = "major"
