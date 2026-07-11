"""Тести детермінованого шару TRACE L1 (взаємодії ліків, межі доз)."""
from __future__ import annotations

from livemedcard.fhir import CodeableConcept, Coding, MedicationRequest, Reference, Sys
from livemedcard.layers.l1_safety import check_dose_limits, check_interactions, run

SUBJECT = Reference(reference="Patient/p1")


def _med(id: str, rxnorm: str, display: str, dose_mg_per_day: float | None = None) -> MedicationRequest:
    return MedicationRequest(
        id=id,
        status="active",
        intent="order",
        medicationCodeableConcept=CodeableConcept(
            coding=[Coding(system=Sys.RXNORM, code=rxnorm, display=display)]
        ),
        subject=SUBJECT,
        dose_mg_per_day=dose_mg_per_day,
    )


def test_interaction_warfarin_aspirin():
    warfarin = _med("m1", "11289", "Warfarin")
    aspirin = _med("m2", "1191", "Aspirin")
    signals = check_interactions([warfarin, aspirin])
    assert len(signals) == 1
    signal = signals[0]
    assert signal.layer == "L1"
    assert signal.kind == "interaction"
    assert signal.severity == "major"
    assert signal.subjects == ["MedicationRequest/m1", "MedicationRequest/m2"]
    assert signal.confidence == 1.0


def test_interaction_lone_drug_no_signal():
    warfarin = _med("m1", "11289", "Warfarin")
    signals = check_interactions([warfarin])
    assert signals == []


def test_dose_limit_metformin_exceeded():
    metformin = _med("m1", "6809", "Metformin", dose_mg_per_day=3000)
    signals = check_dose_limits([metformin])
    assert len(signals) == 1
    signal = signals[0]
    assert signal.layer == "L1"
    assert signal.kind == "dose_limit"
    assert signal.severity == "major"
    assert signal.subjects == ["MedicationRequest/m1"]
    assert signal.confidence == 1.0


def test_dose_limit_metformin_within_limit():
    metformin = _med("m1", "6809", "Metformin", dose_mg_per_day=2000)
    signals = check_dose_limits([metformin])
    assert signals == []


def test_run_combines_both_checks():
    warfarin = _med("m1", "11289", "Warfarin")
    aspirin = _med("m2", "1191", "Aspirin")
    metformin = _med("m3", "6809", "Metformin", dose_mg_per_day=3000)
    signals = run([warfarin, aspirin, metformin])
    kinds = [s.kind for s in signals]
    assert kinds == ["interaction", "dose_limit"]


def test_interaction_english_detail():
    """lang='en' → механізм із detail_en, назви — з display coding (EN)."""
    warfarin = _med("m1", "11289", "Warfarin")
    aspirin = _med("m2", "1191", "Aspirin")
    signals = check_interactions([warfarin, aspirin], lang="en")
    assert len(signals) == 1
    assert signals[0].detail == (
        "Warfarin + Aspirin: additive antithrombotic effect, increased bleeding risk"
    )


def test_dose_limit_english_detail():
    metformin = _med("m1", "6809", "Metformin", dose_mg_per_day=3000)
    signals = check_dose_limits([metformin], lang="en")
    assert len(signals) == 1
    assert signals[0].detail == (
        "Metformin: daily dose 3000 mg exceeds the daily limit of 2550 mg"
    )
