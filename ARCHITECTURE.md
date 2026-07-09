# LiveMedCard — архітектура реалізації (TRACE)

Повноцінна програмна реалізація MVP кейсу LiveMedCard за фреймворком **TRACE**
(Zabolotnii, arXiv:2605.03838). Кожен шар — окремий модуль; головний інваріант:
**сигнали безпеки формуються детерміновано на факт-сторі (L1/L2a), а не генеративною
моделлю (L2b).**

## Потік даних (E2E)

```
Документ (фото/текст)
   │
   ▼  L2b  — Stage 1 (MedGemma → SGR-summary) → Stage 2 (детермінований deterministic_extract)
             дефолт offline: StubExtractor (Stage 2 напряму по сирому тексту)
ExtractionResult  (FHIR: Observation / Condition / MedicationRequest + Provenance; + DocumentReference — free-text)
   │
   ├──► FactStore (KV-T)   — джерело істини для кількісного
   ├──► KnowledgeGraph     — зв'язки + провенанс (вузол Document несе summary)
   └──► DocumentReference  — free-text іпостась (Stage 1 summary)
   │
   ▼  L1  — детермінований якір: взаємодії ліків, дозові межі (по відкритій БД)
   ▼  L2a — класичний аналіз: тренди й вихід за референс-межі на KV-T
list[Signal]
   │
   ▼  L3  — політика роутингу/ескалації (stateful)
Decision  (auto | ескалація до L4-людини)
   │
   ▼  L4  — людина (родина / лікар) приймає рішення
```

Q&A (grounded): запит → детермінований ретрив по FactStore+Graph → відповідь
природною мовою з посиланням на документи-джерела (провенанс). LLM тут не робить
висновків про безпеку.

## Модулі та контракти

| Шар | Модуль | Відповідальність | Вхід → вихід |
|---|---|---|---|
| — | `livemedcard/fhir.py` | FHIR-ресурси (Pydantic v2) | — |
| — | `livemedcard/dto.py` | DTO конвеєра: `Document`, `ExtractionResult`, `Signal`, `Decision`, `TraceReport` | — |
| — | `livemedcard/stores.py` | `FactStore` (KV-T), `KnowledgeGraph` (networkx) | — |
| — | `livemedcard/reference.py` | Завантаження довідкових БД з `data/*.json` | — |
| L2b | `livemedcard/layers/l2b_extractor.py` | `Extractor` protocol + `StubExtractor` | `Document` → `ExtractionResult` |
| L1 | `livemedcard/layers/l1_safety.py` | Взаємодії ліків, дозові межі | ресурси → `list[Signal]` |
| L2a | `livemedcard/layers/l2a_trends.py` | Тренди + вихід за референс | `FactStore` → `list[Signal]` |
| L3 | `livemedcard/layers/l3_router.py` | Роутинг/ескалація | `list[Signal]`, conf → `Decision` |
| — | `livemedcard/pipeline.py` | Оркестрація E2E | `Document` → `TraceReport` |
| — | `livemedcard/qa.py` | Grounded Q&A з провенансом | питання → відповідь |
| — | `livemedcard/api.py` | FastAPI (ingest / state / reset / timeline / signals / ask) + віддає UI | HTTP |
| — | `livemedcard/static/index.html` | Веб-інтерфейс: таймлайн, панель безпеки, ліки, Q&A | — |
| — | `livemedcard/demo.py` | E2E CLI-демонстратор | — |

## Ключові рішення

- **`Signal` — єдиний тип** для L1 і L2a (`layer`, `kind`, `severity`, `subjects`,
  `confidence`). Детерміновані сигнали мають `confidence=1.0`. Це роз'єднує L3 від
  реалізацій L1/L2a.
- **L2b ізольований**: `StubExtractor` (дефолт, offline) парсить український текст
  за словником `data/vocab.json`. Інтерфейс `Extractor` дозволяє підмінити на
  реальну MedGemma без змін конвеєра. Екстрактор ставить лише `confidence` витягу,
  **не** висновок про безпеку.
