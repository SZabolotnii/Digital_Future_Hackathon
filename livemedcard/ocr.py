"""OCR Service Layer.

Uses local LM Studio API (with medgemma-1.5-4b-it or other loaded vision model)
to perform OCR on medical documents using base64 image data. HTTP-виклик
делеговано спільному `llm_client`; тут лишається лише vision-специфіка (формат
повідомлення із зображенням і промпт транскрипції).
"""
from __future__ import annotations

from .config import OCR_MODEL
from .llm_client import LLMError, chat_completions, detect_model


class OcrError(Exception):
    """Custom exception for OCR failures."""
    pass


_OCR_PROMPT = (
    "Transcribe all text from this image exactly as is, in its original language. "
    "Do not translate, and do not add any comments, introductions, notes, or explanations. "
    "Output only the transcribed text."
)


class OcrService:
    """Service to handle OCR operations with local LLM server."""

    @staticmethod
    def detect_model() -> str:
        """Query LM Studio to detect loaded model name, fallback to config."""
        return detect_model()

    @classmethod
    def perform_ocr(cls, base64_image: str) -> str:
        """Send base64 image to local vision LLM for transcription."""
        # Strip potential data URL prefix, e.g. "data:image/png;base64,"
        if "," in base64_image:
            base64_image = base64_image.split(",", 1)[1]

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _OCR_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        },
                    },
                ],
            }
        ]
        try:
            return chat_completions(
                messages,
                model=cls.detect_model() or OCR_MODEL,
                timeout=120.0,  # vision inference is slow
                service_name="LM Studio",
            )
        except LLMError as exc:
            raise OcrError(str(exc)) from exc
