"""Тести детермінованого шару TRACE L2b (StubExtractor)."""
from __future__ import annotations

from datetime import datetime

from livemedcard.dto import Document
from livemedcard.fhir import loinc_of, rxnorm_of
from livemedcard.layers.l2b_extractor import StubExtractor, extract_documents
from livemedcard.reference import load_sample_docs

PATIENT_ID = "mom-001"


def _docs() -> dict[str, Document]:
    return {d["id"]: Document(**d) for d in load_sample_docs()}


def test_doc_2024_04_extracts_observations_medications_and_condition():
    docs = _docs()
    doc = docs["doc-2024-04"]
    result = StubExtractor().extract(doc, PATIENT_ID)

    creatinine = next(o for o in result.observations if loinc_of(o) == "2160-0")
    assert creatinine.valueQuantity.value == 121.0
    assert creatinine.provenance.source_doc_id == "doc-2024-04"

    aspirin = next(m for m in result.medications if rxnorm_of(m) == "1191")
    assert aspirin.dose_mg_per_day == 100
    assert aspirin.provenance.source_doc_id == "doc-2024-04"

    metformin = next(m for m in result.medications if rxnorm_of(m) == "6809")
    assert metformin.dose_mg_per_day == 3000
    assert metformin.provenance.source_doc_id == "doc-2024-04"

    hypertension = next(
        c for c in result.conditions if c.code.coding[0].code == "38341003"
    )
    assert hypertension.provenance.source_doc_id == "doc-2024-04"


def test_doc_2023_05_extracts_warfarin_and_atrial_fibrillation():
    docs = _docs()
    doc = docs["doc-2023-05"]
    result = StubExtractor().extract(doc, PATIENT_ID)

    warfarin = next(m for m in result.medications if rxnorm_of(m) == "11289")
    assert warfarin.provenance.source_doc_id == "doc-2023-05"

    afib = next(c for c in result.conditions if c.code.coding[0].code == "49436004")
    assert afib.provenance.source_doc_id == "doc-2023-05"


def test_extract_documents_merges_all_sample_docs():
    docs = [Document(**d) for d in load_sample_docs()]
    result = extract_documents(docs, PATIENT_ID)
    assert len(result.observations) >= 3


def test_lab_without_adjacent_value_does_not_steal_later_number():
    """Показник без власного числа не має «красти» значення наступного показника."""
    doc = Document(
        id="d-theft",
        text="Систолічний тиск у межах норми. Калій 4.9 mmol/L.",
        kind="lab_report",
        received_at=datetime(2024, 1, 1),
    )
    result = StubExtractor().extract(doc, PATIENT_ID)
    values = {loinc_of(o): o.valueQuantity.value for o in result.observations}
    assert "8480-6" not in values  # систолічний тиск НЕ сфабриковано
    assert values.get("2823-3") == 4.9  # калій розпізнано коректно


def test_drug_without_dose_does_not_steal_later_drugs_dose():
    """Ліки без власної дози не мають «красти» дозу наступного препарату."""
    doc = Document(
        id="d-dose",
        text="Призначено: варфарин. Аспірин 100 мг на добу.",
        kind="prescription",
        received_at=datetime(2024, 1, 1),
    )
    result = StubExtractor().extract(doc, PATIENT_ID)
    doses = {rxnorm_of(m): m.dose_mg_per_day for m in result.medications}
    assert doses.get("11289") is None  # варфарин — без дози
    assert doses.get("1191") == 100  # аспірин — коректна доза
