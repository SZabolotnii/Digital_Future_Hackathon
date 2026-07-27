"""Регресійні тести на знахідки зовнішнього архітектурного звіту (livemedcard.html):
стабільні id ресурсів, кешовані незмінні довідники, типізований аудит, QA-fallback,
помилкові входи Bundle/персистентності, ізоляція індексу FactStore."""
from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from livemedcard.bundle import from_bundle
from livemedcard.config import STUB_CONFIDENCE
from livemedcard.dto import AuditEntry, Document
from livemedcard.fhir import Patient
from livemedcard.layers.l2b_extractor import StubExtractor
from livemedcard.pipeline import LiveMedCard
from livemedcard.qa import QA
from livemedcard.reference import (
    load_dose_limits,
    load_interactions,
    load_sample_docs,
    load_vocab,
)
from livemedcard.repository import load_card
from livemedcard.stores import FactStore, KnowledgeGraph


def _patient() -> Patient:
    return Patient(id="mom-001", gender="female", birthDate=date(1950, 1, 1))


def _sample_card() -> LiveMedCard:
    card = LiveMedCard(_patient())
    card.ingest_all([Document(**d) for d in load_sample_docs()])
    return card


# ── стабільні детерміновані id ресурсів ────────────────────────────────────────
def test_resource_ids_stable_across_reextraction():
    doc = Document(
        id="d",
        text="Креатинін 121 µmol/L. Калій 5.9 mmol/L. "
             "Аспірин 100 мг. Фібриляція передсердь.",
        kind="discharge_summary",
        received_at=datetime(2024, 1, 1),
    )
    r1 = StubExtractor().extract(doc, "p1")
    r2 = StubExtractor().extract(doc, "p1")
    assert [o.id for o in r1.observations] == [o.id for o in r2.observations]
    assert [m.id for m in r1.medications] == [m.id for m in r2.medications]
    assert [c.id for c in r1.conditions] == [c.id for c in r2.conditions]
    assert all(o.id.startswith("obs-") for o in r1.observations)
    assert len({o.id for o in r1.observations}) == 2  # різний зміст → різні id


def test_identical_repeated_mention_is_single_fact():
    doc = Document(
        id="d",
        text="Креатинін 88 µmol/L вранці, креатинін 88 µmol/L увечері.",
        kind="lab_report",
        received_at=datetime(2024, 1, 1),
    )
    res = StubExtractor().extract(doc, "p1")
    assert len(res.observations) == 1  # той самий зміст → той самий факт


def test_different_values_in_one_doc_get_distinct_ids():
    doc = Document(
        id="d",
        text="Креатинін 88 µmol/L вранці, креатинін 140 µmol/L увечері.",
        kind="lab_report",
        received_at=datetime(2024, 1, 1),
    )
    res = StubExtractor().extract(doc, "p1")
    assert len({o.id for o in res.observations}) == 2


def test_document_confidence_is_measured_not_configured():
    """Раніше тут перевірялось, що впевненість береться з конфігу. Тепер вимога
    сильніша: вона береться з ВИМІРЮВАННЯ на gold-корпусі (нижня межа Вілсона),
    а константа лишається відкатом на випадок, коли калібрування не проганяли."""
    from livemedcard.calibration import confidence_for

    doc = Document(id="d", text="Креатинін 100 µmol/L.", kind="lab_report",
                   received_at=datetime(2024, 1, 1))
    res = StubExtractor().extract(doc, "p1")
    assert res.observations[0].provenance.confidence == STUB_CONFIDENCE

    measured = confidence_for("StubExtractor")
    assert res.mean_confidence == (
        measured if measured is not None else STUB_CONFIDENCE
    )
    # Виміряне число нижче за призначене: корпус ловить пропуски, яких константа
    # не знала. Якщо колись стане навпаки — це привід перевірити корпус.
    if measured is not None:
        assert measured < STUB_CONFIDENCE


# ── кешовані незмінні довідники ────────────────────────────────────────────────
def test_reference_loaders_cached_single_instance():
    assert load_vocab() is load_vocab()
    assert load_interactions() is load_interactions()
    assert isinstance(load_interactions(), tuple)


def test_vocab_and_limits_are_immutable():
    with pytest.raises(ValidationError):
        load_vocab().labs["креатинін"].loinc = "0000-0"
    with pytest.raises(TypeError):
        load_dose_limits()["x"] = None  # MappingProxyType


# ── типізований аудит ──────────────────────────────────────────────────────────
def test_audit_snapshot_roundtrip_and_validation():
    card = _sample_card()
    snap = card.snapshot()
    restored = LiveMedCard.from_snapshot(snap)
    assert restored.audit_log() == card.audit_log()

    bad = dict(snap)
    bad["audit"] = [{"ts": "2024-01-01T00:00:00Z", "document_id": "d"}]  # неповний
    with pytest.raises(ValidationError):
        LiveMedCard.from_snapshot(bad)


def test_audit_entry_rejects_unknown_route():
    with pytest.raises(ValidationError):
        AuditEntry(ts=datetime(2024, 1, 1), document_id="d", kind="note",
                   extraction_confidence=1.0, n_signals=0,
                   route_to="somewhere", escalate=False)


# ── QA: явний fallback замість мовчазної порожнечі ─────────────────────────────
def test_qa_unknown_question_falls_back_explicitly():
    card = _sample_card()
    qa = QA(card.factstore, card.graph, card.current_signals(), sex="female")
    ans = qa.ask("Скільки буде два плюс два?")
    assert "Не вдалося зіставити" in ans.text
    assert ans.sources == []


def test_qa_lab_question_on_empty_card():
    qa = QA(FactStore(), KnowledgeGraph(), [], sex="female")
    ans = qa.ask("Що з нирками?")
    assert "немає вимірювань" in ans.text
    assert ans.sources == []


# ── помилкові входи Bundle / персистентності ───────────────────────────────────
def test_bundle_without_patient_raises():
    with pytest.raises(ValueError):
        from_bundle({"resourceType": "Bundle", "entry": []})


def test_corrupted_snapshot_file_raises(tmp_path):
    p = tmp_path / "card.json"
    p.write_text("{ це не json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_card(str(p))


# ── FactStore: викликач не може зіпсувати індекс ───────────────────────────────
def test_series_returns_isolated_copy():
    card = _sample_card()
    s = card.factstore.series("2160-0")
    assert s
    s.clear()  # мутація копії викликачем
    assert card.factstore.series("2160-0")  # індекс неушкоджений