- **Двоетапний L2b** (метод *LLM StructCore*): Stage 1 (MedGemma) згортає текст у
  SGR-summary (free-text іпостась → FHIR `DocumentReference`); Stage 2 витягує з
  summary KV-T факти з **датою на факт**. Два режими Stage 2:
  - `two_stage` — `deterministic_extract` (regex + `vocab.json` → LOINC);
  - `two_stage_lora` — FT MedGemma база+LoRA генерує KVT4 `{cluster,keyword,value,
    timestamp}`, часті лаби мостяться keyword→LOINC (два llama.cpp-сервери).

  У будь-якому режимі стохастика LLM впливає лише на recall; сигнали безпеки
  L1/L2a лишаються детермінованими на FactStore.
- **L3 ескалює до людини** на поєднанні **високий ризик + висока впевненість**
  (`severity ∈ {major, critical}` і `mean_confidence ≥ 0.85`) — саме впевнена
  помилка найнебезпечніша. Також ескалація на низьку впевненість витягу.
- **Довідкові БД** (`data/interactions.json`, `dose_limits.json`,
  `reference_ranges.json`) — відкриті, версіоновані, читаються детерміновано.
  Коди узгоджені: RxNorm (ліки), LOINC (аналізи), SNOMED (діагнози).

## Запуск

```bash
uv run python -m livemedcard.demo          # E2E демо в терміналі
uv run uvicorn livemedcard.api:app         # веб-UI на http://127.0.0.1:8000
uv run --extra dev pytest                  # тести (64 шт.)
uv run --extra dev ruff check livemedcard tests   # лінтер (як у CI)
```

## Додаткові модулі (архітектурні покращення)

- `config.py` — централізовані налаштування (пороги/шляхи; env `LMC_*`,
  зокрема `LMC_STUB_CONFIDENCE`).
- `refmodels.py` — типізовані моделі довідників; `reference.range_for(loinc, sex)`
  дає межі з урахуванням статі пацієнта (проброс sex через L2a/Q&A/демо).
- `bundle.py` — експорт/імпорт стану як стандартного **FHIR Bundle**.
- `repository.py` + `LiveMedCard.snapshot`/`from_snapshot` — персистентність у
  локальний JSON (local-first; env `LMC_PERSIST_PATH`).
- Аудит рішень L3 і метрики (`/audit`, `/metrics`); `LiveMedCard` — джерело істини
  FHIR-ресурсів, `FactStore`/`KnowledgeGraph` — похідні проєкції.

## Зміцнення за зовнішнім архітектурним ревʼю (livemedcard.html)

- **Потокобезпечний стан API**: картка інкапсульована в `_State` під
  `threading.Lock` (sync-ендпойнти FastAPI виконуються в пулі потоків);
  жодного `global`. Конкурентний мікс запитів покрито тестом.
- **Стабільні id ресурсів**: L2b генерує id як хеш змісту
  (`документ|код|значення|час`), а не порядковий номер — ідемпотентність
  переживає зміни словника і відновлення зі старих знімків; ідентичні повторні
  згадки колапсують в один факт.
- **Типізований аудит**: `dto.AuditEntry` (Pydantic) замість «сирих» dict;
  биті записи журналу відхиляються валідацією при відновленні знімка.
- **Кешовані незмінні довідники**: loader-и `reference.py` під `lru_cache`
  повертають tuple/`MappingProxyType` **заморожених** моделей — без перебудови
  об'єктів на кожен виклик і без ризику мутації спільного стану; мапа пар
  взаємодій L1 будується один раз.
- **Індекс FactStore за LOINC**: `series()`/`loincs()` без лінійного проходу
  всього стору.
- **Захист знімка**: пошкоджений JSON персистентності → явний `ValueError`
  (сервер не стартує порожнім і не перезаписує дані автозбереженням).
- **CI**: `ruff check` + `pytest`; HTTP-контракт API покрито TestClient-тестами.
