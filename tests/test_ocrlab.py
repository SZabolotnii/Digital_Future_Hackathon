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
    def __init__(
        self,
        md: str = "",
        facts=None,
        raise_on: str | None = None,
        image_facts=None,
    ) -> None:
        self._md = md
        self._facts = facts if facts is not None else []
        self._image_facts = image_facts if image_facts is not None else []
        self._raise = raise_on
        self.calls: list[str] = []

    def predict(self, *args, api_name=None):
        self.calls.append(api_name)
        if self._raise == api_name:
            raise RuntimeError("boom")
        if api_name == "/image_to_markdown":
            return self._md
        if api_name == "/extract_facts":
            return {"facts": self._facts}
        if api_name == "/facts_from_image":
            return {"facts": self._image_facts}
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


def test_extractor_falls_back_to_image_when_text_yields_no_facts(
    monkeypatch, fake_gradio_client
):
    """Обірвана транскрипція → нуль фактів із тексту → добираємо їх із самого фото."""
    fc = FakeClient(
        facts=[],  # markdown вийшов неповним — витягати нема з чого
        image_facts=[{"keyword": "Креатинін", "value": "135 мкмоль/л",
                      "timestamp": "2024-10-15"}],
    )
    _use(monkeypatch, fc)
    b64 = base64.b64encode(b"fake-jpeg-bytes").decode()
    doc = Document(id="ocr-fb", text="# AGP Report\n&nbsp;&nbsp;", kind="lab_report")

    ext = OcrLabExtractor(image_base64=b64).extract(doc, "mom-001")

    assert fc.calls == ["/extract_facts", "/facts_from_image"]  # спершу текст, тоді фото
    assert len(ext.observations) == 1
    assert ext.observations[0].valueQuantity.value == 135.0


def test_extractor_skips_image_fallback_when_text_yields_facts(monkeypatch):
    """Факти з тексту є → зайвого (і дорогого) проходу по зображенню не робимо."""
    fc = FakeClient(
        facts=[{"keyword": "Креатинін", "value": "135 мкмоль/л", "timestamp": "2024-10-15"}],
        image_facts=[{"keyword": "Калій", "value": "5.1 ммоль/л", "timestamp": "2024-10-15"}],
    )
    _use(monkeypatch, fc)
    b64 = base64.b64encode(b"fake-jpeg-bytes").decode()
    doc = Document(id="ocr-ok", text="Креатинін 135", kind="lab_report")

    ext = OcrLabExtractor(image_base64=b64).extract(doc, "mom-001")

    assert fc.calls == ["/extract_facts"]
    assert len(ext.observations) == 1


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


def test_mixed_naive_aware_timestamps_do_not_crash_series():
    """Регресія: сід із naive-мітками + факт із aware-міткою в одному LOINC-ряді.

    Без нормалізації ts у observation_to_kvt series() падав на порівнянні
    naive vs aware datetime (шлях stub-сід + хмарний OCR фото)."""
    import datetime as dt
    from livemedcard.fhir import CodeableConcept, Coding, Observation, Quantity, Sys
    from livemedcard.stores import FactStore

    def _obs(oid, value, when):
        return Observation(
            id=oid, status="final",
            code=CodeableConcept(coding=[Coding(system=Sys.LOINC, code="2160-0", display="Креатинін")], text="Креатинін"),
            effectiveDateTime=when,
            valueQuantity=Quantity(value=value, unit="µmol/L"),
        )

    store = FactStore()
    store.add_observations([
        _obs("naive", 88, dt.datetime(2023, 5, 1, 10, 0)),                       # naive (демо-семпл)
        _obs("aware", 135, dt.datetime(2024, 10, 15, tzinfo=dt.timezone.utc)),   # aware (екстрактор)
    ])
    series = store.series("2160-0")  # НЕ має кидати TypeError
    assert [f.value for f in series] == [88.0, 135.0]
    assert all(f.ts.tzinfo is not None for f in series)  # усе нормалізовано до aware


def test_ingest_image_uses_ocrlab_when_image_ocr_set(monkeypatch, fake_gradio_client):
    """IMAGE_OCR=ocrlab → /ingest/image робить реальний OCR через Lab,
    а текстовий/сід-шлях лишається детермінованим stub (розв'язка бекендів)."""
    from fastapi.testclient import TestClient
    import livemedcard.api as api_module

    fc = FakeClient(
        md="# Аналіз\nКреатинін 135 мкмоль/л 2024-10-15",
        facts=[{"keyword": "Креатинін", "value": "135 мкмоль/л", "timestamp": "2024-10-15"}],
    )
    _use(monkeypatch, fc)
    monkeypatch.setattr(api_module, "PERSIST_PATH", "")
    monkeypatch.setattr(api_module, "IMAGE_OCR", "ocrlab")

    c = TestClient(api_module.app)
    c.post("/reset")
    try:
        b64 = "data:image/jpeg;base64," + base64.b64encode(b"fake-jpeg").decode()
        r = c.post("/ingest/image", json={"image_base64": b64, "kind": "lab_report"})
        assert r.status_code == 200, r.text
        state = c.get("/state").json()
        # Факт із Lab потрапив у таймлайн з LOINC-мостом + провенансом
        creat = next(s for s in state["series"] if s["loinc"] == "2160-0")
        assert creat["points"][-1]["value"] == 135.0
        assert creat["points"][-1]["source"]  # провенанс збережено
    finally:
        c.post("/reset")


def test_cloud_extraction_is_marked_unverified_and_escalates(monkeypatch):
    """Хмарний витяг не має заявляти високу впевненість: калібрування немає, а
    обірваний OCR виглядає так само, як повний. Отже — «не перевірено» → до людини."""
    from livemedcard.economics import escalation_threshold
    from livemedcard.layers import l3_router

    fc = FakeClient(facts=[
        {"keyword": "Креатинін", "value": "135 мкмоль/л", "timestamp": "2024-10-15"},
    ])
    _use(monkeypatch, fc)
    doc = Document(id="ocr-conf", text="# md", kind="lab_report")
    ext = OcrLabExtractor().extract(doc, "mom-001")

    assert ext.mean_confidence < escalation_threshold("measurement")
    decision = l3_router.route([], ext.mean_confidence, "lab_report", hazard="measurement")
    assert decision.escalate is True
    assert decision.route_to == "L4_human"
