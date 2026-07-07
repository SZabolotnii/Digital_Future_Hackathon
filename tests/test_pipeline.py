"""E2E-тест конвеєра: демо-документи → сигнали → рішення → Q&A."""
from datetime import date

from livemedcard.dto import Document
from livemedcard.fhir import Patient
from livemedcard.pipeline import LiveMedCard
from livemedcard.qa import QA
from livemedcard.reference import load_sample_docs


def _card_with_samples() -> LiveMedCard:
    patient = Patient(id="mom-001", gender="female", birthDate=date(1950, 1, 1))
    card = LiveMedCard(patient)
    card.ingest_all([Document(**d) for d in load_sample_docs()])
    return card


def test_creatinine_trend_and_breach_detected():
    card = _card_with_samples()
    signals = card.current_signals()
    kinds = {(s.layer, s.kind) for s in signals}
    assert ("L2a", "trend") in kinds  # креатинін росте 88→104→121
    assert ("L2a", "range_breach") in kinds  # 121 > 106


def test_warfarin_aspirin_interaction_detected():
    card = _card_with_samples()
    interactions = [s for s in card.current_signals() if s.kind == "interaction"]
    assert any(s.severity == "major" for s in interactions)


def test_high_risk_high_confidence_escalates():
    card = _card_with_samples()
    reports = card.ingest_all([Document(**d) for d in load_sample_docs()])
    # після повного таймлайну є major-сигнали за високої впевненості → ескалація
    assert any(r.decision.escalate for r in reports)


def test_qa_kidney_answer_has_sources():
    card = _card_with_samples()
    qa = QA(card.factstore, card.graph, card.current_signals())
    ans = qa.ask("Що з нирками?")
    assert "121" in ans.text
    assert ans.sources  # має посилання на документи-джерела


def test_qa_medications_lists_interaction():
    card = _card_with_samples()
    qa = QA(card.factstore, card.graph, card.current_signals())
    ans = qa.ask("Які ліки приймає і чи є взаємодії?")
    assert "Варфарин" in ans.text or "варфарин" in ans.text.lower()
