"""Драйвер Stage 1 на PP-OCR: складання рядків із боксів і чесний вибір моделі.

Реального paddleocr тут немає — він опційна залежність, а перевіряти треба не
його, а те, що робимо ми: повертаємо назву показника і значення на один рядок
(без цього таблиця не доходить до Stage 2) і не приховуємо, що для кирилиці
працює інша модель, ніж заявлена.
"""
from __future__ import annotations

import base64
import sys
import types

import pytest
from fastapi.testclient import TestClient

import livemedcard.api as api_module
from livemedcard import ppocr
from livemedcard.dto import Document
from livemedcard.layers.l2b_extractor import deterministic_extract


def _box(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


# ── те, заради чого драйвер існує ──────────────────────────────────────────────
def test_row_assembly_puts_the_value_next_to_its_term():
    """Бокси приходять у порядку детекції, рядок треба зібрати назад.

    Кожен рядок таблиці — окремі бокси для назви, значення, одиниці й норми.
    Поки вони не на одному рядку, `_LAB_VALUE_RE` не бачить значення впритул до
    терміна, і показник не отримує LOINC.
    """
    polys = [_box(90, 300, 320, 330),    # назва другого рядка
             _box(380, 200, 430, 230),   # значення першого
             _box(90, 200, 320, 230),    # назва першого
             _box(380, 300, 430, 330)]   # значення другого
    texts = ["Калій", "135 µmol/L", "Креатинін", "5.1 mmol/L"]

    assert ppocr._rows_from_boxes(polys, texts) == (
        "Креатинін 135 µmol/L\nКалій 5.1 mmol/L")


def test_assembled_rows_reach_the_fact_store():
    """Наскрізна перевірка сенсу: зібрані рядки дають кодовані показники."""
    polys = [_box(90, 200, 320, 230), _box(380, 200, 430, 230),
             _box(90, 300, 320, 330), _box(380, 300, 430, 330)]
    # Русифіковано, як пише East-Slavic розпізнавач — згортання омоглифів у
    # `l2b_extractor` має дотягнути це до LOINC.
    texts = ["Креатинин", "135 µmol/L", "Калий", "5.1 mmol/L"]
    text = ppocr._rows_from_boxes(polys, texts)

    doc = Document(id="p", text=text, kind="lab_report")
    ext = deterministic_extract(text, doc, "pt")
    assert {o.code.coding[0].code: o.valueQuantity.value
            for o in ext.observations} == {"2160-0": 135.0, "2823-3": 5.1}


def test_boxes_on_the_same_line_are_ordered_left_to_right():
    polys = [_box(500, 100, 560, 130), _box(90, 100, 200, 130),
             _box(300, 105, 380, 128)]
    assert ppocr._rows_from_boxes(polys, ["третій", "перший", "другий"]) == \
        "перший другий третій"


# ── провенанс не має брехати про те, що працювало ──────────────────────────────
def test_cyrillic_falls_back_to_v5_recogniser_and_says_so():
    """У PP-OCRv6 кирилиці немає. Це має бути видно, а не підставлятись мовчки."""
    det, rec = ppocr.model_names("uk", "small")
    assert det == "PP-OCRv6_small_det"
    assert rec == "eslav_PP-OCRv5_mobile_rec"
    assert "PP-OCRv5" in ppocr.engine_id("uk", "small")


def test_latin_languages_use_v6_end_to_end():
    assert ppocr.model_names("en", "small") == (
        "PP-OCRv6_small_det", "PP-OCRv6_small_rec")


# ── поведінка без залежності й на смітті ───────────────────────────────────────
def test_missing_dependency_is_a_named_error_not_a_crash(monkeypatch):
    """API мусить віддати 503 «сервіс недоступний», а не 500."""
    ppocr._engine.cache_clear()
    monkeypatch.setitem(sys.modules, "paddleocr", None)
    with pytest.raises(ppocr.PpOcrError, match="не встановлено"):
        ppocr._engine("en", "small")
    ppocr._engine.cache_clear()


def test_broken_base64_is_rejected_before_the_engine_loads():
    with pytest.raises(ppocr.PpOcrError, match="base64"):
        ppocr.image_to_text("не-base64!!")


def test_image_to_text_assembles_pages(monkeypatch):
    class _Result:
        json = {"res": {"dt_polys": [_box(90, 10, 200, 40), _box(300, 10, 400, 40)],
                        "rec_texts": ["Калій", "5.1 mmol/L"]}}

    monkeypatch.setattr(ppocr, "_engine",
                        lambda lang, tier: types.SimpleNamespace(
                            predict=lambda path: [_Result()]))
    payload = base64.b64encode(b"\x89PNG fake").decode()
    assert ppocr.image_to_text(payload) == "Калій 5.1 mmol/L"


# ── маршрутизація в API ────────────────────────────────────────────────────────
def test_ingest_image_routes_to_ppocr(monkeypatch):
    monkeypatch.setattr(api_module, "IMAGE_OCR", "ppocr")
    monkeypatch.setattr(api_module.ppocr, "image_to_text",
                        lambda _b64: "Креатинін 135 µmol/L")
    api_module.PERSIST_PATH = ""
    client = TestClient(api_module.app)
    client.post("/reset")

    payload = base64.b64encode(b"\x89PNG fake").decode()
    resp = client.post("/ingest/image", json={"image_base64": payload,
                                              "kind": "lab_report"})
    assert resp.status_code == 200
    state = client.get("/state").json()
    assert "2160-0" in {s["loinc"] for s in state["series"]}
    assert state["photo_ocr"] == "local"      # PP-OCR працює на пристрої
    client.post("/reset")


def test_ppocr_failure_becomes_503(monkeypatch):
    monkeypatch.setattr(api_module, "IMAGE_OCR", "ppocr")

    def _fail(_b64):
        raise ppocr.PpOcrError("PP-OCR не встановлено")

    monkeypatch.setattr(api_module.ppocr, "image_to_text", _fail)
    client = TestClient(api_module.app)
    payload = base64.b64encode(b"\x89PNG fake").decode()
    resp = client.post("/ingest/image", json={"image_base64": payload,
                                              "kind": "lab_report"})
    assert resp.status_code == 503
