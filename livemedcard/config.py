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

# Хмарний OCR Lab (ZeroGPU) — окремий Gradio Space (Spec.md). Вмикається лише коли
# EXTRACTOR == "ocrlab"; за замовчуванням не використовується (pitch-демо на stub).
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

