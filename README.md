# LiveMedCard

> **«Жива медична картка»** — структурує медичні документи, будує таймлайн здоров'я і відповідає на питання з посиланням на джерела.

[![CI](https://github.com/SZabolotnii/Digital_Future_Hackathon/actions/workflows/ci.yml/badge.svg)](https://github.com/SZabolotnii/Digital_Future_Hackathon/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![FHIR R4](https://img.shields.io/badge/FHIR-R4-orange)](https://hl7.org/fhir/R4/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## Проблема

Медична документація літньої людини — стос паперових виписок і роздруківок аналізів у папці. Ніхто не бачить **динаміки**: що креатинін ріс три виписки поспіль, що два різні лікарі призначили взаємодіючі ліки. Проблема особливо гостра для опікунів і для самого пацієнта.

## Рішення

LiveMedCard бере текст медичного документа → витягує FHIR-ресурси → **детермінованими правилами** (не «думкою» ШІ) перевіряє взаємодії ліків, дозові межі, тренди показників → показує зведену картку з таймлайном і відповідає на питання.

```
Документ (текст / фото*)
    │
    ▼  L2b  Витяг фактів (StubExtractor → MedGemma†)
FHIR-ресурси (Observation / Condition / MedicationRequest)
    │                     │
    ▼                     ▼
FactStore (KV-T)    KnowledgeGraph
    │                     │
    ▼  L1  взаємодії + дозові межі (детерміновано)
    ▼  L2a тренди + вихід за референс (детерміновано)
list[Signal]
    │
    ▼  L3  Роутинг → auto | ескалація до людини (L4)

* OCR — roadmap   † підміняється без зміни конвеєра
```

**Ключовий принцип**: сигнали безпеки формуються **детерміновано** на факт-сторі, а не генеративною моделлю. Це операціоналізований людський нагляд (EU AI Act ст. 14) ще на рівні MVP.

---

## Можливості

| Функція | Опис |
|---|---|
| 🔬 **Таймлайн показників** | Динаміка кожного аналізу з референсними нормами, спарклайнами, статусами (у нормі / вище / нижче) — за статтю пацієнта |
| 💊 **Перевірка взаємодій ліків** | Детерміновано по RxNorm-базі: відомі пари + перевищення добових доз |
| 📈 **Тренди і порушення норм** | Стійке монотонне зростання/спадання (≥ 3 вимірювання) та останнє значення поза нормою |
| 🔴 **Ескалація до людини** | L3-роутер виносить рішення на L4 при major/critical-сигналі або низькій впевненості витягу |
| 💬 **Grounded Q&A** | Відповіді на питання природною мовою — лише з фактів картки, з посиланням на документи-джерела |
| 📤 **FHIR Bundle export/import** | Стандартний HL7 FHIR R4 Bundle для передачі лікарю або в eHealth |
| 🔒 **Local-first** | Медичні дані не залишають пристрій |

---

## Швидкий старт

### Вимоги

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) (рекомендований менеджер)

### Встановлення та запуск

```bash
# 1. Клонувати репозиторій
git clone https://github.com/SZabolotnii/Digital_Future_Hackathon.git
cd Digital_Future_Hackathon

# 2. Запустити веб-інтерфейс
uv run uvicorn livemedcard.api:app --reload
```

Відкрити у браузері: **http://127.0.0.1:8000**

Натиснути **«Завантажити демо-документи»** — картка заповниться таймлайном демо-пацієнтки з трьома виписками за рік.

### CLI-демо (без браузера)

```bash
uv run python -m livemedcard.demo
```

Виведе E2E-прогін: витяг → сигнали → рішення L3 → grounded Q&A з джерелами.

---

## Розгортання

### Локально з персистентністю

За замовчуванням стан зберігається лише в пам'яті і зникає при перезапуску. Щоб зберігати між сесіями:

```bash
LMC_PERSIST_PATH=~/livemedcard.json uv run uvicorn livemedcard.api:app
```

При наступному запуску з тим самим `LMC_PERSIST_PATH` стан картки відновиться автоматично.

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install uv && uv sync
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "livemedcard.api:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t livemedcard .
docker run -p 8000:8000 \
  -v "$HOME/.livemedcard:/data" \
  -e LMC_PERSIST_PATH=/data/card.json \
  livemedcard
```

### Змінні середовища

| Змінна | За замовч. | Опис |
|---|---|---|
| `LMC_PERSIST_PATH` | `""` (пам'ять) | Шлях до JSON-файлу знімка картки |
| `LMC_LOW_CONFIDENCE` | `0.6` | Поріг впевненості L2b для ескалації L3 |
| `LMC_STUB_CONFIDENCE` | `0.95` | Впевненість StubExtractor на словниковий збіг |
| `LMC_MAX_DOC_CHARS` | `20000` | Ліміт символів одного документа |

### Production-зауваження

> ⚠️ Поточна реалізація — демонстраційний прототип. Для production необхідно додати:
> - **Автентифікацію** (Bearer-токен / OAuth2) — у поточній версії будь-хто з доступом до URL бачить картку.
> - **Ізоляцію пацієнтів** — зараз один процес = одна картка.
> - **HTTPS** — обов'язково для медичних даних.
>
> Архітектура `_State + threading.Lock` готова до розширення на `dict[patient_id, _State]` + middleware без зміни логіки ендпойнтів.

---

## Тести та якість

```bash
# Усі тести (64 шт.)
uv run --extra dev pytest -q

# Лінтер
uv run --extra dev ruff check livemedcard tests

# Один модуль
uv run --extra dev pytest tests/test_pipeline.py -q
```

CI запускається автоматично на кожен push/PR через GitHub Actions (`.github/workflows/ci.yml`): `ruff check` + `pytest`.

---

## Структура проєкту

```
livemedcard/
├── fhir.py           # FHIR R4 Pydantic-моделі (Observation, Condition, MedicationRequest…)
├── dto.py            # DTO конвеєра: Signal, Decision, TraceReport, AuditEntry
├── config.py         # Централізовані налаштування (env LMC_*)
├── stores.py         # FactStore (KV-T, LOINC-індекс) + KnowledgeGraph (networkx)
├── reference.py      # Завантаження довідкових БД (frozen + lru_cache)
├── refmodels.py      # Типізовані frozen-моделі довідників
├── pipeline.py       # LiveMedCard — оркестрація E2E, snapshot/from_snapshot
├── bundle.py         # FHIR Bundle export/import
├── repository.py     # Персистентність у локальний JSON
├── qa.py             # Grounded Q&A з провенансом
├── api.py            # FastAPI + threading.Lock (_State)
├── demo.py           # CLI-демонстратор
├── layers/
│   ├── l1_safety.py      # L1: взаємодії + дозові межі (детерміновано)
│   ├── l2a_trends.py     # L2a: тренди + порушення референс-меж
│   ├── l2b_extractor.py  # L2b: Extractor Protocol + StubExtractor
│   └── l3_router.py      # L3: роутинг/ескалація
├── data/
│   ├── interactions.json     # RxNorm-пари взаємодій
│   ├── dose_limits.json      # Добові дозові межі
│   ├── reference_ranges.json # Референсні межі (зі статтю)
│   ├── vocab.json            # Словник StubExtractor
│   └── sample_docs.json      # Демо-документи
└── static/
    └── index.html        # Веб-SPA (без збірки, самодостатня)

tests/                    # 64 тести (pytest)
ARCHITECTURE.md           # Детальна архітектура TRACE
USER_GUIDE.md             # Посібник користувача
REVIEW_AND_ROADMAP.md     # Code review + дорожня карта
```

---

## API

| Метод | Ендпойнт | Опис |
|---|---|---|
| `GET` | `/` | Веб-інтерфейс (SPA) |
| `POST` | `/ingest` | Обробити документ `{id, text, kind, received_at?}` |
| `POST` | `/ingest/samples` | Завантажити демо-документи |
| `POST` | `/reset` | Скинути картку (новий пацієнт) |
| `GET` | `/state` | Агрегований стан картки (для UI) |
| `GET` | `/signals` | Поточні детерміновані сигнали |
| `GET` | `/timeline/{loinc}` | Часовий ряд показника |
| `POST` | `/ask` | Grounded Q&A `{question: str}` |
| `GET` | `/export` | FHIR Bundle (JSON) |
| `POST` | `/import` | Імпорт з FHIR Bundle |
| `GET` | `/audit` | Журнал рішень L3 |
| `GET` | `/metrics` | Агреговані метрики |
| `GET` | `/health` | Health check |

Інтерактивна документація: **http://127.0.0.1:8000/docs** (Swagger UI).

---

## Архітектура TRACE

Система реалізує фреймворк **TRACE** (Zabolotnii, [arXiv:2605.03838](https://arxiv.org/abs/2605.03838)):

| Шар | Модуль | Роль |
|---|---|---|
| **L1** | `l1_safety.py` | Детермінований якір довіри: взаємодії ліків і дозові межі по відкритій RxNorm-БД. Не навчений, не галюцинує. |
| **L2a** | `l2a_trends.py` | Класичний аналіз на факт-сторі: тренди і вихід за референс-межі. |
| **L2b** | `l2b_extractor.py` | Розуміння тексту (витяг фактів). У MVP — детермінований `StubExtractor`; підміняється на MedGemma через `Extractor` Protocol без змін конвеєра. |
| **L3** | `l3_router.py` | Політика ескалації: `auto` або `L4_human` (major/critical-сигнал або низька впевненість витягу). |
| **L4** | — | Людина (родина / лікар) приймає фінальне рішення. |

**Інваріант:** сигнали безпеки формуються детерміновано на `FactStore` (L1/L2a), а не генеративною моделлю (L2b). Будь-які зміни коду мають зберігати цю межу.

---

## Дорожня карта

**Горизонт 1 — від демо до прототипу:**
- [ ] Реальний екстрактор MedGemma 4B (schema-guided generation)
- [ ] OCR pipeline (Apple Vision / Tesseract → `Document.text`)
- [ ] Розширення довідкових БД (NLM DDI, повний LOINC-референс)

**Горизонт 2 — продуктова зрілість:**
- [ ] Автентифікація (OAuth2) + ізоляція пацієнтів
- [ ] LLM-формулювання відповідей Q&A (RAG поверх детермінованого ретриву)
- [ ] Мобільний клієнт (on-device, офлайн)

**Горизонт 3 — масштаб і регуляція:**
- [ ] Відповідність EU AI Act (ст. 14 операціоналізовано через L3/L4) + шлях MDR
- [ ] Інтеграція з EHDS / eHealth

---

## Контекст проєкту

Розроблено в рамках **Digital Future Hackathon** (ніша Aging & Longevity, соло-участь).

- Архітектурний звіт та submit: `LiveMedCard_DigitalFuture_Submit.pdf`
- Детальна архітектура: [`ARCHITECTURE.md`](ARCHITECTURE.md)
- Посібник користувача: [`USER_GUIDE.md`](USER_GUIDE.md)
- Code review та дорожня карта: [`REVIEW_AND_ROADMAP.md`](REVIEW_AND_ROADMAP.md)

---

## Ліцензія

[MIT](LICENSE)
