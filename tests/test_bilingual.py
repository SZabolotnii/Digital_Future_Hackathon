"""Тести білінгвального режиму (UA/EN) з авто-вибором мови."""
from __future__ import annotations

from datetime import datetime, timezone

from livemedcard.dto import Document
from livemedcard.fhir import Patient, loinc_of
from livemedcard.lang import detect_lang
from livemedcard.layers.l2b_extractor import deterministic_extract
from livemedcard.pipeline import LiveMedCard
from livemedcard.qa import QA

_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_detect_lang():
    assert detect_lang("Що з нирками?") == "uk"
    assert detect_lang("What about kidneys?") == "en"
    assert detect_lang("12345") == "uk"          # без літер → дефолт
    assert detect_lang("12345", default="en") == "en"


def test_english_vocab_extraction():
    """Білінгвальний vocab: англ. glucose/creatinine тепер витягуються."""
    doc = Document(id="d1", text="Glucose 6.5 Creatinine 120", received_at=_TS)
    ext = deterministic_extract(doc.text, doc, "p1")
    loincs = {loinc_of(o) for o in ext.observations}
    assert "2345-7" in loincs  # glucose (раніше відсутній)
    assert "2160-0" in loincs  # creatinine


def _card_en() -> LiveMedCard:
    card = LiveMedCard(Patient(id="mom-001", gender="female"))
    card.ingest(Document(
        id="d1", text="Creatinine 120. Aspirin 100 mg. Warfarin 5 mg.", received_at=_TS,
    ))
    return card


def test_qa_english_lab_answers_in_english():
    card = _card_en()
    qa = QA(card.factstore, card.graph, card.current_signals(), sex="female")
    ans = qa.ask("What is my creatinine?")
    assert "latest value" in ans.text  # EN-шаблон
    assert "120" in ans.text


def test_qa_english_answer_has_no_ukrainian_metric_name():
    """EN-відповідь не має починатися українською назвою показника з довідника."""
    card = _card_en()
    qa = QA(card.factstore, card.graph, card.current_signals(), sex="female")
    ans = qa.ask("What is my creatinine?")
    assert ans.text.startswith("Creatinine")
    assert not any("Ѐ" <= ch <= "ӿ" for ch in ans.text)


def test_qa_english_meds_and_interactions():
    card = _card_en()
    qa = QA(card.factstore, card.graph, card.current_signals(), sex="female")
    ans = qa.ask("What medications and interactions do I have?")
    assert "Prescribed medications" in ans.text
    assert "⚠" in ans.text  # warfarin+aspirin взаємодія


def test_qa_ukrainian_still_answers_in_ukrainian():
    card = _card_en()
    qa = QA(card.factstore, card.graph, card.current_signals(), sex="female")
    ans = qa.ask("Що з креатиніном?")
    assert "останнє значення" in ans.text
