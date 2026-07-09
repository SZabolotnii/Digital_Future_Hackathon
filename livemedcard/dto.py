"""DTO конвеєра TRACE — спільні типи між шарами.

`Signal` — єдиний тип сигналу для L1 і L2a; це роз'єднує L3-роутер від реалізацій
шарів безпеки. Детерміновані сигнали мають ``confidence == 1.0``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .fhir import Condition, DocumentReference, MedicationRequest, Observation

Severity = Literal["info", "moderate", "major", "critical"]
DocKind = Literal["discharge_summary", "lab_report", "prescription", "note"]

_SEVERITY_ORDER: dict[str, int] = {"info": 0, "moderate": 1, "major": 2, "critical": 3}


def severity_rank(s: Severity) -> int:
    """Порядковий ранг тяжкості (більше = гірше)."""
    return _SEVERITY_ORDER[s]


class Document(BaseModel):
    """Вхідний документ (у MVP — текст; у проді — фото → OCR/vision)."""

    id: str
    text: str
    kind: DocKind = "note"
    received_at: Optional[datetime] = None


class ExtractionResult(BaseModel):
    """Результат L2b: FHIR-ресурси + середня впевненість витягу.

    ``document_reference`` (free-text іпостась) заповнює лише двоетапний
    екстрактор; детермінований stub лишає None.
    """

    observations: list[Observation] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)
    medications: list[MedicationRequest] = Field(default_factory=list)
    mean_confidence: float = 1.0
    document_reference: Optional[DocumentReference] = None

    def is_empty(self) -> bool:
        return not (self.observations or self.conditions or self.medications)


class Signal(BaseModel):
    """Детермінований сигнал безпеки/тренду (L1 або L2a)."""

    layer: Literal["L1", "L2a"]
    kind: str  # напр. "interaction", "dose_limit", "trend", "range_breach"
    severity: Severity
    detail: str
    subjects: list[str] = Field(default_factory=list)  # посилання на FHIR-ресурси
    confidence: float = 1.0


class Decision(BaseModel):
    """Рішення L3: авто чи ескалація до людини (L4)."""

    route_to: Literal["auto", "L4_human"]
    escalate: bool
    reasons: list[str] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)


class TraceReport(BaseModel):
    """Повний результат прогону одного документа крізь конвеєр."""

    document_id: str
    extraction: ExtractionResult
    signals: list[Signal]
    decision: Decision


class AuditEntry(BaseModel):
    """Запис аудиту одного рішення L3 (типізована схема журналу).

    Гарантує стабільну структуру журналу між версіями: знімки зі сторонніми чи
    неповними записами відхиляються валідацією при відновленні.
    """

    ts: datetime
    document_id: str
    kind: str
    extraction_confidence: float
    n_signals: int
    route_to: Literal["auto", "L4_human"]
    escalate: bool
