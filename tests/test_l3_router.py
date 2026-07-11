"""Тести шару TRACE L3: політика роутингу/ескалації."""
from __future__ import annotations

from livemedcard.dto import Signal
from livemedcard.layers.l3_router import route


def _major_signal(confidence: float = 1.0) -> Signal:
    return Signal(
        layer="L1",
        kind="interaction",
        severity="major",
        detail="test major signal",
        subjects=["MedicationRequest/1"],
        confidence=confidence,
    )


def _moderate_signal(confidence: float = 1.0) -> Signal:
    return Signal(
        layer="L2a",
        kind="trend",
        severity="moderate",
        detail="test moderate signal",
        subjects=["Observation/1"],
        confidence=confidence,
    )


def test_major_signal_high_confidence_escalates():
    decision = route([_major_signal()], extraction_confidence=0.95, doc_kind="lab_report")
    assert decision.escalate is True
    assert decision.route_to == "L4_human"
    assert decision.reasons


def test_moderate_signal_high_confidence_does_not_escalate():
    major_decision = route(
        [_major_signal()], extraction_confidence=0.95, doc_kind="lab_report",
        hazard="measurement",
    )
    assert major_decision.escalate is True

    moderate_decision = route(
        [_moderate_signal()], extraction_confidence=0.95, doc_kind="lab_report",
        hazard="measurement",
    )
    assert moderate_decision.escalate is False
    assert moderate_decision.route_to == "auto"
    assert moderate_decision.reasons


def test_no_signals_low_confidence_escalates():
    decision = route([], extraction_confidence=0.5, doc_kind="note")
    assert decision.escalate is True
    assert decision.route_to == "L4_human"
    assert decision.reasons


def test_no_signals_high_confidence_does_not_escalate():
    decision = route([], extraction_confidence=0.95, doc_kind="note", hazard="measurement")
    assert decision.escalate is False
    assert decision.route_to == "auto"
    assert decision.reasons


def test_same_confidence_routes_differently_by_what_the_document_risks():
    """Один і той самий витяг 0.95: для документа з ліками цього мало, для
    документа лише з показниками — досить. Поріг не константа, а відношення
    вартостей: пропуск препарату коштує ADE-госпіталізації, пропуск тренду — ні."""
    from livemedcard.economics import escalation_threshold

    assert escalation_threshold("medication") > escalation_threshold("measurement")

    meds = route([], extraction_confidence=0.95, doc_kind="prescription", hazard="medication")
    labs = route([], extraction_confidence=0.95, doc_kind="lab_report", hazard="measurement")
    assert meds.escalate is True and meds.route_to == "L4_human"
    assert labs.escalate is False and labs.route_to == "auto"
    # причина називає поріг, а не ховає його
    assert any("0.95" in r and "0.97" in r for r in meds.reasons)


def test_reasons_english():
    decision = route(
        [_major_signal()], extraction_confidence=0.95, doc_kind="lab_report", lang="en"
    )
    assert decision.escalate is True
    assert any("human decision required" in r for r in decision.reasons)
    assert decision.reasons[-1] == "Document type: lab_report"
