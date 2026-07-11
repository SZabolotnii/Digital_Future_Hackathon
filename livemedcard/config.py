"""Централізовані налаштування застосунку (з можливістю override через env).

Виносить «магічні» пороги й шляхи з коду в одне місце — легше конфігурувати й
тестувати. Значення читаються один раз при імпорті.
"""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
STATIC_DIR = Path(__file__).parent / "static"

# Поріг впевненості витягу L2b, нижче якого документ іде на перегляд людиною (L3).
LOW_CONFIDENCE = float(os.getenv("LMC_LOW_CONFIDENCE", "0.6"))

# Впевненість, яку StubExtractor приписує кожному словниковому збігу.
# Реальний екстрактор (MedGemma) має ставити модельну впевненість замість цієї.
STUB_CONFIDENCE = float(os.getenv("LMC_STUB_CONFIDENCE", "0.95"))

# Впевненість витягу, зробленого LLM (NuExtract/LoRA/хмарний Lab). Тримаємо НИЖЧЕ
# LOW_CONFIDENCE навмисно: калібрування в нас поки немає, а обірваний OCR виглядає
# рівно так само, як повний. Поки не вміємо міряти — чесне значення це «не
# перевірено», і L3 веде такий документ до людини. Детермінований `stub` (regex +
# vocab на сирому тексті) лишається при 0.95: там впевненість справді щось означає.
UNVERIFIED_CONFIDENCE = float(os.getenv("LMC_UNVERIFIED_CONFIDENCE", "0.5"))

# Максимальний розмір тексту документа (символів) — валідація API.
MAX_DOC_CHARS = int(os.getenv("LMC_MAX_DOC_CHARS", "20000"))

# Шлях до файлу персистентності (JSON). Порожньо → стан лише в пам'яті.
PERSIST_PATH = os.getenv("LMC_PERSIST_PATH", "")

# Засіяти демо-картку на старті (для публічного демо на HF Space — щоб картка була
# заповнена одразу). Порожньо → старт з порожньої картки (дефолт, тести/CI).
SEED_SAMPLES = os.getenv("LMC_SEED_SAMPLES", "")

# Налаштування локальної LLM/Vision моделі для OCR
LMSTUDIO_URL = os.getenv("LMC_LMSTUDIO_URL", "http://127.0.0.1:1234/v1")
OCR_MODEL = os.getenv("LMC_OCR_MODEL", "medgemma-1.5-4b-it")

# Екстрактор L2b:
#   "stub"           — детермінований offline (дефолт, тести/CI/демо);
#   "two_stage"      — Stage 1 (LLM SGR) → Stage 2 (детермінована vocab KV-T);
#   "two_stage_lora" — Stage 1 (база) → Stage 2 (база+LoRA генерує KVT4);
#   "nuextract"      — NuExtract3 VLM (локальний): image→markdown + markdown→JSON;
#   "ocrlab"         — той самий NuExtract3, але на хмарному ZeroGPU Space
#                      (opt-in cloud; лише синтетичні/деідентифіковані документи).
EXTRACTOR = os.getenv("LMC_EXTRACTOR", "stub")

# Бекенд OCR саме для завантаження ФОТО (`/ingest/image`), відокремлений від
# текстового екстрактора. Дозволяє публічному демо тримати детермінований `stub`
# для сіду/тексту/безпеки (миттєвий старт, 0 хмари), але для явного завантаження
# фото — робити РЕАЛЬНИЙ OCR через хмарний Lab. Порожньо → успадковує EXTRACTOR.
#   "ocrlab" — хмарний ZeroGPU NuExtract3; "nuextract" — локальний; "local" — vision-LLM (OcrService).
IMAGE_OCR = os.getenv("LMC_IMAGE_OCR", "")

# Хмарний OCR Lab (ZeroGPU) — окремий Gradio Space (Spec.md). Вмикається, коли
# EXTRACTOR == "ocrlab" АБО IMAGE_OCR == "ocrlab" (лише синтетичні/деідентифіковані документи).
OCRLAB_SPACE = os.getenv("LMC_OCRLAB_SPACE", "DocUA/livemedcard-ocr-lab")
OCRLAB_TOKEN = os.getenv("LMC_OCRLAB_TOKEN") or os.getenv("HF_TOKEN") or None

# Модель Stage 1 (SGR-стиснення). None → автовизначення завантаженої моделі.
STAGE1_MODEL = os.getenv("LMC_STAGE1_MODEL") or None

# Двосерверний LoRA-сетап (llama.cpp): Stage 1 — база, Stage 2 — база+LoRA-адаптер.
# Аліаси/порти за README FT-моделі DocUA/...-two-stage.
STAGE1_URL = os.getenv("LMC_STAGE1_URL", LMSTUDIO_URL)
STAGE2_URL = os.getenv("LMC_STAGE2_URL", "http://127.0.0.1:1246/v1")
STAGE2_MODEL = os.getenv("LMC_STAGE2_MODEL", "medgemma-ft-lora-adapters-q5_k_m")

# NuExtract (VLM structured extraction) — OpenAI-сумісний ендпойнт (llama-server
# --jinja з mmproj, або vLLM). Один рушій робить image→markdown і markdown→JSON.
NUEXTRACT_URL = os.getenv("LMC_NUEXTRACT_URL", "http://127.0.0.1:1299/v1")
NUEXTRACT_MODEL = os.getenv("LMC_NUEXTRACT_MODEL", "NuExtract3")

# Семантичний ретрив по free-text summary у Q&A. Дефолт off → лексичне ранжування
# (offline, CI, демо). On → embedding-косинус через локальний сервер embeddings
# (OpenAI-сумісний `/embeddings`), з м'яким відкатом на лексику при недоступності.
SEMANTIC_RETRIEVAL = os.getenv("LMC_SEMANTIC_RETRIEVAL", "").lower() in (
    "1", "true", "yes", "on",
)
EMBED_MODEL = os.getenv("LMC_EMBED_MODEL", "nomic-embed-text")

