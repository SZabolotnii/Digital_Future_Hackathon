"""L2b через NuExtract3 (VLM structured extraction) — один рушій, дві іпостасі.

NuExtract читає документ (текст/зображення) + JSON-шаблон і повертає структурований
JSON; окремо вміє image→markdown. Використовуємо двоетапно (за результатами бенчмарку
найточніше): Stage 1 image→markdown (розширена іпостась), Stage 2 markdown→facts
(стисла). Шаблон передається через `chat_template_kwargs` (llama-server --jinja / vLLM).

Не медична модель → кодування в LOINC робимо мостом (`l2b_stage2_lora`).
"""
from __future__ import annotations

import json

from ..config import NUEXTRACT_MODEL, NUEXTRACT_URL
from ..llm_client import chat_completions

# Шаблон KVT-фактів (leaf-значення = типи NuExtract).
_FACTS_TEMPLATE = {
    "facts": [
        {"keyword": "verbatim-string", "value": "verbatim-string", "timestamp": "verbatim-string"}
    ]
}
_TEMPLATE_JSON = json.dumps(_FACTS_TEMPLATE, indent=2)


def _ctk(**kw) -> dict:
    return {"chat_template_kwargs": {"enable_thinking": False, **kw}}


def image_to_markdown(base64_image: str, *, url: str | None = None, model: str | None = None) -> str:
    """Stage 1: зображення → markdown (розширена іпостась). mode=markdown."""
    if "," in base64_image:
        base64_image = base64_image.split(",", 1)[1]
    messages = [{
        "role": "user",
        "content": [{"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}],
    }]
    return chat_completions(
        messages, model=model or NUEXTRACT_MODEL, base_url=url or NUEXTRACT_URL,
        temperature=0.0, max_tokens=4096, timeout=180.0,
        service_name="NuExtract", extra_body=_ctk(mode="markdown"),
    )


def extract_facts(text: str, *, url: str | None = None, model: str | None = None) -> list[dict]:
    """Stage 2: текст/markdown → KVT-факти за шаблоном. Повертає [{keyword,value,timestamp}]."""
    messages = [{"role": "user", "content": [{"type": "text", "text": text}]}]
    raw = chat_completions(
        messages, model=model or NUEXTRACT_MODEL, base_url=url or NUEXTRACT_URL,
        temperature=0.0, max_tokens=3072, timeout=120.0,
        service_name="NuExtract", extra_body=_ctk(template=_TEMPLATE_JSON),
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return []
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return []
    facts = data.get("facts", []) if isinstance(data, dict) else data
    return [f for f in facts if isinstance(f, dict) and f.get("keyword")]
