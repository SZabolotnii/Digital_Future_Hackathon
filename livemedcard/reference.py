"""Завантаження відкритих довідкових БД (детерміноване джерело для L1/L2a/L2b).

Усі коди узгоджені між файлами: RxNorm (ліки), LOINC (аналізи), SNOMED (діагнози).

Loader-и кешуються (`lru_cache`) і повертають **незмінні** структури: заморожені
Pydantic-моделі (`refmodels.FrozenModel`) у tuple/MappingProxyType. Це водночас
прибирає перебудову об'єктів на кожен виклик і унеможливлює псування спільного
довідника випадковою мутацією (раніше від цього захищали свіжі копії).
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Optional

from .config import DATA_DIR
from .refmodels import (
    CondTerm,
    DoseLimit,
    Interaction,
    LabTerm,
    MedTerm,
    RangeSpec,
    RefRange,
    Vocab,
)


@lru_cache(maxsize=None)
def _raw(name: str) -> Any:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def load_interactions() -> tuple[Interaction, ...]:
    """Пари RxNorm-кодів із тяжкістю взаємодії."""
    return tuple(Interaction(**p) for p in _raw("interactions.json")["pairs"])


@lru_cache(maxsize=None)
def load_dose_limits() -> Mapping[str, DoseLimit]:
    """RxNorm → DoseLimit."""
    return MappingProxyType(
        {d["rxnorm"]: DoseLimit(**d) for d in _raw("dose_limits.json")["limits"]}
    )


@lru_cache(maxsize=None)
def load_range_specs() -> Mapping[str, RangeSpec]:
    """LOINC → RangeSpec (з опціональними варіантами за статтю)."""
    return MappingProxyType({
        loinc: RangeSpec(**spec)
        for loinc, spec in _raw("reference_ranges.json")["ranges"].items()
    })


def range_for(loinc: str, sex: Optional[str] = None) -> Optional[RefRange]:
    """Референсні межі показника, розвʼязані під стать пацієнта (або None)."""
    spec = load_range_specs().get(loinc)
    return spec.resolve(sex) if spec is not None else None


@lru_cache(maxsize=None)
def load_vocab() -> Vocab:
    """Словник для StubExtractor: labs / medications / conditions."""
    raw = _raw("vocab.json")
    return Vocab(
        labs={k: LabTerm(**v) for k, v in raw.get("labs", {}).items()},
        medications={k: MedTerm(**v) for k, v in raw.get("medications", {}).items()},
        conditions={k: CondTerm(**v) for k, v in raw.get("conditions", {}).items()},
    )


def load_sample_docs() -> list[dict]:
    """Демо-документи (український текст) для E2E. Щоразу свіжа копія."""
    return json.loads(json.dumps(_raw("sample_docs.json")["documents"]))
