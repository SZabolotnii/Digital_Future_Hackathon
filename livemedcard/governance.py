"""Задекларована межа автономії системи (принцип A TRACE; EU AI Act ст. 14).

Межа відповідає на питання «що цій системі дозволено робити самій». Без
записаної відповіді автономія дрейфує: кожна окрема зміна виглядає невинною, а
разом вони посувають систему з «підказує» в «вирішує», і момент переходу ніде
не зафіксований.

Класи:

- ``mandatory_review`` — система лише готує докази, вирішує людина;
- ``advisory`` — система показує висновок, людина може не втручатися;
- ``autonomous`` — система діє сама (тут: епістемічні застереження про власні
  дані, які нічого не стверджують про пацієнта).

**Чому клас виводиться з тяжкості, а не з окремої таблиці за видом сигналу.**
Тяжкість уже призначена правилом у L1/L2a і вона ж є критерієм ескалації в L3
(`severity_rank >= major`). Друга, незалежна таблиця дозволила б їм розійтися —
з'явився б сигнал, який ескалює до людини, але переглядати його не обов'язково,
або навпаки. Одне джерело істини дорожче помилитись, але дешевше перевірити.

Порушення межі — не абстракція, а подія: `BoundaryEvent` пишеться, коли картку
віддають назовні (`GET /export`) при непереглянутих ``mandatory_review``. Саме
експорт є моментом, коли рекомендація починає жити як рішення.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from .dto import Severity, Signal

AutonomyClass = Literal["autonomous", "advisory", "mandatory_review"]

__all__ = ["AutonomyClass", "DECLARATION", "autonomy_class", "unreviewed_mandatory"]


DECLARATION: dict[Severity, AutonomyClass] = {
    "info": "autonomous",
    "moderate": "advisory",
    "major": "mandatory_review",
    "critical": "mandatory_review",
}


def autonomy_class(signal: Signal) -> AutonomyClass:
    """Клас автономії для сигналу — за задекларованою таблицею."""
    return DECLARATION[signal.severity]


def unreviewed_mandatory(
    signals: Iterable[Signal], reviewed_keys: Iterable[str]
) -> list[Signal]:
    """Сигнали, що вимагають рішення людини і його ще не отримали."""
    seen = set(reviewed_keys)
    return [
        s
        for s in signals
        if autonomy_class(s) == "mandatory_review" and s.key not in seen
    ]
