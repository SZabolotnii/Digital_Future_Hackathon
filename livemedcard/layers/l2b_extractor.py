"""L2b — розуміння вільного тексту.

У MVP детермінований stub замість MedGemma; екстрактор НЕ робить висновків
про безпеку, лише витягує факти (лабораторні показники, ліки, діагнози)
з українського вільного тексту у FHIR-ресурси.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional, Protocol, TypeVar

from ..config import STUB_CONFIDENCE
from ..dto import Document, ExtractionResult
from ..fhir import (
    CodeableConcept,
    Coding,
    Condition,
    MedicationRequest,
    Observation,
    Provenance,
    Quantity,
    Reference,
    Sys,
)
from ..reference import load_vocab

_T = TypeVar("_T")

# Значення показника і дозу шукаємо ТІЛЬКИ впритул після терміна (між ними —
# лише роздільники), а не пошуком уперед по всьому тексту. Інакше показник чи
# ліки без власного числа «крадуть» число наступного терміна (напр. систолічний
# тиск забирав би значення калію, а варфарин — дозу аспірину).
_SEP = r"[\s:=–—-]*"
_LAB_VALUE_RE = re.compile(_SEP + r"(\d+(?:\.\d+)?)")
_DOSE_MG_RE = re.compile(_SEP + r"(\d+(?:\.\d+)?)\s*мг")
# Кратність прийому: «×2», «x2», «2 рази», «двічі», «тричі» → множник за добу.
_FREQ_RE = re.compile(r"[×xх]\s*(\d+)|(\d+)\s*раз|(двіч)|(тріч)", re.IGNORECASE)


def _stable_id(prefix: str, *parts: object) -> str:
    """Детермінований id ресурсу від його змісту, а не порядку знахідки.

    Стабільний між запусками і версіями словника: зміна порядку матчингу не
    змінює id, тож ідемпотентність FactStore переживає повторний витяг і
    відновлення зі старих знімків. Ідентичний зміст (той самий документ, код,
    значення, час) навмисно дає той самий id — це один факт.
    """
    key = "|".join(str(p) for p in parts)
    return f"{prefix}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"


def _parse_daily_dose_mg(text: str, pos: int) -> Optional[float]:
    """Добова доза в мг: разова доза «N мг» × кратність прийому (за замовч. 1)."""
    m = _DOSE_MG_RE.match(text, pos)
    if m is None:
        return None
    single = float(m.group(1))
    freq = 1
    fm = _FREQ_RE.search(text, m.end(), m.end() + 25)  # вікно тієї самої фрази
    if fm:
        if fm.group(1):
            freq = int(fm.group(1))
        elif fm.group(2):
            freq = int(fm.group(2))
        elif fm.group(3):
            freq = 2  # двічі
        elif fm.group(4):
            freq = 3  # тричі
    return single * freq


class Extractor(Protocol):
    """Контракт L2b-екстрактора."""

    def extract(self, doc: Document, patient_id: str) -> ExtractionResult: ...


def _find_vocab_terms(
    text: str, vocab_terms: dict[str, _T]
) -> list[tuple[str, _T, re.Match[str]]]:
    """Знаходить УСІ входження кожного терміна в тексті.

    Довші (багатослівні) терміни перевіряються першими, щоб виграти при
    перекритті зі коротшими. Результат впорядкований за позицією входження
    в тексті.
    """
    terms_by_length_desc = sorted(vocab_terms.items(), key=lambda kv: -len(kv[0]))
    claimed_spans: list[tuple[int, int]] = []
    found: list[tuple[int, str, _T, re.Match[str]]] = []
    for term, info in terms_by_length_desc:
        pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
        # Усі входження терміна, а не лише перше — щоб не губити повторні
        # вимірювання одного показника в межах документа.
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < claimed_end and end > claimed_start for claimed_start, claimed_end in claimed_spans):
                continue
            claimed_spans.append((start, end))
            found.append((start, term, info, match))
    found.sort(key=lambda item: item[0])
    return [(term, info, match) for _, term, info, match in found]


class StubExtractor:
    """Детермінований stub-екстрактор (заміна MedGemma для MVP)."""

    def extract(self, doc: Document, patient_id: str) -> ExtractionResult:
        vocab = load_vocab()
        subject = Reference(reference=f"Patient/{patient_id}")
        text = doc.text

        observations: list[Observation] = []
        seen_obs: set[str] = set()
        for term, info, match in _find_vocab_terms(text, vocab.labs):
            number_match = _LAB_VALUE_RE.match(text, match.end())
            if number_match is None:
                continue
            obs_id = _stable_id(
                "obs", doc.id, info.loinc, number_match.group(1), doc.received_at
            )
            if obs_id in seen_obs:  # ідентична повторна згадка — той самий факт
                continue
            seen_obs.add(obs_id)
            observations.append(
                Observation(
                    id=obs_id,
                    status="final",
                    code=CodeableConcept(
                        coding=[
                            Coding(
                                system=Sys.LOINC,
                                code=info.loinc,
                                display=info.display,
                            )
                        ],
                        text=term.capitalize(),
                    ),
                    subject=subject,
                    effectiveDateTime=doc.received_at,
                    valueQuantity=Quantity(
                        value=float(number_match.group(1)), unit=info.unit
                    ),
                    provenance=Provenance(
                        source_doc_id=doc.id,
                        extracted_by="StubExtractor",
                        confidence=STUB_CONFIDENCE,
                    ),
                )
            )

        medications: list[MedicationRequest] = []
        seen_med: set[str] = set()
        for term, info, match in _find_vocab_terms(text, vocab.medications):
            dose_mg_per_day: Optional[float] = _parse_daily_dose_mg(text, match.end())
            med_id = _stable_id("med", doc.id, info.rxnorm, dose_mg_per_day)
            if med_id in seen_med:
                continue
            seen_med.add(med_id)
            medications.append(
                MedicationRequest(
                    id=med_id,
                    status="active",
                    intent="order",
                    medicationCodeableConcept=CodeableConcept(
                        coding=[
                            Coding(
                                system=Sys.RXNORM,
                                code=info.rxnorm,
                                display=info.display,
                            )
                        ],
                        text=term.capitalize(),
                    ),
                    subject=subject,
                    dose_mg_per_day=dose_mg_per_day,
                    provenance=Provenance(
                        source_doc_id=doc.id,
                        extracted_by="StubExtractor",
                        confidence=STUB_CONFIDENCE,
                    ),
                )
            )

        conditions: list[Condition] = []
        seen_cond: set[str] = set()
        for term, info, _match in _find_vocab_terms(text, vocab.conditions):
            cond_id = _stable_id("cond", doc.id, info.snomed)
            if cond_id in seen_cond:
                continue
            seen_cond.add(cond_id)
            conditions.append(
                Condition(
                    id=cond_id,
                    subject=subject,
                    code=CodeableConcept(
                        coding=[
                            Coding(
                                system=Sys.SNOMED,
                                code=info.snomed,
                                display=info.display,
                            )
                        ],
                        text=term,
                    ),
                    clinicalStatus="active",
                    onsetDateTime=doc.received_at,
                    provenance=Provenance(
                        source_doc_id=doc.id,
                        extracted_by="StubExtractor",
                        confidence=STUB_CONFIDENCE,
                    ),
                )
            )

        # Порожній витяг означає «нічого не розпізнали», а не «впевнені на 100%».
        # Низька впевненість веде документ на перегляд людиною (L3), а не в auto.
        mean_confidence = (
            STUB_CONFIDENCE if (observations or medications or conditions) else 0.0
        )
        return ExtractionResult(
            observations=observations,
            conditions=conditions,
            medications=medications,
            mean_confidence=mean_confidence,
        )


def extract_documents(
    docs: list[Document], patient_id: str, extractor: Extractor | None = None
) -> ExtractionResult:
    """Об'єднує результати L2b-екстракції з кількох документів."""
    extractor = extractor if extractor is not None else StubExtractor()

    observations: list[Observation] = []
    conditions: list[Condition] = []
    medications: list[MedicationRequest] = []
    confidences: list[float] = []

    for doc in docs:
        result = extractor.extract(doc, patient_id)
        observations.extend(result.observations)
        conditions.extend(result.conditions)
        medications.extend(result.medications)
        confidences.append(result.mean_confidence)

    mean_confidence = sum(confidences) / len(confidences) if confidences else 1.0
    return ExtractionResult(
        observations=observations,
        conditions=conditions,
        medications=medications,
        mean_confidence=mean_confidence,
    )
