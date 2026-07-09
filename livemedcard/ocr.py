"""OCR Service Layer.

Uses local LM Studio API (with medgemma-1.5-4b-it or other loaded vision model)
to perform OCR on medical documents using base64 image data.
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error

from .config import LMSTUDIO_URL, OCR_MODEL


class OcrError(Exception):
    """Custom exception for OCR failures."""
    pass


class OcrService:
    """Service to handle OCR operations with local LLM server."""

    @staticmethod
    def detect_model() -> str:
        """Query LM Studio to detect loaded model name, fallback to config."""
        url = f"{LMSTUDIO_URL.rstrip('/')}/models"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5.0) as response:
                data = json.loads(response.read().decode("utf-8"))
                if "data" in data and len(data["data"]) > 0:
                    model_id = data["data"][0].get("id")
                    if model_id:
                        return model_id
        except Exception:
            # Silence and fallback to default configured model
            pass
        return OCR_MODEL

    @classmethod
    def perform_ocr(cls, base64_image: str) -> str:
        """Send base64 image to local vision LLM for transcription."""
        # Strip potential data URL prefix, e.g. "data:image/png;base64,"
        if "," in base64_image:
            base64_image = base64_image.split(",", 1)[1]

        model_name = cls.detect_model()
        url = f"{LMSTUDIO_URL.rstrip('/')}/chat/completions"

        prompt = (
            "Transcribe all text from this image exactly as is, in its original language. "
            "Do not translate, and do not add any comments, introductions, notes, or explanations. "
            "Output only the transcribed text."
        )

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                          "type": "image_url",
                          "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                          }
                        }
                    ]
                }
            ],
            "temperature": 0.0,
            "max_tokens": 2048
        }

        req_body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=req_body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        try:
            # Vision inference can take some time, set a higher timeout (e.g. 120.0s)
            with urllib.request.urlopen(req, timeout=120.0) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                choices = res_data.get("choices", [])
                if not choices:
                    raise OcrError("LM Studio повернув порожню відповідь (choices порожній).")
                content = choices[0].get("message", {}).get("content", "")
                if not content:
                    raise OcrError("У відповіді моделі відсутній вміст (content).")
                return content.strip()
        except urllib.error.URLError as exc:
            raise OcrError(
                f"Не вдалося з'єднатися з LM Studio за адресою {LMSTUDIO_URL}. "
                f"Переконайтеся, що сервер запущено та завантажено модель з підтримкою Vision. "
                f"Помилка: {exc.reason if hasattr(exc, 'reason') else exc}"
            )
        except Exception as exc:
            if not isinstance(exc, OcrError):
                raise OcrError(f"Помилка під час виконання OCR: {exc}")
            raise
