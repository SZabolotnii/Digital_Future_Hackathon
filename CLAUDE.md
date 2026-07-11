# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Про проєкт

Матеріали кейсового завдання для **Digital Future Hackathon** (ніша Aging & Longevity, соло-участь): проєкт **LiveMedCard** — «жива медична картка», що структурує медичні документи та відповідає на питання. У репозиторії два пласти: (1) submit-документи (PDF, згенеровані reportlab-скриптами) і (2) **робоча програмна реалізація MVP** — Python-пакет `livemedcard/` за архітектурою TRACE.

Еволюція назви: «Картка+» (рання версія) → **LiveMedCard** (фінальна). Актуальний submit — `LiveMedCard_DigitalFuture_Submit.pdf`, згенерований `build_submit.py`.

## Файли та структура

Пакет **`livemedcard/`** (реалізація MVP; повна архітектура — `ARCHITECTURE.md`):
- `fhir.py` — FHIR-вирівняні Pydantic v2-моделі + `Sys`, `rxnorm_of`, `loinc_of`
- `dto.py` — DTO конвеєра: `Document`, `ExtractionResult`, `Signal`, `Decision`, `TraceReport`, `AuditEntry`
- `refmodels.py` — типізовані моделі довідників (`Interaction`, `DoseLimit`, `RangeSpec`/`RefRange`, `Vocab`); усі **заморожені** (frozen), бо loader-и кешуються
- `config.py` — централізовані налаштування (пороги, шляхи; override через env `LMC_*`)
- `stores.py` — `FactStore` (KV-T, ідемпотентний, індекс за LOINC; `KVTFact` несе `source_doc_id` + **`source_span`** — span-рівневий провенанс) + `KnowledgeGraph` (networkx; ребра `derived_from` факт→`Document/{id}`, вузол-документ тримає free-text)
- `reference.py` + `data/*.json` — відкриті довідкові БД; loader-и кешовані й повертають незмінні структури; `range_for(loinc, sex)` — межі за статтю
- `layers/` — шари TRACE: `l1_safety` (взаємодії/дози), `l2a_trends` (тренди/межі, за статтю), `l2b_extractor` (екстрактори L2b: `StubExtractor` (offline, `deterministic_extract`; зберігає сирий текст як `DocumentReference` — free-text іпостась для span/QA), `TwoStageExtractor` (Stage1+детермінований Stage2), `LoraStage2Extractor` (Stage1+LoRA Stage2); доза×кратність; **дата на факт** або fallback `received_at`; **стабільні хеш-id**; **`source_span`** (offset у free-text) на факт у детермінованому шляху), `l2b_stage1` (Stage 1 SGR-стиснення → 9-key summary, free-text іпостась), `l2b_stage2_lora` (Stage 2 через FT база+LoRA → KVT4 → Observations, keyword→LOINC міст), `l2b_nuextract` (NuExtract3 VLM: image→markdown + markdown→JSON за шаблоном), `l3_router` (роутинг/ескалація)
- `pipeline.py` — `LiveMedCard` (джерело істини FHIR-ресурсів; `snapshot`/`from_snapshot`, типізований аудит `AuditEntry`; тримає й `DocumentReference`-и — free-text іпостась, `_absorb_docref` додає summary у вузол `Document/{id}` графа); **`_assert_provenance`** — інваріант: витягнутий факт без `source_doc_id` відхиляється на межі `ingest` (`ValueError`)
- `bundle.py` — експорт/імпорт FHIR Bundle; `repository.py` — персистентність у локальний JSON (пошкоджений знімок → явний `ValueError`, не мовчазний порожній старт)
- `qa.py` — grounded Q&A з провенансом (за статтю); **білінгвальний**: `detect_lang` за питанням → відповідь UA/EN через каталог `_MSG`; keyword-мапи UA+EN для лаб/ліки-інтентів; freetext-гілка — **ранжований span-aware ретрив** через `retrieval.py`; `api.py` — FastAPI + UI (стан під `threading.Lock`, без `global`); `demo.py` — CLI-демо
- `lang.py` — легкий детектор мови `detect_lang(text) -> 'uk'|'en'` (частка кирилиці); основа авто-вибору мови у білінгвальному режимі
- `retrieval.py` — релевантнісний ретрив по free-text іпостасі (`chunk` → пасажі з offset-ами + `rank`): лексичне ранжування offline (дефолт), опційно семантичний косинус через `llm_client.embeddings` (`LMC_SEMANTIC_RETRIEVAL`), м'який відкат на лексику; живить freetext-Q&A. Ретрив НЕ породжує сигналів (межа детермінізму)
- `llm_client.py` — спільний OpenAI-сумісний клієнт до локального сервера (LM Studio): `chat_completions(messages)` + `detect_model()` + `embeddings()` (семантичний ретрив); лише stdlib (`urllib`), щоб тести мокали `urllib.request.urlopen`. Спільний канал для OCR, Stage 1 і embeddings
- `ocr.py` — `OcrService`: OCR медичних зображень (vision-модель типу MedGemma). Делегує HTTP у `llm_client`, лишає лише vision-payload і промпт дослівної транскрипції. Крок image→text перед двоетапним L2b, **жодних висновків про безпеку**

