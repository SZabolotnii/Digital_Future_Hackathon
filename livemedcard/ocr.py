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
    "You transcribe a medical document image into plain text. Follow these rules:\n"
    "1. LANGUAGE: keep the original language. Do NOT translate. No comments, "
    "headings, or explanations — output only the transcription.\n"
    "2. VALUES ARE CRITICAL: transcribe every number, unit and date exactly "
    "(e.g. '204 mg/dL', '8.2%', '06/13/2025'). Never omit a value.\n"
    "3. PAIR LABELS WITH THEIR VALUES: this may be a multi-column report where a "
    "metric name and its value are separated horizontally or placed in different "
    "columns. Read the layout and write each on one line as 'Label: value unit' "
    "(e.g. 'Average Glucose: 204 mg/dL', 'Time CGM Active: 34%').\n"
    "4. CHARTS: for plots/graphs, transcribe axis titles and the legend ONCE. Do "
    "NOT repeat tick labels or time-axis labels (e.g. '12am 3am 6am'); write such a "
    "sequence at most once. Never loop or repeat a line.\n"
    "5. If a value is unreadable, write the label with '[unreadable]' — do NOT "
    "invent a number.\n"
    "Output the transcription now."
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
                # OCR мусить цілитись у КОНКРЕТНУ vision-модель (OCR_MODEL), а не в
                # «першу завантажену»: інакше text-only модель (напр. 27B) перехопить
                # запит і поверне 500 на зображення. Auto-detect тут недоречний.
                model=OCR_MODEL,
                max_tokens=4096,  # щільні звіти не влазять у 2048
                timeout=180.0,  # vision inference is slow
                service_name="LM Studio",
                frequency_penalty=0.8,  # проти зациклення на підписах осей
            )
        except LLMError as exc:
            raise OcrError(str(exc)) from exc
