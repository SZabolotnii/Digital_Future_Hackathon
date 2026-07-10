"""Тести архітектурних покращень: типізовані довідники, стать, доза×кратність,
FHIR Bundle, персистентність, аудит/метрики."""
from __future__ import annotations

from datetime import date, datetime

from livemedcard.bundle import from_bundle, to_bundle
from livemedcard.dto import Document
from livemedcard.fhir import Patient, rxnorm_of
from livemedcard.layers.l2b_extractor import StubExtractor
from livemedcard.pipeline import LiveMedCard
from livemedcard.reference import (
    load_dose_limits,
    load_interactions,
    load_vocab,
    range_for,
)
from livemedcard.repository import load_card, save_card


def _patient() -> Patient:
    return Patient(id="mom-001", gender="female", birthDate=date(1950, 1, 1))


def _sample_card() -> LiveMedCard:
    from livemedcard.reference import load_sample_docs

    card = LiveMedCard(_patient())
    card.ingest_all([Document(**d) for d in load_sample_docs()])
    return card


# ── типізовані довідники ───────────────────────────────────────────────────────
def test_typed_reference_models():
    inter = load_interactions()
    assert inter and hasattr(inter[0], "severity") and hasattr(inter[0], "pair")
    limits = load_dose_limits()
    assert limits["6809"].max_daily == 2550
    vocab = load_vocab()
    assert vocab.labs["креатинін"].loinc == "2160-0"


# ── референс-межі за статтю ─────────────────────────────────────────────────────
def test_sex_specific_reference_range():
    female = range_for("2160-0", "female")
    male = range_for("2160-0", "male")
    assert female.high == 90 and male.high == 106
    default = range_for("2160-0", None)
    assert default.high == 106  # без статі — загальні межі


def test_female_creatinine_breach_uses_sex_range():
    # 100 у нормі за загальними (44–106), але вище жіночої межі (44–90)
    card = LiveMedCard(_patient())
    card.ingest(Document(id="d", text="Креатинін 100 µmol/L.", kind="lab_report",
                         received_at=datetime(2024, 1, 1)))
    assert any(s.kind == "range_breach" for s in card.current_signals())


# ── доза × кратність ────────────────────────────────────────────────────────────
def test_dose_times_frequency():
    doc = Document(id="d", text="Аспірин 100 мг двічі на добу.", kind="prescription",
                   received_at=datetime(2024, 1, 1))
    res = StubExtractor().extract(doc, "p1")
    aspirin = next(m for m in res.medications if rxnorm_of(m) == "1191")
    assert aspirin.dose_mg_per_day == 200  # 100 × 2


def test_dose_default_frequency_once():
    doc = Document(id="d", text="Аспірин 100 мг на добу.", kind="prescription",
                   received_at=datetime(2024, 1, 1))
    res = StubExtractor().extract(doc, "p1")
    aspirin = next(m for m in res.medications if rxnorm_of(m) == "1191")
    assert aspirin.dose_mg_per_day == 100


# ── FHIR Bundle round-trip ──────────────────────────────────────────────────────
def test_bundle_round_trip_preserves_state():
    card = _sample_card()
    bundle = to_bundle(card)
    assert bundle["resourceType"] == "Bundle"
    assert any(e["resource"]["resourceType"] == "Patient" for e in bundle["entry"])

    restored = from_bundle(bundle)
    assert restored.document_count == card.document_count
    assert len(restored.medications) == len(card.medications)
    # сигнали безпеки відтворюються ідентично
    before = {(s.layer, s.kind, s.detail) for s in card.current_signals()}
    after = {(s.layer, s.kind, s.detail) for s in restored.current_signals()}
    assert before == after


# ── персистентність ────────────────────────────────────────────────────────────
def test_persistence_save_load(tmp_path):
    card = _sample_card()
    path = str(tmp_path / "card.json")
    save_card(card, path)
    loaded = load_card(path)
    assert loaded is not None
    assert loaded.document_count == card.document_count
    assert {m.id for m in loaded.medications} == {m.id for m in card.medications}


def test_load_missing_file_returns_none(tmp_path):
    assert load_card(str(tmp_path / "nope.json")) is None


# ── аудит ───────────────────────────────────────────────────────────────────────
def test_audit_log_records_decisions():
    card = _sample_card()
    log = card.audit_log()
    assert len(log) == 4  # чотири документи (UA×3 + EN)
    assert all("route_to" in a and "escalate" in a for a in log)
    assert any(a["escalate"] for a in log)
