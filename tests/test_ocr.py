"""Unit and integration tests for local-first OCR."""
from __future__ import annotations
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import livemedcard.api as api_module
from livemedcard.ocr import OcrError, OcrService


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(api_module, "PERSIST_PATH", "")
    c = TestClient(api_module.app)
    c.post("/reset")
    yield c
    c.post("/reset")


# ── Unit tests for OcrService ────────────────────────────────────────────────

def test_detect_model_success():
    """Verify that detect_model properly queries LM Studio API and retrieves model ID."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "data": [
            {"id": "medgemma-custom-model", "object": "model"}
        ]
    }).encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response):
        model = OcrService.detect_model()
        assert model == "medgemma-custom-model"


def test_detect_model_fallback_on_error():
    """Verify detect_model falls back to configured default model on connection failure."""
    with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
        model = OcrService.detect_model()
        # Default config fallback
        assert model == "medgemma-1.5-4b-it"


def test_perform_ocr_success():
    """Verify perform_ocr correctly parses base64 and returns model's response text."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Креатинін 95 µmol/L"
                }
            }
        ]
    }).encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    # Test with base64 prefix
    test_base64 = "data:image/jpeg;base64,iVBORw0KGgoAAAANS"
    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        text = OcrService.perform_ocr(test_base64)
        assert text == "Креатинін 95 µmol/L"

        # Verify that mock_urlopen was called and we stripped the data prefix
        args, kwargs = mock_urlopen.call_args
        req = args[0]
        # Check payload
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["messages"][0]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,iVBORw0KGgoAAAANS"


def test_perform_ocr_connection_error():
    """Verify perform_ocr raises OcrError when LM Studio is unreachable."""
    url_error = urllib.error.URLError("Connection refused")
    with patch("urllib.request.urlopen", side_effect=url_error):
        with pytest.raises(OcrError) as exc_info:
            OcrService.perform_ocr("base64data")
        assert "Не вдалося з'єднатися з LM Studio" in str(exc_info.value)


def test_perform_ocr_empty_choices():
    """Verify perform_ocr raises OcrError when LM Studio returns empty choices list."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "choices": []
    }).encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response):
        with pytest.raises(OcrError) as exc_info:
            OcrService.perform_ocr("base64data")
        assert "choices порожній" in str(exc_info.value)


# ── Integration tests for FastAPI endpoint ───────────────────────────────────

def test_api_ingest_image_success(client):
    """Verify that /ingest/image endpoint successfully triggers OCR and pipeline."""
    mock_ocr_text = "Креатинін 120 µmol/L.\nПризначено: аспірин 100 мг на добу."

    with patch.object(OcrService, "perform_ocr", return_value=mock_ocr_text) as mock_ocr:
        payload = {
            "image_base64": "data:image/png;base64,fakeimagecontent",
            "kind": "lab_report"
        }
        r = client.post("/ingest/image", json=payload)
        assert r.status_code == 200
        body = r.json()

        assert body["document_id"].startswith("doc-ocr-")
        assert len(body["signals"]) > 0  # Should trigger high creatinine warning and daily dose limits check

        # Verify OCR function was called with base64 image data
        mock_ocr.assert_called_once_with("data:image/png;base64,fakeimagecontent")

        # Verify fact store contains the data
        tl = client.get("/timeline/2160-0").json()
        assert len(tl) == 1
        assert tl[0]["value"] == 120.0


def test_api_ingest_image_service_unavailable(client):
    """Verify that /ingest/image handles OcrError with 503 HTTP status."""
    with patch.object(OcrService, "perform_ocr", side_effect=OcrError("LM Studio is down")):
        payload = {
            "image_base64": "fake",
            "kind": "note"
        }
        r = client.post("/ingest/image", json=payload)
        assert r.status_code == 503
        assert r.json()["detail"] == "LM Studio is down"


def test_api_ingest_image_disabled(client, monkeypatch):
    """FR-CNT-005: LMC_IMAGE_OCR=none → 503 без жодного виклику OCR."""
    monkeypatch.setattr(api_module, "IMAGE_OCR", "none")
    with patch.object(OcrService, "perform_ocr",
                      side_effect=AssertionError("OCR не мав викликатись")):
        r = client.post("/ingest/image", json={
            "image_base64": "data:image/png;base64,fakeimagecontent",
            "kind": "lab_report",
        })
    assert r.status_code == 503
    assert "вимкнено" in r.json()["detail"]
    assert client.get("/state").json()["photo_ocr"] == "none"


def test_api_ingest_image_empty_payload(client):
    """Verify that /ingest/image returns 422 on empty image payload."""
    payload = {
        "image_base64": "   ",
        "kind": "note"
    }
    r = client.post("/ingest/image", json=payload)
    assert r.status_code == 422
