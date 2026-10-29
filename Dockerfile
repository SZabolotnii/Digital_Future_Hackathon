# LiveMedCard demo — один образ для Hugging Face Docker Space (CPU) і Cloud Run.
# Публічний прототип для Digital Future Hackathon. Текст/сід/безпека — детермінований
# `stub` (миттєвий старт, дані не покидають процес). Завантаження ФОТО (`/ingest/image`)
# — реальний OCR через хмарний ZeroGPU Lab (NuExtract3), лише синтетичні документи.

# ── builder: залежності рівно з uv.lock ─────────────────────────────────────────
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /bin/uv

# Python з образу, не завантажений uv: runtime-стадія має той самий інтерпретатор
# за тим самим шляхом, тож скопійований .venv там працює.
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# stub-залежності (без torch/transformers) + extra `cloud` (gradio_client) для виклику
# хмарного OCR Lab із фото. Легкий образ, швидкий cold-start; GPU лишається на боці Lab.
# Сам проєкт не встановлюється: код іде окремою копією в runtime (PYTHONPATH).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra cloud

# ── runtime ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Картка стартує ПОРОЖНЬОЮ (без LMC_SEED_SAMPLES): демо-документи вантажаться лише
# кнопкою — і набором тієї мови, якою відкрито інтерфейс.
ENV PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app \
    LMC_EXTRACTOR=stub \
    LMC_IMAGE_OCR=ocrlab

# UID 1000 — той самий, під яким HF Spaces запускає контейнер.
RUN useradd --create-home --uid 1000 app \
    && install -d -o app -g app /app/bench

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
# Пакет разом зі static/ (SPA) і data/ (демо-документи, довідники).
COPY livemedcard ./livemedcard
# Бенч у тому самому образі: job `livemedcard-bench` запускає його звідси (INV-7).
# Каталог належить app (див. вище), бо бенч пише extraction_results.json поруч із собою.
COPY --chown=app:app bench/run_extraction_bench.py bench/extraction_gold.json ./bench/

USER app

# 7860 для HF Spaces; Cloud Run задає свій PORT. Shell-форма, щоб ${PORT} розкрився.
EXPOSE 7860
CMD ["sh", "-c", "exec uvicorn livemedcard.api:app --host 0.0.0.0 --port ${PORT:-7860}"]
