# LiveMedCard demo — Hugging Face Docker Space (CPU).
# Публічний прототип для Digital Future Hackathon. Текст/сід/безпека — детермінований
# `stub` (миттєвий старт, дані не покидають процес). Завантаження ФОТО (`/ingest/image`)
# — реальний OCR через хмарний ZeroGPU Lab (NuExtract3), лише синтетичні документи.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LMC_EXTRACTOR=stub \
    LMC_IMAGE_OCR=ocrlab \
    LMC_SEED_SAMPLES=1

WORKDIR /app

# stub-залежності (без torch/transformers) + gradio_client для виклику хмарного
# OCR Lab із фото. Легкий образ, швидкий cold-start; GPU лишається на боці Lab.
RUN pip install "pydantic>=2" "networkx>=3" "fastapi>=0.110" "uvicorn>=0.29" "gradio_client>=1.3"

# Пакет разом зі static/ (SPA) і data/ (демо-документи, довідники).
COPY livemedcard ./livemedcard

EXPOSE 7860
CMD ["uvicorn", "livemedcard.api:app", "--host", "0.0.0.0", "--port", "7860"]
