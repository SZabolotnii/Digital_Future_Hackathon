"""Регресійні тести на виправлення знахідок code review (пункт 7)."""
from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from livemedcard.dto import Document, Signal
from livemedcard.fhir import (
    CodeableConcept,
    Coding,
    Condition,
    Observation,
    Patient,
    Quantity,
    Reference,
    Sys,
    loinc_of,
)
from livemedcard.layers.l2b_extractor import StubExtractor
from livemedcard.layers.l3_router import route
from livemedcard.pipeline import LiveMedCard
from livemedcard.qa import QA
from livemedcard.reference import load_range_specs, load_sample_docs
from livemedcard.stores import FactStore, KnowledgeGraph


def _patient() -> Patient:
    return Patient(id="mom-001", gender="female", birthDate=date(1950, 1, 1))


def _sample_card() -> LiveMedCard:
    card = LiveMedCard(_patient())
    card.ingest_all([Document(**d) for d in load_sample_docs()])
    return card


def _creat(oid: str, value: float, date_str: str) -> Observation:
    return Observation(
        id=oid,
        status="final",
        code=CodeableConcept(
            coding=[Coding(system=Sys.LOINC, code="2160-0", display="Creatinine")],
            text="Креатинін",
        ),
        effectiveDateTime=datetime.fromisoformat(date_str),
        valueQuantity=Quantity(value=value, unit="µmol/L"),
    )


# ── #3: документ без received_at не втрачає обзервації ──────────────────────────
def test_ingest_without_received_at_keeps_observation_and_flags_breach():
    card = LiveMedCard(_patient())
    rep = card.ingest(Document(id="d1", text="Креатинін 200 µmol/L.", kind="lab_report"))
    assert len(rep.extraction.observations) == 1
    assert len(card.factstore.series("2160-0")) == 1  # не зникло з факт-стору
    assert any(s.kind == "range_breach" for s in card.current_signals())


# ── #10: повторний прогін ідемпотентний ────────────────────────────────────────
def test_reingesting_samples_is_idempotent():
    card = _sample_card()
    n_facts = len(card.factstore.all_facts())
    n_meds = len(card.medications)
    n_signals = len(card.current_signals())
    card.ingest_all([Document(**d) for d in load_sample_docs()])  # повторно
    assert len(card.factstore.all_facts()) == n_facts
    assert len(card.medications) == n_meds
    assert len(card.current_signals()) == n_signals


# ── #7: повторні вимірювання в одному документі ────────────────────────────────
def test_repeated_measurement_in_one_document_captured():
    doc = Document(
        id="d",
        text="Креатинін 88 µmol/L вранці, креатинін 140 µmol/L увечері.",
        kind="lab_report",
        received_at=datetime(2024, 1, 1),
    )
    ext = StubExtractor().extract(doc, "mom-001")
    values = sorted(o.valueQuantity.value for o in ext.observations if loinc_of(o) == "2160-0")
    assert values == [88.0, 140.0]


# ── #5 + #6: порожній витяг → низька впевненість → ескалація ────────────────────
def test_empty_extraction_low_confidence_escalates():
    card = LiveMedCard(_patient())
    rep = card.ingest(
        Document(id="d", text="Пацієнтка почувається добре, скарг немає.",
                 kind="note", received_at=datetime(2024, 1, 1))
    )
    assert rep.extraction.mean_confidence == 0.0
    assert rep.decision.escalate is True


def test_major_signal_escalates_regardless_of_extraction_confidence():
    major = Signal(layer="L1", kind="interaction", severity="major",
                   detail="варфарин+аспірин", subjects=["m1", "m2"])
    dec = route([major], extraction_confidence=0.7, doc_kind="prescription")
    assert dec.escalate is True
    assert dec.route_to == "L4_human"
    # причина називає фактичну тяжкість, а не зашите "major"
    assert any("major" in r for r in dec.reasons)


# ── #4 (H): рівний крок не скасовує тренд ──────────────────────────────────────
def test_flat_step_does_not_cancel_rising_trend():
    from livemedcard.layers.l2a_trends import detect_trends

    store = FactStore()
    store.add_observations([
        _creat("o1", 80, "2023-01-01"),
        _creat("o2", 80, "2023-02-01"),  # рівний крок
        _creat("o3", 100, "2023-03-01"),
        _creat("o4", 120, "2023-04-01"),
    ])
    trends = detect_trends(store)
    assert len(trends) == 1
    assert "зростає" in trends[0].detail


# ── #12: Condition без text бере display, а не None ─────────────────────────────
def test_condition_without_text_uses_display_label():
    g = KnowledgeGraph()
    g.add_patient(_patient())
    c = Condition(
        id="c1",
        subject=Reference(reference="Patient/mom-001"),
        code=CodeableConcept(
            coding=[Coding(system=Sys.SNOMED, code="49436004", display="Atrial fibrillation")]
        ),
    )
    nid = g.add_condition(c, "mom-001")
    assert g.g.nodes[nid]["label"] == "Atrial fibrillation"


# ── #8: питання про ліки з лабораторною підстрокою → гілка ліків ────────────────
def test_medication_question_with_lab_substring_routes_to_meds():
    card = _sample_card()
    qa = QA(card.factstore, card.graph, card.current_signals())
    ans = qa.ask("Чи безпечно приймати ці ліки при підвищеному тиску?")
    assert "варфарин" in ans.text.lower()  # відповідь про ліки/взаємодії, не про тиск


# ── #9: звичайний «гемоглобін» не підміняється на HbA1c ────────────────────────
def test_plain_hemoglobin_not_mapped_to_hba1c():
    card = _sample_card()  # у картці є HbA1c 7.2%
    qa = QA(card.factstore, card.graph, card.current_signals())
    ans = qa.ask("Який гемоглобін?")
    # Не підміняємо аналіт: не повертаємо ані значення HbA1c, ані його джерела.
    assert "7.2" not in ans.text
    assert ans.sources == []


# ── #15: спільний довідник неможливо зіпсувати мутацією ────────────────────────
# (раніше захист — свіжі копії на кожен виклик; тепер сильніше: заморожені моделі
#  + кешований loader — мутація падає, а довідник лишається недоторканим)
def test_reference_data_is_immutable_and_cached():
    a = load_range_specs()
    with pytest.raises(ValidationError):
        a["2160-0"].default.high = 999
    with pytest.raises(TypeError):
        a["2160-0"] = None  # MappingProxyType: контейнер теж незмінний
    assert load_range_specs()["2160-0"].default.high != 999
    assert load_range_specs() is a  # кешовано: той самий обʼєкт, без перебудови
