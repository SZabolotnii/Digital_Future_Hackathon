"""Тести NuExtract-екстрактора (VLM). Ендпойнт мокається."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

from livemedcard.dto import Document
from livemedcard.fhir import Patient
from livemedcard.layers import l2b_nuextract
from livemedcard.layers.l2b_extractor import NuExtractExtractor
from livemedcard.pipeline import LiveMedCard

_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)
_FACTS = json.dumps({"facts": [
    {"keyword": "Creatinine", "value": "1.4 mg/dL", "timestamp": "2024-03-15"},
    {"keyword": "CGM Average Glucose", "value": "204 mg/dL", "timestamp": "not stated"},
    {"keyword": "Report Date", "value": "Yes", "timestamp": "2025-06-13"},
]})


def _doc() -> Document:
    return Document(id="d1", text="## LABS\nCreatinine 1.4", kind="lab_report", received_at=_TS)


def test_extract_facts_parses_facts_object():
    with patch.object(l2b_nuextract, "chat_completions", return_value=_FACTS):
        facts = l2b_nuextract.extract_facts("будь-що")
    assert len(facts) == 3
    assert facts[0]["keyword"] == "Creatinine"


def test_extract_facts_tolerates_wrapped_json():
    with patch.object(l2b_nuextract, "chat_completions", return_value="prefix\n" + _FACTS + "\ntrailing"):
        facts = l2b_nuextract.extract_facts("x")
    assert len(facts) == 3


def test_image_to_markdown_passes_markdown_mode():
    with patch.object(l2b_nuextract, "chat_completions", return_value="# Doc\ntable") as m:
        md = l2b_nuextract.image_to_markdown("data:image/png;base64,AAAA")
    assert md == "# Doc\ntable"
    # шаблон/режим передано через extra_body.chat_template_kwargs
    kwargs = m.call_args.kwargs
    assert kwargs["extra_body"]["chat_template_kwargs"]["mode"] == "markdown"


def test_nuextract_extractor_end_to_end():
    with patch.object(l2b_nuextract, "chat_completions", return_value=_FACTS):
        card = LiveMedCard(Patient(id="mom-001", gender="female"), NuExtractExtractor())
        card.ingest(_doc())

    # Creatinine → LOINC-мостований у таймлайн; "Yes" (нечислове) пропущено.
    assert len(card.observations) == 2
    assert card.factstore.series("2160-0")[0].value == 1.4
    # free-text іпостась (markdown/текст) збережено.
    assert len(card.document_references) == 1
    assert "Creatinine" in card.document_references[0].content.data
