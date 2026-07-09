"""Легкий детектор мови (UA/EN) для білінгвального режиму.

Евристика за абеткою: частка кириличних літер серед усіх літер. Достатньо для
вибору мови відповіді Q&A та адаптації промптів — без важких залежностей.
"""
from __future__ import annotations

import re
from typing import Literal

Lang = Literal["uk", "en"]

_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_lang(text: str, *, default: Lang = "uk") -> Lang:
    """Повертає 'uk' чи 'en' за переважанням абетки; `default` — коли літер немає."""
    cyr = len(_CYRILLIC_RE.findall(text))
    lat = len(_LATIN_RE.findall(text))
    if cyr == 0 and lat == 0:
        return default
    return "uk" if cyr >= lat else "en"
