"""Тести двоетапного L2b: Stage 1 SGR-стиснення + детермінований Stage 2.

LLM мокається (як `test_ocr` мокає urllib), тож живий сервер не потрібен.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

from livemedcard.bundle import from_bundle, to_bundle
from livemedcard.dto import Document
from livemedcard.fhir import Patient
from livemedcard.layers import l2b_stage1
from livemedcard.layers.l2b_extractor import (
    StubExtractor,
    TwoStageExtractor,
    deterministic_extract,
)
from livemedcard.pipeline import LiveMedCard

_RECEIVED = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Канонічна «відповідь моделі» Stage 1: сирий JSON із датами на факт.
_MODEL_JSON = json.dumps({
    "DEMOGRAPHICS": "алергія: на пеніцилін",
    "VITALS": "not stated",
    "LABS": "креатинін: 120 (2024-03-15)\nкреатинін: 150 (2024-06-20)",
    "PROBLEMS": "not stated",
    "SYMPTOMS": "not stated",
    "MEDICATIONS": "аспірин: 100 мг",
    "PROCEDURES": "not stated",
    "UTILIZATION": "not stated",
    "DISPOSITION": "not stated",
}, ensure_ascii=False)


def _doc(text: str = "будь-що") -> Document:
    return Document(id="d1", text=text, kind="lab_report", received_at=_RECEIVED)


# ── Stage 1 парсинг/ремонт JSON ───────────────────────────────────────────────
def test_extract_json_object_repairs_truncated():
    truncated = '{"DEMOGRAPHICS": "x", "VITALS": "y"'  # без закритої дужки
    obj = l2b_stage1.extract_json_object(truncated)
    assert obj["DEMOGRAPHICS"] == "x"


def test_coerce_fills_missing_keys_and_flattens():
    summary = l2b_stage1.coerce_stage1_summary({"LABS": ["креатинін: 120"]})
    assert set(summary) == set(l2b_stage1.STAGE1_KEYS_9)
    assert summary["VITALS"] == "not stated"
    assert summary["LABS"] == "креатинін: 120"


# ── Stage 2: дати на факт ─────────────────────────────────────────────────────
def test_deterministic_extract_iso_date_per_fact():
    text = "## LABS\nкреатинін: 120 (2024-03-15)\nкреатинін: 150 (2024-06-20)\n"
    ext = deterministic_extract(text, _doc(), "mom-001")
    dates = sorted(o.effectiveDateTime for o in ext.observations)
    assert dates == [
        datetime(2024, 3, 15, tzinfo=timezone.utc),
        datetime(2024, 6, 20, tzinfo=timezone.utc),
    ]


def test_deterministic_extract_dotted_date():
    ext = deterministic_extract("креатинін: 88 15.03.2024", _doc(), "mom-001")
    assert ext.observations[0].effectiveDateTime == datetime(2024, 3, 15, tzinfo=timezone.utc)


def test_deterministic_extract_no_date_falls_back_to_received_at():
    ext = deterministic_extract("креатинін 95", _doc(), "mom-001")
    assert ext.observations[0].effectiveDateTime == _RECEIVED


def test_stub_extractor_unchanged_behaviour():
    """StubExtractor лишається тонкою обгорткою над deterministic_extract."""
    ext = StubExtractor().extract(_doc("креатинін 95"), "mom-001")
    assert ext.observations[0].effectiveDateTime == _RECEIVED
    assert ext.document_reference is None


# ── TwoStageExtractor E2E (мок LLM) ──────────────────────────────────────────
def test_two_stage_extractor_produces_both_hypostases():
    with patch.object(l2b_stage1, "chat_completions", return_value=_MODEL_JSON):
        ext = TwoStageExtractor().extract(_doc(), "mom-001")

    # Структурована іпостась: два датовані виміри креатиніну + аспірин.
    assert len(ext.observations) == 2
    assert {o.valueQuantity.value for o in ext.observations} == {120.0, 150.0}
    assert any(m.dose_mg_per_day == 100.0 for m in ext.medications)

    # Free-text іпостась: DocumentReference зі summary.
    dref = ext.document_reference
    assert dref is not None
    assert dref.id == "docref-d1"
    assert "креатинін: 120 (2024-03-15)" in dref.content.data
    assert dref.provenance.source_doc_id == "d1"


# ── Персистентність / Bundle round-trip із DocumentReference ──────────────────
def _card_with_docref() -> LiveMedCard:
    card = LiveMedCard(Patient(id="mom-001", gender="female"), TwoStageExtractor())
    with patch.object(l2b_stage1, "chat_completions", return_value=_MODEL_JSON):
        card.ingest(_doc())
    return card


def test_snapshot_roundtrip_preserves_docref():
    card = _card_with_docref()
    assert len(card.document_references) == 1
    restored = LiveMedCard.from_snapshot(card.snapshot())
    assert len(restored.document_references) == 1
    assert restored.document_references[0].content.data == card.document_references[0].content.data


def test_bundle_export_includes_docref_resource():
    card = _card_with_docref()
    bundle = to_bundle(card)
    rts = [e["resource"].get("resourceType") for e in bundle["entry"]]
    assert "DocumentReference" in rts
    restored = from_bundle(bundle)
    assert len(restored.document_references) == 1


# ── Q&A fallback на free-text іпостась ────────────────────────────────────────
def test_qa_freetext_fallback_uses_summary():
    from livemedcard.qa import QA

    card = _card_with_docref()
    qa = QA(card.factstore, card.graph, card.current_signals(), sex="female")
    # «алергія» немає у структурному роутингу → fallback у summary.
    ans = qa.ask("чи є алергія у пацієнта")
    assert "За текстом виписок" in ans.text
    assert "пеніцилін" in ans.text
    assert "d1" in ans.sources
