"""Економіка рішень TRACE: поріг ескалації як відношення вартостей.

Мотивація. `LOW_CONFIDENCE = 0.6` було магічним числом: його ніхто не виводив, і
захистити його перед регулятором (EU AI Act ст.14 — обґрунтований human oversight)
нічим. Тут воно виводиться.

**Ключова властивість TRACE.** Помилка безпеки факторизується:

    P(пропуск) = (1 - R) · 1  +  R · P(правило не спрацювало)
                                     └─────────── = 0 ────────┘

Другий доданок строго нульовий: правила L1/L2a вичерпні над своєю БД і нічого не
«вирішують». Отже весь ризик безпеки зведено до одного вимірюваного числа —
recall витягу R. Саме тому впевненість витягу (а не якась «впевненість моделі у
висновку») — єдина величина, за якою має ухвалюватись рішення про ескалацію.

**Правило рішення.** Ескалювати до людини варто тоді, коли очікувана шкода від
автоматичної обробки перевищує вартість перевірки:

    (1 - c) · π · C_harm  >  C_human
    ⟹  c* = 1 - C_human / (π · C_harm)

де c — впевненість витягу, π — частка документів цього класу, у яких узагалі є
факт, чий пропуск шкодить, C_harm — вартість такої шкоди, C_human — вартість
перегляду людиною.

Наслідок, який варто проговорити вголос: при асиметрії «$5 проти $15 000» поріг
для документів із ліками виходить ≈0.97, тобто **майже будь-який сумнів має йти до
людини**. Для документів лише з показниками (пропуск тренду не є гострою подією)
поріг помітно нижчий, і автоматична обробка економічно виправдана. Тобто поріг —
не один на всіх, а функція від того, чим документ ризикує.

⚠ Числа вартостей нижче — **робочі припущення, не дані**. Перед будь-яким
клінічним чи інвесторським використанням їх треба замінити на citable: вартість
ADE-госпіталізації (AHRQ HCUP), базовий ризик для 65+ на 5+ ліках, реальну
вартість хвилини перевірки. Модель від цього не змінюється — змінюються входи.
"""
from __future__ import annotations

from typing import Literal

from .config import (
    COST_HARM_MEDICATION,
    COST_HARM_MEASUREMENT,
    COST_HUMAN_REVIEW,
    HAZARD_PRIOR_MEDICATION,
    HAZARD_PRIOR_MEASUREMENT,
    LOW_CONFIDENCE_OVERRIDE,
    P_UNADDRESSED,
)

# Чим ризикує документ. `medication` — з нього витягнуто препарати, тож пропуск
# факту може коштувати взаємодії або передозування (гостра подія). `measurement` —
# лише показники: пропуск тренду шкодить, але не гостро. `none` — фактів немає.
Hazard = Literal["medication", "measurement", "none"]

_PARAMS: dict[str, tuple[float, float]] = {           # hazard → (π, C_harm)
    "medication": (HAZARD_PRIOR_MEDICATION, COST_HARM_MEDICATION),
    "measurement": (HAZARD_PRIOR_MEASUREMENT, COST_HARM_MEASUREMENT),
    "none": (HAZARD_PRIOR_MEASUREMENT, COST_HARM_MEASUREMENT),
}

# Поріг не має вироджуватись у крайнощі: 1.0 означав би «ніколи не довіряти», 0.0 —
# «ніколи не перевіряти». Обидва роблять шар L3 беззмістовним.
_FLOOR, _CEIL = 0.05, 0.99


def escalation_threshold(hazard: Hazard = "medication") -> float:
    """Поріг впевненості витягу, нижче якого документ іде до людини.

    Виводиться з вартостей: ``c* = 1 - C_human / (π · C_harm)``. Env
    ``LMC_LOW_CONFIDENCE`` лишається явним override — для тестів і для випадку,
    коли оператор свідомо задає поріг адміністративно, а не економічно.
    """
    if LOW_CONFIDENCE_OVERRIDE is not None:
        return LOW_CONFIDENCE_OVERRIDE
    prior, harm = _PARAMS[hazard]
    expected_harm = prior * harm
    if expected_harm <= 0:
        return _FLOOR
    return min(_CEIL, max(_FLOOR, 1.0 - COST_HUMAN_REVIEW / expected_harm))


def expected_cost(
    confidence: float,
    hazard: Hazard = "medication",
    *,
    escalated: bool = False,
    high_risk_signal: bool = False,
) -> float:
    """Очікувана вартість одного рішення (умовні одиниці вартості).

    Ескалація коштує рівно ``C_human``: людина перевіряє документ, і залишковий
    ризик вважаємо нульовим (припущення на користь людини — воно ж робить оцінку
    консервативною щодо цінності автоматизації).

    В автоматичної обробки **два різні джерела шкоди**, і плутати їх не можна:

    1. **Ризик витягу** — факт міг не витягнутись: ``(1-c)·π·C_harm``. Тут π —
       апріорна частка документів, у яких взагалі є що пропускати.
    2. **Непроглянута підтверджена небезпека** — детермінований сигнал уже
       спрацював (взаємодія, перевищення дози), тобто небезпека не гіпотетична, а
       наявна. Тоді π не потрібне: ризик у тому, що родина не відреагує на знайдене
       без лікаря — ``P_unaddressed · C_harm``.

    Без другого доданка модель вважала ескалацію на реальній взаємодії марною
    витратою ($5 проти нуля) — і виходило, що шар L3 приносить збиток. Це був
    дефект моделі, а не політики.
    """
    if escalated:
        return COST_HUMAN_REVIEW
    prior, harm = _PARAMS[hazard]
    cost = max(0.0, 1.0 - confidence) * prior * harm
    if high_risk_signal:
        cost += P_UNADDRESSED * _PARAMS["medication"][1]
    return cost


def hazard_of(*, has_medications: bool, has_observations: bool) -> Hazard:
    """Чим ризикує документ — за тим, що з нього фактично витягнуто."""
    if has_medications:
        return "medication"
    return "measurement" if has_observations else "none"


def summary() -> dict:
    """Параметри та виведені пороги — для `/metrics` і для захисту рішення."""
    return {
        "cost_human_review": COST_HUMAN_REVIEW,
        "cost_harm_medication": COST_HARM_MEDICATION,
        "cost_harm_measurement": COST_HARM_MEASUREMENT,
        "hazard_prior_medication": HAZARD_PRIOR_MEDICATION,
        "hazard_prior_measurement": HAZARD_PRIOR_MEASUREMENT,
        "p_unaddressed": P_UNADDRESSED,
        "threshold_medication": round(escalation_threshold("medication"), 3),
        "threshold_measurement": round(escalation_threshold("measurement"), 3),
        "threshold_source": "override" if LOW_CONFIDENCE_OVERRIDE is not None else "derived",
    }
