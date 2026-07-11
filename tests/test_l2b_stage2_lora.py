"""Тести LoRA-Stage 2 (FT MedGemma генерує KVT4). Обидва сервери мокаються."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

from livemedcard.dto import Document
from livemedcard.fhir import Patient, loinc_of
from livemedcard.layers import l2b_stage1, l2b_stage2_lora
from livemedcard.layers.l2b_extractor import LoraStage2Extractor
from livemedcard.pipeline import LiveMedCard

_RECEIVED = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Канонічний вихід LoRA-Stage 2: масив KVT4.
_KVT_JSON = json.dumps([
    {"cluster": "LABS", "keyword": "Creatinine", "value": "1.4 mg/dL", "timestamp": "2024-03-15"},
    {"cluster": "LABS", "keyword": "CGM Average Glucose", "value": "204 mg/dL",
     "timestamp": "March 16, 2025 - June 13, 2025"},
    {"cluster": "DEMOGRAPHICS", "keyword": "AGP Report Start Date", "value": "Yes",
     "timestamp": "March 16, 2025"},
])

_STAGE1_JSON = json.dumps({k: "not stated" for k in l2b_stage1.STAGE1_KEYS_9})


def _doc() -> Document:
    return Document(id="d1", text="raw", kind="lab_report", received_at=_RECEIVED)


# ── парсинг значень і дат ─────────────────────────────────────────────────────
def test_split_value():
    assert l2b_stage2_lora._split_value("204 mg/dL") == (204.0, "mg/dL")
    assert l2b_stage2_lora._split_value("34%") == (34.0, "%")
    assert l2b_stage2_lora._split_value("Yes") == (None, None)


def test_parse_timestamp_formats_and_range():
    p = l2b_stage2_lora._parse_timestamp
    assert p("2024-03-15", None) == (datetime(2024, 3, 15, tzinfo=timezone.utc), True)
    assert p("06/13/2025", None) == (datetime(2025, 6, 13, tzinfo=timezone.utc), True)
    assert p("March 16, 2025", None) == (datetime(2025, 3, 16, tzinfo=timezone.utc), True)
    # діапазон → кінець
    assert p("March 16, 2025 - June 13, 2025", None) == (
        datetime(2025, 6, 13, tzinfo=timezone.utc), True,
    )
    # дати в документі немає → fallback на дату завантаження, і це ПОЗНАЧЕНО
    assert p("not stated", _RECEIVED) == (_RECEIVED, False)
    assert p(None, _RECEIVED) == (_RECEIVED, False)


def test_fact_without_date_is_marked_not_stated():
    """AGP-звіт дає факти з timestamp=null. Датувати їх днем завантаження можна
    (ряду потрібна мітка), видавати цю дату за виміряну — ні."""
    from livemedcard.dto import Document

    doc = Document(id="agp", text="# AGP", kind="lab_report", received_at=_RECEIVED)
    kvt = [
        {"keyword": "Average Glucose", "value": "204", "unit": "mg/dL", "timestamp": None},
        {"keyword": "Creatinine", "value": "135", "unit": "µmol/L", "timestamp": "2024-10-15"},
    ]
    obs = l2b_stage2_lora.to_observations(kvt, doc, "mom-001", extracted_by="t")
    by_kw = {o.code.text: o for o in obs}
    assert by_kw["Average Glucose"].provenance.date_stated is False
    assert by_kw["Average Glucose"].effectiveDateTime == doc.received_at
    assert by_kw["Creatinine"].provenance.date_stated is True


def test_extract_json_array_strips_thought_tokens_and_fences():
    raw = "<unused95>thought\nlet me think\n```json\n" + _KVT_JSON + "\n```"
    facts = l2b_stage2_lora._extract_json_array(raw)
    assert len(facts) == 3
    assert facts[0]["keyword"] == "Creatinine"


# ── KVT4 → Observations ───────────────────────────────────────────────────────
def test_cgm_bridge_substring_match():
    """CGM-метрики з варіативним формулюванням мостяться через substring."""
    kvt = [
        {"keyword": "CGM Average Glucose", "value": "204 mg/dL", "timestamp": "not stated"},
        {"keyword": "Glucose Management Indicator (GMI)", "value": "8.2%", "timestamp": "not stated"},
        {"keyword": "CGM Glucose Variability", "value": "32.0%", "timestamp": "not stated"},
    ]
    obs = l2b_stage2_lora.to_observations(kvt, _doc(), "mom-001", extracted_by="test")
    by_kw = {o.code.text: o for o in obs}
    assert loinc_of(by_kw["CGM Average Glucose"]) == "97507-8"          # mean glucose (CGM)
    assert loinc_of(by_kw["Glucose Management Indicator (GMI)"]) == "97506-0"  # GMI
    assert loinc_of(by_kw["CGM Glucose Variability"]) is None           # свідомо не мапимо


def test_to_observations_bridge_and_freeform():
    kvt = json.loads(_KVT_JSON)
    obs = l2b_stage2_lora.to_observations(kvt, _doc(), "mom-001", extracted_by="test")
    # "Yes" (нечислове) пропущено → 2 обзервації
    assert len(obs) == 2
    by_kw = {o.code.text: o for o in obs}

    creat = by_kw["Creatinine"]
    assert loinc_of(creat) == "2160-0"  # міст спрацював
    assert creat.valueQuantity.value == 1.4
    assert creat.effectiveDateTime == datetime(2024, 3, 15, tzinfo=timezone.utc)

    glu = by_kw["CGM Average Glucose"]
    assert loinc_of(glu) == "97507-8"  # CGM mean glucose міст
    assert glu.valueQuantity.value == 204.0
    assert glu.effectiveDateTime == datetime(2025, 6, 13, tzinfo=timezone.utc)  # кінець періоду


# ── E2E через конвеєр (обидва сервери мокнуті) ────────────────────────────────
def test_lora_extractor_end_to_end():
    with patch.object(l2b_stage1, "chat_completions", return_value=_STAGE1_JSON), \
         patch.object(l2b_stage2_lora, "chat_completions", return_value=_KVT_JSON):
        card = LiveMedCard(Patient(id="mom-001", gender="female"), LoraStage2Extractor())
        card.ingest(_doc())

    # Структурована іпостась: 2 обзервації (LOINC-мостований креатинін у таймлайні).
    assert len(card.observations) == 2
    assert card.factstore.series("2160-0")[0].value == 1.4
    # Free-text іпостась збережено.
    assert len(card.document_references) == 1


def test_kvt4_unit_field_reaches_observation():
    """Хмарний Lab несе одиницю ОКРЕМИМ полем — вона не має губитися дорогою у FHIR."""
    doc = Document(id="d-unit", text="x", kind="lab_report")
    obs = l2b_stage2_lora.to_observations(
        [{"keyword": "Creatinine", "value": "135", "unit": "µmol/L",
          "timestamp": "2024-10-15"}],
        doc, "mom-001", extracted_by="test",
    )
    assert obs[0].valueQuantity.unit == "µmol/L"


def test_en_lab_keywords_bridge_to_loinc():
    """Формулювання зі справжнього EN-бланка мають мостити у LOINC, а не падати у free-form."""
    doc = Document(id="d-en", text="x", kind="lab_report")
    obs = l2b_stage2_lora.to_observations(
        [
            {"keyword": "Fasting glucose", "value": "6.8", "unit": "mmol/L",
             "timestamp": "2024-10-15"},
            {"keyword": "Hemoglobin A1c (HbA1c)", "value": "7.6", "unit": "%",
             "timestamp": "2024-10-15"},
        ],
        doc, "mom-001", extracted_by="test",
    )
    assert [loinc_of(o) for o in obs] == ["2345-7", "4548-4"]


def test_glycated_hemoglobin_does_not_collapse_into_hemoglobin():
    """Захист від колапсу лишається: «глікований гемоглобін» ≠ «гемоглобін» (718-7)."""
    doc = Document(id="d-hb", text="x", kind="lab_report")
    obs = l2b_stage2_lora.to_observations(
        [{"keyword": "Глікований гемоглобін", "value": "7.6 %", "timestamp": "2024-10-15"}],
        doc, "mom-001", extracted_by="test",
    )
    assert loinc_of(obs[0]) == "4548-4"