Документи та легасі:
- `build_pdf.py` / `build_onepager.py` / `build_submit.py` — reportlab-генератори PDF (`build_submit.py` — фінальний, з flowable `TraceDiagram`)
- `case_kartka_plus.md` — вихідний текст кейсу
- `livemedcard_fhir_skeleton.py` — **початковий** одно-файловий прототип; концепції перенесено в пакет `livemedcard/`, тримати як довідку, не розвивати

## Команди

Проєкт керується `uv` (див. `pyproject.toml`). Системний `python3` не має залежностей.

```bash
uv run python -m livemedcard.demo            # E2E-демо в терміналі
uv run uvicorn livemedcard.api:app --reload  # веб-UI на http://127.0.0.1:8000 + JSON API
uv run --extra dev pytest -q                 # тести (133 шт.)
uv run --extra dev pytest tests/test_pipeline.py -q   # один файл
uv run --extra dev ruff check livemedcard tests       # лінтер (як у CI)
LMC_PERSIST_PATH=~/lmc.json uv run uvicorn livemedcard.api:app  # з персистентністю
```

Веб-інтерфейс (`livemedcard/static/index.html`) — самодостатня SPA, яку віддає
FastAPI за `/`: таймлайн показників зі спарклайнами, панель безпеки TRACE
(сигнали + рішення L3), список ліків, grounded Q&A і експорт FHIR. Ендпойнти:
`/state`, `/ingest`, `/ingest/image` (OCR зображення → текст → `ingest`),
`/ingest/samples`, `/reset`, `/timeline/{loinc}`, `/signals`, `/ask`,
`/export` (FHIR Bundle), `/import`, `/audit`, `/metrics`. Персистентність —
через env `LMC_PERSIST_PATH` (local-first JSON-знімок).

**OCR через локальну LLM:** `/ingest/image` вимагає запущеного LM Studio (або
іншого OpenAI-сумісного сервера) з завантаженою vision-моделлю. Налаштування —
env `LMC_LMSTUDIO_URL` (типово `http://127.0.0.1:1234/v1`) і `LMC_OCR_MODEL`
(типово `medgemma-1.5-4b-it`). Без сервера ендпойнт віддає `503`. Тести
(`tests/test_ocr.py`) мокають `urllib`, тож реального сервера не потребують.

**Двоетапний L2b (дві іпостасі даних).** За методом статті автора *LLM StructCore*
(Schema-Guided Reasoning Condensation): вхідний текст (OCR або введений) дає дві
іпостасі — (1) **free-text** SGR-summary (Stage 1, MedGemma; зберігається як FHIR
`DocumentReference`, живить провенанс і Q&A-fallback) і (2) **структуровану** KV-T
(Stage 2 → FactStore). Межа детермінізму збережена: LLM лише стискає/витягує факти
(впливає на recall), сигнали безпеки — детерміновані на FactStore. Stage 1
surface-ить **дату на факт** (`показник: значення (YYYY-MM-DD)`).

Три режими `LMC_EXTRACTOR` (дефолт `stub` — offline, для тестів/CI/демо):
- `two_stage` — Stage 2 = детермінований `deterministic_extract` по summary
  (regex + `vocab.json` → LOINC). Одна модель.
