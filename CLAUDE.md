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
- `stores.py` — `FactStore` (KV-T, ідемпотентний, індекс за LOINC) + `KnowledgeGraph` (networkx)
- `reference.py` + `data/*.json` — відкриті довідкові БД; loader-и кешовані й повертають незмінні структури; `range_for(loinc, sex)` — межі за статтю
- `layers/` — шари TRACE: `l1_safety` (взаємодії/дози), `l2a_trends` (тренди/межі, за статтю), `l2b_extractor` (`StubExtractor`; доза×кратність; **стабільні хеш-id** ресурсів від змісту), `l3_router` (роутинг/ескалація)
- `pipeline.py` — `LiveMedCard` (джерело істини FHIR-ресурсів; `snapshot`/`from_snapshot`, типізований аудит `AuditEntry`)
- `bundle.py` — експорт/імпорт FHIR Bundle; `repository.py` — персистентність у локальний JSON (пошкоджений знімок → явний `ValueError`, не мовчазний порожній старт)
- `qa.py` — grounded Q&A з провенансом (за статтю); `api.py` — FastAPI + UI (стан під `threading.Lock`, без `global`); `demo.py` — CLI-демо

Документи та легасі:
- `build_pdf.py` / `build_onepager.py` / `build_submit.py` — reportlab-генератори PDF (`build_submit.py` — фінальний, з flowable `TraceDiagram`)
- `case_kartka_plus.md` — вихідний текст кейсу
- `livemedcard_fhir_skeleton.py` — **початковий** одно-файловий прототип; концепції перенесено в пакет `livemedcard/`, тримати як довідку, не розвивати

## Команди

Проєкт керується `uv` (див. `pyproject.toml`). Системний `python3` не має залежностей.

```bash
uv run python -m livemedcard.demo            # E2E-демо в терміналі
uv run uvicorn livemedcard.api:app --reload  # веб-UI на http://127.0.0.1:8000 + JSON API
uv run --extra dev pytest -q                 # тести (64 шт.)
uv run --extra dev pytest tests/test_pipeline.py -q   # один файл
uv run --extra dev ruff check livemedcard tests       # лінтер (як у CI)
LMC_PERSIST_PATH=~/lmc.json uv run uvicorn livemedcard.api:app  # з персистентністю
```

Веб-інтерфейс (`livemedcard/static/index.html`) — самодостатня SPA, яку віддає
FastAPI за `/`: таймлайн показників зі спарклайнами, панель безпеки TRACE
(сигнали + рішення L3), список ліків, grounded Q&A і експорт FHIR. Ендпойнти:
`/state`, `/ingest`, `/ingest/samples`, `/reset`, `/timeline/{loinc}`, `/signals`,
`/ask`, `/export` (FHIR Bundle), `/import`, `/audit`, `/metrics`. Персистентність —
через env `LMC_PERSIST_PATH` (local-first JSON-знімок).

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

Тека проєкту **не є окремим git-репозиторієм** — команди git резолвляться в репозиторій у домашній директорії (`~`) без жодного коміту. Не робити commit/push звідси без явного запиту користувача: staging зачепить увесь `$HOME`.
