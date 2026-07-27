"""Шар TRACE L3: політика роутингу/ескалації над stateless-інвентарем L2.

Два незалежні приводи ескалації до людини (L4):

1. **Детермінований сигнал високого ризику** (major/critical з L1/L2a). Такі
   сигнали самі по собі достовірні (confidence = 1.0), тому їхня передача людині
   НЕ залежить від впевненості текстового витягу L2b — реальна небезпека завжди
   потребує рішення людини.
2. **Низька впевненість витягу L2b** — документ погано розпізнано, факти
   ненадійні, тому потрібна перевірка людиною навіть без сигналів.

**Межі стану названо прямо.** `route` — чиста функція від одного результату
витягу. Стан у політики є, але рівно один і в межах життя документа:
`should_reinvoke` рахує витрачені спроби проти бюджету, тож перед тим як кликати
людину, система може спробувати альтернативний екстрактор. Накопиченої
впевненості між документами, розширення контексту й перевірки узгодженості
L2a/L2b тут немає — і поки їх немає, називати шар stateful у ширшому сенсі
означало б приписати системі властивість, якої в ній нема.

`card_decision` — окреме питання до тієї ж політики: не «що зробити з цим
документом», а «що в картці лишається невирішеним». Тому воно не вигадує
впевненість витягу, а дивиться на непереглянуті ескалації.
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..config import REINVOCATION_BUDGET
from ..dto import AuditEntry, Decision, Severity, Signal, severity_rank
from ..economics import Hazard, escalation_threshold

__all__ = ["route", "card_decision", "should_reinvoke"]


def route(
    signals: list[Signal],
    extraction_confidence: float,
    doc_kind: str,
    lang: str = "uk",
    hazard: Hazard = "medication",
) -> Decision:
    """Приймає рішення про маршрут документа: "auto" чи "L4_human".

    Поріг впевненості не константа: він виводиться з вартостей (`economics`) і
    залежить від того, чим ризикує документ. Пропуск препарату коштує дорожче за
    пропущений тренд, тож і планка довіри для нього вища.
    """
    top: Optional[Signal] = max(
        signals, key=lambda s: severity_rank(s.severity), default=None
    )
    high_risk = top is not None and severity_rank(top.severity) >= severity_rank(
        _MAJOR
    )
    threshold = escalation_threshold(hazard)
    low_confidence = extraction_confidence < threshold
    escalate = high_risk or low_confidence

    en = lang == "en"
    reasons: list[str] = []
    if high_risk and top is not None:
        reasons.append(
            f"Deterministic “{top.severity}” signal ({top.kind}) — "
            f"human decision required regardless of extraction confidence"
            if en else
            f"Детермінований сигнал рівня «{top.severity}» ({top.kind}) — "
            f"рішення за людиною незалежно від впевненості витягу"
        )
    if low_confidence:
        reasons.append(
            f"Extraction confidence {extraction_confidence:.2f} < {threshold:.2f} — "
            f"the threshold follows from the cost of a missed fact, not a tuned "
            f"hyperparameter — human review required"
            if en else
            f"Впевненість витягу {extraction_confidence:.2f} < {threshold:.2f} — "
            f"поріг виведено з вартості пропущеного факту, а не підібрано — "
            f"потрібна перевірка людиною"
        )
    if not escalate:
        reasons.append(
            "No high-risk signals, extraction confident — automatic processing"
            if en else
            "Немає сигналів високого ризику, витяг впевнений — автоматична обробка"
        )
    reasons.append(f"Document type: {doc_kind}" if en else f"Тип документа: {doc_kind}")

    return Decision(
        route_to="L4_human" if escalate else "auto",
        escalate=escalate,
        reasons=reasons,
        signals=signals,
    )


def should_reinvoke(
    extraction_confidence: float,
    hazard: Hazard,
    attempts_used: int,
    *,
    budget: int = REINVOCATION_BUDGET,
) -> bool:
    """Чи варто спробувати ще раз іншим екстрактором, замість кликати людину.

    Це єдине місце, де політика має **стан**: рішення залежить не лише від
    поточного результату, а й від того, скільки спроб уже витрачено.

    Перевикликаємо лише через низьку впевненість витягу. Знайдена небезпека
    (major+) — не привід для другої спроби: сигнал уже достовірний, і повторний
    витяг його не спростує, а лише відкладе передачу людині. Плутати «погано
    прочитали» з «прочитали добре й знайшли погане» — найдорожча помилка, яку
    тут можна зробити.
    """
    if attempts_used > budget:
        return False
    return extraction_confidence < escalation_threshold(hazard)


def card_decision(
    signals: list[Signal],
    outstanding: Iterable[AuditEntry],
    lang: str = "uk",
    reviewed_keys: Iterable[str] = (),
) -> Decision:
    """Що в картці лишається невирішеним — зведення, а не нове роутингове рішення.

    Раніше тут викликався `route` із захардкоженою впевненістю 1.0, і це мовчки
    втрачало половину політики: документ, ескальований через погано розпізнаний
    текст, зникав зі стану картки одразу після завантаження — ескалація жила
    лише в тості. Тепер невирішене видно доти, доки його не переглянули.

    Рахуються лише НЕПЕРЕГЛЯНУТІ сигнали. Взаємодія нікуди не дівається від
    того, що лікар її подивився, — але рішення щодо неї вже прийняте, і картка
    має це показати. Інакше бейдж червоний завжди, робота людини не має видимого
    наслідку, і нагляд вироджується в штампування — те саме, від чого шар L4 і
    мав би захищати.
    """
    seen = set(reviewed_keys)
    pending_signals = [s for s in signals if s.key not in seen]
    top: Optional[Signal] = max(
        pending_signals, key=lambda s: severity_rank(s.severity), default=None
    )
    high_risk = top is not None and severity_rank(top.severity) >= severity_rank(
        _MAJOR
    )
    pending = list(outstanding)
    escalate = high_risk or bool(pending)

    en = lang == "en"
    reasons: list[str] = []
    if high_risk and top is not None:
        reasons.append(
            f"Deterministic “{top.severity}” signal ({top.kind}) — "
            f"human decision required"
            if en else
            f"Детермінований сигнал рівня «{top.severity}» ({top.kind}) — "
            f"рішення за людиною"
        )
    if pending:
        n = len(pending)
        reasons.append(
            f"{n} document(s) escalated on extraction confidence and not yet "
            f"reviewed"
            if en else
            f"Документів, ескальованих за впевненістю витягу й ще не "
            f"переглянутих: {n}"
        )
    if not escalate:
        reasons.append(
            "No high-risk signals, nothing awaiting review — automatic processing"
            if en else
            "Немає сигналів високого ризику, непереглянутого теж — автоматична обробка"
        )
    return Decision(
        route_to="L4_human" if escalate else "auto",
        escalate=escalate,
        reasons=reasons,
        signals=signals,
    )


_MAJOR: Severity = "major"
