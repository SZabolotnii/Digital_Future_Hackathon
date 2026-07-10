---
title: LiveMedCard OCR Lab
emoji: 🔬
colorFrom: green
colorTo: blue
sdk: gradio
app_file: app.py
pinned: false
short_description: Синтетичний OCR/extraction sandbox · NuExtract3 · ZeroGPU
---

# LiveMedCard OCR Lab

Окремий **OCR / structured-extraction** сервіс на **ZeroGPU** для проєкту
[LiveMedCard](https://huggingface.co/spaces/DocUA/LiveMedCard). Один рушій
**NuExtract3** (Apache-2.0, VLM) дає дві іпостасі документа: **markdown-транскрипцію**
та **структуровані KVT4-факти**, які може прийняти LiveMedCard.

> **⚠ Публічний demonstration sandbox.** Завантажуйте лише **синтетичні або повністю
> деідентифіковані** документи — не реальні персональні медичні дані.

## Межа відповідальності

Сервіс **лише розпізнає й структурує**. Він **не ставить діагнозів**, не визначає
взаємодій ліків, доз і клінічних ризиків. Сигнали безпеки формуються **детерміновано**
у самому LiveMedCard (шари L1/L2a), а не цією моделлю. Кожен факт — **candidate fact**,
поки його не прийняв downstream-валідатор (Spec.md §3.2).

## Ендпойнт

`extract(image, mode, source_document_id) → (markdown, {"facts":[…]}, diagnostics)`

- `mode`: `Full` (markdown + KVT4, двоетапно) · `Markdown` · `Structured`.
- normalized KVT4-факти: `cluster, keyword, value, unit, timestamp, unreadable,
  source_document_id, loinc?` — нечислові/нерозбірливі значення → `value=null,
  unreadable=true` (модель не вигадує числа).
- keyword→LOINC — детермінований міст (спільний із local-шляхом LiveMedCard).

Викликається програмно через `gradio_client` (Client → `/extract`).

Модель: [numind/NuExtract3](https://huggingface.co/numind/NuExtract3) · базова Qwen3.5-4B.
