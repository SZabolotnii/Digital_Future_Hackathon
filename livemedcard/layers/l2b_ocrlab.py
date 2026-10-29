"""L2b через хмарний OCR Lab (ZeroGPU NuExtract3) — opt-in cloud-шлях.

Дзеркалить `l2b_nuextract`, але транспорт — `gradio_client` до окремого Space
(hackathon/Spec.md FR-MODEL-004: різниця лише в транспорті, контракт/шаблон/normalizer ті
самі). Local-first лишається дефолтом; цей шлях вмикається ЯВНО
(`LMC_EXTRACTOR=ocrlab`) і надсилає зображення у хмару HF — тож призначений лише
для **синтетичних / повністю деідентифікованих** документів.

Межа збережена: Lab лише витягує факти (markdown + raw KVT), а Observations і
сигнали безпеки рахує сам LiveMedCard (`to_observations` + детермінований FactStore).
`gradio_client` — опційна залежність (extra `cloud`); імпортується ліниво, щоб
пакет лишався імпортовним без неї.
"""
from __future__ import annotations

import base64
import os
import tempfile
from functools import lru_cache

from ..config import OCRLAB_SPACE, OCRLAB_TOKEN


class OcrLabError(RuntimeError):
    """Помилка виклику хмарного OCR Lab (недоступний / таймаут / невалідна відповідь)."""


@lru_cache(maxsize=4)
def _client(space: str):
    try:
        from gradio_client import Client
    except ImportError as exc:  # pragma: no cover - лише коли не встановлено extra
        raise OcrLabError(
            "gradio_client не встановлено — постав extra 'cloud' для хмарного OCR Lab."
        ) from exc
    token = OCRLAB_TOKEN or None
    # Назва kwarg токена змінювалася між версіями (`token` / `hf_token`); Space
    # публічний, тож зрештою можна й без токена. Пробуємо сумісні варіанти.
    last: Exception | None = None
    for kwargs in ({"token": token}, {"hf_token": token}, {}):
        try:
            return Client(space, **kwargs)
        except TypeError as exc:  # несумісна назва kwarg — пробуємо наступну
            last = exc
        except Exception as exc:  # мережа/недоступність — це вже не про kwarg
            raise OcrLabError(f"Не вдалося під'єднатися до OCR Lab '{space}': {exc}") from exc
    raise OcrLabError(f"Не вдалося під'єднатися до OCR Lab '{space}': {last}")


def _facts_of(result) -> list[dict]:
    """Спільний розбір відповіді Lab: {"facts": [...]} або голий список."""
    facts = result.get("facts", []) if isinstance(result, dict) else result
    if not isinstance(facts, list):
        return []
    return [f for f in facts if isinstance(f, dict) and f.get("keyword")]


def _decode_to_tempfile(image_base64: str) -> str:
    if "," in image_base64:
        image_base64 = image_base64.split(",", 1)[1]
    fd, path = tempfile.mkstemp(suffix=".jpg")
    with os.fdopen(fd, "wb") as f:
        f.write(base64.b64decode(image_base64))
    return path


def image_to_markdown(image_base64: str, *, space: str | None = None) -> str:
    """Stage 1: зображення → markdown (через ендпойнт /image_to_markdown Lab-Space)."""
    try:
        from gradio_client import handle_file
    except ImportError as exc:  # pragma: no cover
        raise OcrLabError(
            "gradio_client не встановлено — постав extra 'cloud' для хмарного OCR Lab."
        ) from exc
    client = _client(space or OCRLAB_SPACE)
    path = _decode_to_tempfile(image_base64)
    try:
        result = client.predict(handle_file(path), api_name="/image_to_markdown")
    except Exception as exc:
        raise OcrLabError(f"OCR Lab image_to_markdown недоступний: {exc}") from exc
    finally:
        os.unlink(path)
    return str(result or "").strip()


def facts_from_image(image_base64: str, *, space: str | None = None) -> list[dict]:
    """Зображення → сирі KVT-факти одним проходом (в обхід markdown-етапу).

    Запасний шлях для випадку, коли транскрипція markdown вийшла неповною (модель
    може зірватися в петлю на широких таблицях і обірвати документ) — тоді фактів
    із тексту не витягти, а з самого зображення все ще можна.
    """
    try:
        from gradio_client import handle_file
    except ImportError as exc:  # pragma: no cover
        raise OcrLabError(
            "gradio_client не встановлено — постав extra 'cloud' для хмарного OCR Lab."
        ) from exc
    client = _client(space or OCRLAB_SPACE)
    path = _decode_to_tempfile(image_base64)
    try:
        result = client.predict(handle_file(path), api_name="/facts_from_image")
    except Exception as exc:
        raise OcrLabError(f"OCR Lab facts_from_image недоступний: {exc}") from exc
    finally:
        os.unlink(path)
    return _facts_of(result)


def extract_facts(text: str, *, space: str | None = None) -> list[dict]:
    """Stage 2: текст/markdown → сирі KVT-факти (через ендпойнт /extract_facts)."""
    client = _client(space or OCRLAB_SPACE)
    try:
        result = client.predict(text, api_name="/extract_facts")
    except Exception as exc:
        raise OcrLabError(f"OCR Lab extract_facts недоступний: {exc}") from exc
    return _facts_of(result)
