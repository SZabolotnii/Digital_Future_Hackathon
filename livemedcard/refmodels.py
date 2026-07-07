"""Типізовані моделі довідкових даних (замість «сирих» dict).

Дає валідацію при завантаженні, автодоповнення й чіткий контракт для L1/L2a/L2b.

Усі моделі **заморожені** (frozen): loader-и в `reference.py` кешують екземпляри,
тож незмінність — гарантія, що жоден викликач не зіпсує спільний довідник.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from .dto import Severity


class FrozenModel(BaseModel):
    """База довідкових моделей: незмінні після створення (безпечно кешувати)."""

    model_config = ConfigDict(frozen=True)


class Interaction(FrozenModel):
    a: str  # RxNorm
    b: str  # RxNorm
    severity: Severity
    detail: str

    def pair(self) -> frozenset[str]:
        return frozenset((self.a, self.b))


class DoseLimit(FrozenModel):
    rxnorm: str
    name: str
    max_daily: float
    unit: str


class Bounds(FrozenModel):
    low: float
    high: float


class RangeSpec(FrozenModel):
    """Референсні межі показника з опціональними варіантами за статтю."""

    keyword: str
    unit: str
    default: Bounds
    by_sex: dict[str, Bounds] = {}

    def resolve(self, sex: Optional[str]) -> "RefRange":
        bounds = self.by_sex.get(sex or "", self.default)
        return RefRange(
            keyword=self.keyword, unit=self.unit,
            low=bounds.low, high=bounds.high,
            sex=sex if (sex and sex in self.by_sex) else None,
        )


class RefRange(FrozenModel):
    """Розвʼязані межі для конкретного пацієнта (стать враховано)."""

    keyword: str
    unit: str
    low: float
    high: float
    sex: Optional[str] = None


class LabTerm(FrozenModel):
    loinc: str
    unit: str
    display: str


class MedTerm(FrozenModel):
    rxnorm: str
    display: str


class CondTerm(FrozenModel):
    snomed: str
    display: str


class Vocab(FrozenModel):
    labs: dict[str, LabTerm] = {}
    medications: dict[str, MedTerm] = {}
    conditions: dict[str, CondTerm] = {}
