"""Спільний клієнт до локального OpenAI-сумісного сервера (LM Studio, llama.cpp).

Один канал доступу до on-device MedGemma для обох споживачів: OCR (`ocr.py`) і
Stage 1 SGR-стиснення (`layers/l2b_stage1.py`). Використовує лише stdlib
(`urllib`), щоб тести могли мокати `urllib.request.urlopen` так само, як для OCR.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import LMSTUDIO_URL, OCR_MODEL


class LLMError(Exception):
    """Помилка виклику локальної LLM (недоступний сервер, порожня відповідь тощо)."""


def detect_model(base_url: str | None = None) -> str:
    """Назва завантаженої моделі з `/models`; fallback на `OCR_MODEL`."""
    url = f"{(base_url or LMSTUDIO_URL).rstrip('/')}/models"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5.0) as response:
            data = json.loads(response.read().decode("utf-8"))
            if data.get("data"):
                model_id = data["data"][0].get("id")
                if model_id:
                    return model_id
    except Exception:
        pass
    return OCR_MODEL


def chat_completions(
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    timeout: float = 120.0,
    service_name: str = "локальною LLM",
    frequency_penalty: float | None = None,
    presence_penalty: float | None = None,
    base_url: str | None = None,
    extra_body: dict | None = None,
) -> str:
    """Викликати `/chat/completions` і повернути текст першого choice.

    `messages` — у форматі OpenAI (роль + content; content може бути рядком або
    списком частин для vision). `model=None` → автовизначення через `detect_model()`.
    `service_name` — назва сервісу у повідомленні про помилку з'єднання (напр.
    «LM Studio» для OCR). `frequency_penalty`/`presence_penalty` (коли задані) —
    придушення повторів (напр. зациклення на підписах осей при OCR інфографіки).
    `base_url` — override ендпойнта (напр. окремий llama.cpp-сервер LoRA-Stage 2).
    """
    endpoint = base_url or LMSTUDIO_URL
    model_name = model or detect_model(endpoint)
    url = f"{endpoint.rstrip('/')}/chat/completions"
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if frequency_penalty is not None:
        payload["frequency_penalty"] = frequency_penalty
    if presence_penalty is not None:
        payload["presence_penalty"] = presence_penalty
    if extra_body:  # напр. NuExtract chat_template_kwargs (template / mode)
        payload.update(extra_body)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            res_data = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise LLMError(
            f"Не вдалося з'єднатися з {service_name} за адресою {endpoint}. "
            f"Переконайтеся, що сервер запущено та завантажено модель. "
            f"Помилка: {exc.reason if hasattr(exc, 'reason') else exc}"
        ) from exc
    except Exception as exc:
        raise LLMError(f"Помилка виклику локальної LLM: {exc}") from exc

    choices = res_data.get("choices", [])
    if not choices:
        raise LLMError("LLM повернула порожню відповідь (choices порожній).")
    content = choices[0].get("message", {}).get("content", "")
    if not content:
        raise LLMError("У відповіді моделі відсутній вміст (content).")
    return content.strip()
