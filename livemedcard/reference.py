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
from .versioning import fingerprint


@lru_cache(maxsize=None)
def _raw(name: str) -> Any:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


# Файли, що утворюють НАБІР ПРАВИЛ безпеки: саме вони визначають, чи стане факт
# сигналом. `vocab.json` сюди не входить — він впливає на витяг (recall), а не на
# рішення про небезпеку, і змішувати ці дві речі в одній версії було б неточно.
_RULESET_FILES = ("interactions.json", "dose_limits.json", "reference_ranges.json")


@lru_cache(maxsize=None)
def ruleset_version() -> str:
    """Версія набору правил безпеки: задекларована + відбиток вмісту.

    Задекларована частина (`"version"` у файлах) прив'язана до `data/CHANGELOG.md`
    і має обґрунтування — це те, що читає аудитор. Відбиток вмісту закриває
    очевидну дірку такої схеми: правило можна змінити й забути підняти версію.
    Разом вони дають властивість, яку перевіряє тест: **вміст правил неможливо
    змінити так, щоб журнал цього не помітив.**
    """
    declared = "/".join(str(_raw(n).get("version", "0")) for n in _RULESET_FILES)
    body = "".join(
        json.dumps(_raw(n), sort_keys=True, ensure_ascii=False) for n in _RULESET_FILES
    )
    return f"{declared}+{fingerprint(body)}"


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


def load_sample_docs(lang: str = "uk") -> list[dict]:
    """Демо-документи мовою ``lang`` (``uk``|``en``). Щоразу свіжа копія.

    Набори дзеркальні: ті самі значення й ті самі призначення, тож сигнали
    безпеки збігаються — демо не залежить від того, якою мовою його дивляться.
    """
    key = "en" if lang == "en" else "uk"
    return json.loads(json.dumps(_raw("sample_docs.json")[key]))
