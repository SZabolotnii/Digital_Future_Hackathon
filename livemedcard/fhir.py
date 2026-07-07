"""FHIR-вирівняні моделі даних (HL7 FHIR R4, мінімальний профіль для MVP).

Беремо СТРУКТУРИ РЕСУРСІВ як схему даних; серверний шар (REST/OAuth/Bundle) не беремо.
Cardinality й value sets — за fhir-developer-skill. `Provenance` — не-FHIR розширення,
основа метрики довіри (звідки взято факт).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Sys:
    """URL кодувальних систем."""

    LOINC = "http://loinc.org"
    SNOMED = "http://snomed.info/sct"
    RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
    ICD10 = "http://hl7.org/fhir/sid/icd-10"


# ── datatypes ────────────────────────────────────────────────────────────────
class Coding(BaseModel):
    system: str
    code: str
    display: Optional[str] = None


class CodeableConcept(BaseModel):
    coding: list[Coding] = Field(default_factory=list)
    text: Optional[str] = None


class Reference(BaseModel):
    reference: str  # напр. "Patient/mom-001"
    display: Optional[str] = None


class Quantity(BaseModel):
    value: float
    unit: Optional[str] = None
    system: Optional[str] = None
    code: Optional[str] = None


class Provenance(BaseModel):
    """Не-FHIR розширення: звідки взято факт. Живить провенанс-ребра графа."""

    source_doc_id: str
    extracted_by: str = "MedGemma-4B"
    extracted_at: datetime = Field(default_factory=_utcnow)
    confidence: Optional[float] = None


# ── resources ─────────────────────────────────────────────────────────────────
class Patient(BaseModel):
    resourceType: Literal["Patient"] = "Patient"
    id: str
    gender: Optional[Literal["male", "female", "other", "unknown"]] = None
    birthDate: Optional[date] = None


class Encounter(BaseModel):
    resourceType: Literal["Encounter"] = "Encounter"
    id: str
    status: Literal[
        "planned", "arrived", "triaged", "in-progress", "onleave",
        "finished", "cancelled", "entered-in-error", "unknown",
    ]
    class_: Coding = Field(alias="class")  # 1..1 Coding (не CodeableConcept)
    subject: Optional[Reference] = None
    period_start: Optional[date] = None
    model_config = {"populate_by_name": True}


class Observation(BaseModel):
    resourceType: Literal["Observation"] = "Observation"
    id: str
    status: Literal[
        "registered", "preliminary", "final", "amended", "corrected",
        "cancelled", "entered-in-error", "unknown",
    ]
    code: CodeableConcept  # 1..1
    subject: Optional[Reference] = None
    effectiveDateTime: Optional[datetime] = None
    valueQuantity: Optional[Quantity] = None
    provenance: Optional[Provenance] = None


class Condition(BaseModel):
    resourceType: Literal["Condition"] = "Condition"
    id: str
    subject: Reference  # 1..1 — єдине обов'язкове
    code: Optional[CodeableConcept] = None
    clinicalStatus: Optional[
        Literal["active", "recurrence", "relapse", "inactive", "remission", "resolved"]
    ] = None
    onsetDateTime: Optional[datetime] = None
    provenance: Optional[Provenance] = None


class MedicationRequest(BaseModel):
    resourceType: Literal["MedicationRequest"] = "MedicationRequest"
    id: str
    status: Literal[
        "active", "on-hold", "cancelled", "completed",
        "entered-in-error", "stopped", "draft", "unknown",
    ]
    intent: Literal[
        "proposal", "plan", "order", "original-order", "reflex-order",
        "filler-order", "instance-order", "option",
    ]
    medicationCodeableConcept: CodeableConcept  # medication[x] 1..1
    subject: Reference  # 1..1
    reason: Optional[Reference] = None  # → Condition (ребро indicated_for)
    dose_mg_per_day: Optional[float] = None  # спрощення для дозового чеку L1
    provenance: Optional[Provenance] = None


def rxnorm_of(m: MedicationRequest) -> Optional[str]:
    """RxNorm-код препарату або None."""
    return next(
        (c.code for c in m.medicationCodeableConcept.coding if c.system == Sys.RXNORM),
        None,
    )


def loinc_of(o: Observation) -> Optional[str]:
    """LOINC-код показника або None."""
    return next((c.code for c in o.code.coding if c.system == Sys.LOINC), None)
