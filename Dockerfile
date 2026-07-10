# LiveMedCard demo — Hugging Face Docker Space (CPU, детермінований stub-режим).
# Публічний прототип для Digital Future Hackathon. Дані не покидають процес; без LLM/GPU.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LMC_EXTRACTOR=stub \
    LMC_SEED_SAMPLES=1

WORKDIR /app

# Лише stub-залежності (без torch/transformers) — легкий образ, швидкий cold-start.
RUN pip install "pydantic>=2" "networkx>=3" "fastapi>=0.110" "uvicorn>=0.29"

# Пакет разом зі static/ (SPA) і data/ (демо-документи, довідники).
COPY livemedcard ./livemedcard

EXPOSE 7860
CMD ["uvicorn", "livemedcard.api:app", "--host", "0.0.0.0", "--port", "7860"]
