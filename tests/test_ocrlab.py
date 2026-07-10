"""Тести хмарного OCR Lab-шляху (l2b_ocrlab + OcrLabExtractor).

Мокають gradio_client — реального Space/мережі не потребують. Перевіряють, що
межа збережена: Lab лише віддає raw KVT, а Observations/LOINC будує LiveMedCard.
"""
from __future__ import annotations

import base64
import sys
import types

import pytest

from livemedcard.dto import Document
from livemedcard.layers import l2b_ocrlab
from livemedcard.layers.l2b_extractor import OcrLabExtractor
from livemedcard.layers.l2b_ocrlab import OcrLabError, extract_facts


class FakeClient:
    def __init__(self, md: str = "", facts=None, raise_on: str | None = None) -> None:
        self._md = md
        self._facts = facts if facts is not None else []
        self._raise = raise_on

    def predict(self, *args, api_name=None):
        if self._raise == api_name:
            raise RuntimeError("boom")
        if api_name == "/image_to_markdown":
            return self._md
        if api_name == "/extract_facts":
            return {"facts": self._facts}
        raise AssertionError(f"unexpected api_name {api_name}")


@pytest.fixture
def fake_gradio_client(monkeypatch):
    """Інжектимо фейковий gradio_client (для handle_file у image_to_markdown)."""
    mod = types.ModuleType("gradio_client")
    mod.Client = lambda *a, **k: None
    mod.handle_file = lambda p: {"path": p}
    monkeypatch.setitem(sys.modules, "gradio_client", mod)
    getattr(l2b_ocrlab._client, "cache_clear", lambda: None)()
    yield mod
    getattr(l2b_ocrlab._client, "cache_clear", lambda: None)()


def _use(monkeypatch, client: FakeClient) -> None:
    monkeypatch.setattr(l2b_ocrlab, "_client", lambda space: client)


def test_extract_facts_filters_invalid(monkeypatch):
    fc = FakeClient(facts=[
        {"keyword": "Креатинін", "value": "135", "timestamp": "2024-10-15"},
        {"value": "no keyword"},   # без keyword → відкидається
        "not a dict",              # не dict → відкидається
    ])
    _use(monkeypatch, fc)
    facts = extract_facts("# markdown")
    assert facts == [{"keyword": "Креатинін", "value": "135", "timestamp": "2024-10-15"}]


def test_extract_facts_error_wraps(monkeypatch):
    _use(monkeypatch, FakeClient(raise_on="/extract_facts"))
    with pytest.raises(OcrLabError):
        extract_facts("# markdown")


def test_ocrlab_extractor_builds_observations(monkeypatch):
    """Lab віддає raw KVT → LiveMedCard будує Observation з LOINC-мостом сам."""
    fc = FakeClient(facts=[
        {"keyword": "Креатинін", "value": "135 мкмоль/л", "timestamp": "2024-10-15"},
        {"keyword": "Глікований гемоглобін (HbA1c)", "value": "7.6 %", "timestamp": "2024-10-15"},
    ])
    _use(monkeypatch, fc)
    doc = Document(id="ocr-1", text="# md", kind="lab_report")
    ext = OcrLabExtractor().extract(doc, "mom-001")

    assert len(ext.observations) == 2
    by_code = {o.code.coding[0].code: o for o in ext.observations}
    assert by_code["2160-0"].valueQuantity.value == 135.0        # креатинін
    assert by_code["4548-4"].valueQuantity.value == 7.6          # HbA1c → НЕ 718-7
    assert ext.document_reference is not None                     # free-text іпостась


def test_image_to_markdown(monkeypatch, fake_gradio_client):
    _use(monkeypatch, FakeClient(md="# Lab report\nCreatinine 135 µmol/L"))
    b64 = base64.b64encode(b"fake-jpeg-bytes").decode()
    md = l2b_ocrlab.image_to_markdown(b64)
    assert "Creatinine 135" in md


def test_image_to_markdown_error_wraps(monkeypatch, fake_gradio_client):
    _use(monkeypatch, FakeClient(raise_on="/image_to_markdown"))
    b64 = base64.b64encode(b"fake").decode()
    with pytest.raises(OcrLabError):
        l2b_ocrlab.image_to_markdown(b64)
