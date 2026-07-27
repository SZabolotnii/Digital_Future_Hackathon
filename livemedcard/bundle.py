"""Експорт/імпорт картки як стандартного FHIR Bundle (type=collection).

Наслідок продуктовий: картку можна передати лікарняній системі чи еЗдоров'ю у
стандартному форматі, а не як скрін. Метадані (документи, аудит) — у полі `_meta`,
щоб не порушувати FHIR-структуру ресурсів.
"""
from __future__ import annotations

from typing import Optional

from .layers.l2b_extractor import Extractor
from .pipeline import LiveMedCard

_RT_TO_KEY = {
    "Observation": "observations",
    "Condition": "conditions",
    "MedicationRequest": "medications",
    "DocumentReference": "document_references",
}


def to_bundle(card: LiveMedCard) -> dict:
    """Серіалізувати картку у FHIR Bundle."""
    snap = card.snapshot()
    entries = [{"resource": snap["patient"]}]
    for key in ("observations", "conditions", "medications", "document_references"):
        entries.extend({"resource": r} for r in snap[key])
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": entries,
        "_meta": {
            "documents": snap["documents"],
            "document_dates": snap["document_dates"],
            "document_texts": snap["document_texts"],
            "audit": snap["audit"],
            "reviews": snap["reviews"],
            "boundary_events": snap["boundary_events"],
        },
    }


def from_bundle(data: dict, extractor: Optional[Extractor] = None) -> LiveMedCard:
    """Відновити картку з FHIR Bundle."""
    patient: Optional[dict] = None
    buckets: dict[str, list] = {
        "observations": [],
        "conditions": [],
        "medications": [],
        "document_references": [],
    }
    for entry in data.get("entry", []):
        resource = entry.get("resource", {})
        rt = resource.get("resourceType")
        if rt == "Patient":
            patient = resource
        elif rt in _RT_TO_KEY:
            buckets[_RT_TO_KEY[rt]].append(resource)
    if patient is None:
        raise ValueError("Bundle не містить ресурсу Patient")
    meta = data.get("_meta", {})
    snapshot = {
        "patient": patient,
        **buckets,
        "documents": meta.get("documents", []),
        "document_dates": meta.get("document_dates", {}),
        "document_texts": meta.get("document_texts", {}),
        "audit": meta.get("audit", []),
        "reviews": meta.get("reviews", []),
        "boundary_events": meta.get("boundary_events", []),
    }
    return LiveMedCard.from_snapshot(snapshot, extractor)