- `two_stage_lora` — Stage 2 = **FT MedGemma база+LoRA** (`l2b_stage2_lora`),
  що генерує **KVT4** (`{cluster,keyword,value,timestamp}`); часті лаби мостяться
  keyword→LOINC (решта — free-form). Потребує **двох llama.cpp-серверів**:
  `LMC_STAGE1_URL` (база) і `LMC_STAGE2_URL` (база+LoRA-адаптер, дефолт `:1246`),
  моделі `LMC_STAGE1_MODEL`/`LMC_STAGE2_MODEL`. Модель:
  `DocUA/medgemma-1.5-4b-it-gguf-q5-k-m-two-stage`.
- `nuextract` — **NuExtract3 VLM** (`l2b_nuextract`): один рушій робить
  image→markdown (Stage 1, у `/ingest/image` замість OCR) і markdown/текст→JSON
  за шаблоном (Stage 2). У бенчмарку — найточніший (value-accuracy 90% на Libre-View
  gold; двоетапний бере TIR-бари, які інші губили). Ендпойнт `LMC_NUEXTRACT_URL`
  (дефолт `:1299`), модель `LMC_NUEXTRACT_MODEL`. Легкий деплой:
  `numind/NuExtract3-GGUF` через `llama-server --jinja --mmproj ...` (без torch,
  `chat_template_kwargs` проходить). KVT4→Observations через той самий міст keyword→LOINC.

Тести (`tests/test_l2b_stage1.py`, `tests/test_l2b_stage2_lora.py`) мокають
`chat_completions` відповідного шару. E2E-runner на реальному документі —
`~/Medical Brain/OCR_model-with_PDF/run_e2e.py` (пише `runs/<timestamp>/`).

Архітектурний інваріант, який слід зберігати при змінах: **сигнали безпеки (L1/L2a) рахуються детерміновано на факт-сторі, а L2b лише витягує факти й не робить висновків про безпеку.**

**macOS-пастка з тестами:** `uv` періодично ставить прапор `UF_HIDDEN` на editable `.pth`-файл venv, а CPython 3.12 пропускає приховані `.pth` — тоді `import livemedcard` падає. Нейтралізовано через `pythonpath = ["."]` у `[tool.pytest.ini_options]`; якщо все ж трапиться поза pytest — `chflags nohidden .venv/lib/python3.12/site-packages/_editable_impl_livemedcard.pth`.

### ⚠ Генерація PDF: скрипти писалися під Linux-sandbox

Усі `build_*.py` **не запустяться на цьому Mac без правок**:
- шрифти DejaVu очікуються за `/usr/share/fonts/truetype/dejavu/` (на macOS немає; локальна альтернатива — знайти TTF через `fc-list` або скористатися `DejaVuSans.ttf` з пакета reportlab/matplotlib);
- `build_submit.py` пише вивід у `/sessions/.../mnt/outputs/...` — шлях контейнера; замінити на локальний.

Мова контенту PDF — українська, тому шрифт обовʼязково з підтримкою кирилиці (стандартні PDF-шрифти reportlab не підійдуть).

## Архітектура (спільна для скелета і текстів)

Скелет `livemedcard_fhir_skeleton.py` інстанціює фреймворк **TRACE** (Zabolotnii, arXiv:2605.03838) — шари знизу вгору:

- **L1** — детермінований якір довіри (НЕ навчений): взаємодії ліків / дозові межі по відкритій БД (`check_interactions`, `INTERACTION_DB`)
- **L2a** — класичний ML: детерміновані тренд-сигнали на факт-сторі (`detect_monotonic_trend`)
- **L2b** — локальна LLM (MedGemma, on-device): лише розуміння вільного тексту виписок
- **L3** — політика роутингу й ескалації; **L4** — людина (родина / лікар)

Два сховища над спільними FHIR-ресурсами (Pydantic v2-моделі: `Patient`, `Encounter`, `Observation`, `Condition`, `MedicationRequest` з кодами LOINC/RxNorm/SNOMED):

1. **KV-T факт-стор** (`KVTFact`, `observation_to_kvt`) — джерело істини для кількісного; на ньому детерміновано рахуються сигнали L2a/L1
2. **Граф знань** (networkx, `build_graph`) — семантичні зв'язки та провенанс (`Provenance`, ребра `derived_from`, `indicated_for`); живить grounded Q&A

**Ключовий принцип**: сигнали безпеки формуються детерміновано на KV-T + L1, а не обходом графа генеративною моделлю. Будь-які зміни скелета мають зберігати цю межу.

## Git

Тека проєкту **є власним git-репозиторієм** (root = сама тека проєкту, гілка `main`). Робити commit/push лише за явним запитом користувача.
