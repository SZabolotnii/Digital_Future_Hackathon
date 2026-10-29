# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Про проєкт

Матеріали кейсового завдання для **Digital Future Hackathon** (ніша Aging & Longevity, соло-участь): проєкт **LiveMedCard** — «жива медична картка», що структурує медичні документи та відповідає на питання. У репозиторії два пласти: (1) submit-документи (PDF, згенеровані reportlab-скриптами) і (2) **робоча програмна реалізація MVP** — Python-пакет `livemedcard/` за архітектурою TRACE.

Еволюція назви: «Картка+» (рання версія) → **LiveMedCard** (фінальна). Актуальний submit — `hackathon/LiveMedCard_DigitalFuture_Submit.pdf`, згенерований `hackathon/build_submit.py`.

## Файли та структура

Пакет **`livemedcard/`** (реалізація MVP; повна архітектура — `ARCHITECTURE.md`):
- `fhir.py` — FHIR-вирівняні Pydantic v2-моделі + `Sys`, `rxnorm_of`, `loinc_of`
- `dto.py` — DTO конвеєра: `Document`, `ExtractionResult`, `Signal`, `Decision`, `TraceReport`, `AuditEntry`
- `refmodels.py` — типізовані моделі довідників (`Interaction`, `DoseLimit`, `RangeSpec`/`RefRange`, `Vocab`); усі **заморожені** (frozen), бо loader-и кешуються
- `config.py` — централізовані налаштування (пороги, шляхи; override через env `LMC_*`)
- `economics.py` — **економіка рішень TRACE**: поріг ескалації не константа, а `c* = 1 − C_human/(π·C_harm)`; залежить від того, чим ризикує документ (`Hazard`: ліки → ≈0.97, показники → 0.8). Живі числа — у `/metrics.economics`; модель і чутливість — `docs/economics.md`
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
- `ppocr.py` — альтернативний драйвер Stage 1 (`LMC_IMAGE_OCR=ppocr`): PP-OCR, детекція рядків + розпізнавання, offline, 30 МБ ваг. Цінність не в точності, а в **геометрії**: `_rows_from_boxes` збирає рядок таблиці назад із боксів, тож назва показника і значення повертаються поруч і доходять до Stage 2. Apple Vision на тому самому бланку дає кращий текст і **нуль** кодованих показників, бо групує таблицю по колонках. У PP-OCRv6 **немає кирилиці** — для неї явно береться `eslav_PP-OCRv5_mobile_rec`, і `engine_id()` це називає (мовчазна підміна моделі несумісна з провенансом). Опційна залежність: `uv sync --extra ppocr`

Темпоральний шар (третя проєкція над FHIR-ресурсами; ADR — `docs/temporal-graph.md`):
- `temporal.py` — `TemporalGraph`: сутності в часі (`TemporalEntity` з `first_valid`/`first_recorded`/`interval`), ребра з інтервалами (`treats`, `co_active`; ребра `precedes` свідомо **немає** — хибна каузальність), епізоди (сегментація за розривом `EPISODE_GAP_DAYS`), метрики топології (мультиморбідність, поліфармація, `anchored_fraction`), темпоральні запити (`as_of`/`since`/`first_appearance` з віссю `valid`|`record`). **Бітемпоральність:** valid time (коли факт істинний) vs record time (коли картка дізналась); `anchored_fraction` — частка фактів на реальній даті, а не на моменті обробки. **Межа детермінізму:** модуль НЕ імпортує `Signal` (закріплено AST-тестом), граф лише описовий. `LiveMedCard.temporal()` будує проєкцію на вимогу; `_document_dates` (valid-time якір призначень) переживає snapshot/Bundle
- `layers/l2a_temporal.py` — детерміновані темпоральні сигнали з фактів (не обходом графа): `polypharmacy` (≥5 одночасно активних), `monitoring_gap` (показник поза межею, не переміряний > `MONITORING_GAP_DAYS`; відлік від горизонту картки), `temporal_uncertainty` (частка прив'язаних фактів < `DATED_FRACTION_FLOOR`). Усі `confidence == 1.0`
- `qa.py` — темпоральні інтенти (`_HISTORY_KEYWORDS`, `_CHANGE_KEYWORDS`; перевіряються ПЕРЕД лабораторними): «коли вперше», «що нового»; білінгвальні числівники (`_plural`), застереження про неприв'язану хронологію
- SPA (`static/index.html`) — вкладка «Історія в часі»: доріжки сутностей, смуги епізодів, маркери першої появи; ендпойнт `/temporal`

Шар нагляду L4 (принцип R; EU AI Act ст. 12/14):
- `governance.py` — **задекларована межа автономії**: `DECLARATION` за тяжкістю (major/critical → `mandatory_review`, moderate → `advisory`, info → `autonomous`). Клас виводиться з тяжкості, а не з окремої таблиці за видом сигналу, щоб критерій ескалації L3 і критерій обов'язковості огляду не могли розійтися. `unreviewed_mandatory()` — що досі чекає рішення
- `dto.Signal.key` — **мовно-незалежна тотожність** сигналу (`interaction:{rx1}|{rx2}`, `range_breach:{loinc}:{above|below}`, …). Сигнали ніде не зберігаються, а перераховуються на кожен запит і в обох мовах, тож брати ідентичність з `detail` не можна. Нове вимірювання, що змінює бік порушення, дає новий ключ — і новий предмет для огляду
- `dto.ReviewEntry` — дія людини (роль, дія `acknowledged|overridden|referred`, ключ сигналу **або** id документа, нотатка, `seconds_to_act` для RBI); `dto.BoundaryEvent` — перетин межі. Обидва журнали окремі від `AuditEntry` і переживають snapshot/Bundle
- `POST /review` (404 на невідомий ключ, 422 якщо ціль не рівно одна), `GET /audit` → `{decisions, reviews, boundary_events}`, `GET /export` **пише** `BoundaryEvent`, якщо є непереглянуті обов'язкові сигнали (не блокує — фіксує)
- `l3_router.card_decision` — «що лишається невирішеним»: рахує лише **непереглянуті** сигнали + невирішені ескалації документів. Замінило виклик `route(sigs, 1.0, …)`, який мовчки викидав привід низької впевненості зі стану картки
- `l3_router.should_reinvoke` + `LiveMedCard(fallback_extractor=…)` — **друга спроба перед викликом людини** (FPA): бюджет `LMC_REINVOCATION_BUDGET` (типово 1), береться **кращий** з двох результатів (гірша альтернатива не витісняє кращу), витяг поглинається лише після вибору — інакше картка накопичувала б обидві версії тих самих фактів. Перевиклик **лише** через низьку впевненість; знайдена небезпека його не запускає. `AuditEntry.attempts`, метрика в `/metrics.cost.false_positive_attenuation`
- `AuditEntry.escalation_drive` (`risk|confidence|both|none`) — чому спрацював маршрут; вхід для SNR і EP. Відрізняється від `high_risk`, який тепер означає «цей документ вніс **нову** небезпеку» (вхід у вартість)
- `/metrics.supervision` — OvR, RBI, SNR, `review_coverage`, `ep_proxy`, `outstanding_escalations`, `unreviewed_mandatory`, `boundary_excursions`. Порожній знаменник → `null`, а не 0
- SPA — дії на кожному сигналі, смуга непереглянутого обов'язкового, перемикач ролі (декларація, не автентифікація), шухляда «Журнал рішень». Ескалацію рівня документа закриває перегляд **оригіналу** в шухлядці документа: «текст погано розпізнано» не є сигналом, тож у панелі сигналів для неї немає предмета

Провенанс версій і метрики (принципи T/E; ст. 12/13/15):
- `versioning.py` — `fingerprint(text)` (короткий sha1) і `agent(name, model)` → `name@model`. `Provenance.extracted_by` **без дефолту** (був `"MedGemma-4B"` незалежно від того, що працювало — провенанс, який бреше, гірший за відсутній) + `Provenance.prompt_id`. Кожен LLM-модуль має `prompt_id()` — відбиток свого системного промпту; для LoRA-шляху в провенанс іде **обидва** (`s1+s2`), бо Stage 1 вирішує, що взагалі доживе до Stage 2
- `reference.ruleset_version()` — `1.0.0/1.0.0/1.0.0+<відбиток>`: задекларовані версії трьох файлів **правил безпеки** (`vocab.json` не входить — він впливає на витяг, не на рішення) плюс відбиток вмісту. `data/CHANGELOG.md` + `test_ruleset_version_is_declared_and_logged` роблять властивість перевірюваною: **правило неможливо змінити так, щоб журнал цього не помітив**. `Signal.ruleset_version` проставляє `current_signals()` в одному місці, а не кожен з восьми детекторів
- **Дві іпостасі документа розділені** (ст. 13): вузол графа несе `text` (оригінал, як подано) і `generated_text` (стиснення Stage 1, написане моделлю). Раніше обидві клались в одне поле, і на двоетапних шляхах UI показував текст моделі під заголовком «Джерело» — при галюцинації Stage 1 підсвічений «доказ» був самою галюцинацією. Ознака генерованості — **факт із провенансу** (`prompt_id is not None`), а не здогадка інтерфейсу. Оригінали персистяться (`snapshot["document_texts"]`, `_meta.document_texts`); `/document/{id}` віддає `{text, generated, span_base}`; Q&A-ретрив шукає в обох
- `/metrics` — `rule_coverage` (RCR: яку частку витягнутого правила взагалі здатні побачити; некодовані факти в знаменнику), `evidence` (ETC: `fact_trail`, `signal_trail`, `ruleset_version`), `cost` (TCC: латентність за класом шляху, з `AuditEntry.extractor`/`latency_ms`)
- `docs/cpr.md` — CPR як **записаний артефакт** ст. 15 по класах задач. Безпека/Q&A/темпоральний шар ≈ 1; витяг ≈ 0.05 усвідомлено, з виміряним обґрунтуванням із `bench/results.json` (Vision 55/58 за 1.2 с проти NuExtract 58/58 за 24 с; уся різниця — два документи з рукописом) і зафіксованим негативним результатом, що confidence-роутер між ними неможливий

Калібрування, бенчмарк витягу і дрейф (принцип E; ст. 15):
- `bench/extraction_gold.json` (+ `build_extraction_gold.py`) — gold-корпус витягу: 40 документів UA+EN, 60 полів. Кожен документ існує заради **названого явища** (`probes`), а не для кількості; кілька очікують **порожній** результат (діапазони норм, палата/телефон, заперечення) — на них міряється precision. Очікуване — те, що витягла б **людина**, інакше recall був би 1.0 за побудовою
- `bench/run_extraction_bench.py` — **польові** precision/recall/F1: збіг це `(LOINC, значення ± допуск, одиниця)`, а не входження підрядка як у `run_bench.py`. Виміряно: P=0.962, R=0.850, Wilson-low **0.739**
- `calibration.py` — `confidence_for(extractor)` читає `data/extractor_calibration.json`; `mean_confidence` stub-а тепер **виміряна величина**, не константа. Береться **нижня межа Вілсона**, не точковий recall: недооцінка коштує перевірки людиною, переоцінка — пропущеного факту. Без файла — відкат на `UNVERIFIED_CONFIDENCE`
- **Наслідок, який треба знати:** 0.739 нижче за обидва пороги (0.967/0.80), тож `escalation_rate` = 1.0 — автоматично не проходить нічого. Це не регресія, а зникнення довіри, для якої не було підстав. Деталі та знайдені дефекти — `docs/calibration.md`
- **Дефекти, які знайшов корпус і які виправлено:** (1) одиниця бралася зі словника, а не з документа — «glucose 122 mg/dL» писалась як 122 mmol/L (major `range_breach` на нормальному значенні), а запобіжник `unit_mismatch` не міг спрацювати **ніколи**; тепер одиниця читається з тексту за закритим списком написань (`units.UNIT_PATTERN`), немає в тексті → `None`. (2) `_FREQ_RE` мав `(тріч)` замість `(трич)` — «тричі на добу» давало добову дозу втричі меншу. Записані й НЕ виправлені прогалини (щоб не підганяти систему під власну мірку): значення не впритул до терміна, табличні крапки-заповнювачі, «по 1 таблетці N мг»
- **Згортання кириличних омоглифів** (`l2b_extractor._fold`, `tests/test_homoglyph_folding.py`): OCR-розпізнавачі кирилиці систематично русифікують українську (`Креатинін→Креатинин`), а словник шукає точний збіг — показник зникає з картки разом зі своїм сигналом. Згортання застосовується до **обох** боків порівняння й лише всередині пошуку; кожен символ 1:1, тож `source_span` лишається дійсним. Пастка, яку тест тримає: згортання самого тексту ламає більше, ніж лагодить — правильний «Аспірин» стає «Аспірін» (ліки зникають), а «Глікований гемоглобін» падає на longest-match «гемоглобін», і HbA1c 7.6 % стає гемоглобіном 7.6 в межах норми. **Пропущений факт міняється на хибно нормальний.** Латинсько-кириличні омоглифи свідомо НЕ згортаються (словник білінгвальний). Gold-корпус русифікації не містить, тож числа калібрування не змінились
- `history.py` — часовий ряд метрик (OSI): `POST /metrics/snapshot`, `GET /metrics/history`, env `LMC_METRICS_HISTORY`. Знімки робить **оператор**, не система на кожен `GET /metrics` — інакше ряд показував би активність вкладки, а не дрейф
- `docs/eu-ai-act.md` — матриця ст. 12–15: вимога → артефакт → метрика → **target/alert/action** пороги, і окремий розділ «чого немає» (IPSR, CE як ECE, незалежна анотація). CI прогонить бенчмарк витягу, а тест звіряє записане калібрування з поточним кодом — стале число валить збірку

Документи та легасі:
- `hackathon/build_pdf.py` / `hackathon/build_onepager.py` / `hackathon/build_submit.py` — reportlab-генератори PDF (`build_submit.py` — фінальний, з flowable `TraceDiagram`)
- `hackathon/case_kartka_plus.md` — вихідний текст кейсу
- `hackathon/livemedcard_fhir_skeleton.py` — **початковий** одно-файловий прототип; концепції перенесено в пакет `livemedcard/`, тримати як довідку, не розвивати

## Команди

Проєкт керується `uv` (див. `pyproject.toml`). Системний `python3` не має залежностей.

```bash
uv run python -m livemedcard.demo            # E2E-демо в терміналі
uv run uvicorn livemedcard.api:app --reload  # веб-UI на http://127.0.0.1:8000 + JSON API
uv run --extra dev pytest -q                 # тести (274 шт.)
uv run --extra dev pytest tests/test_pipeline.py -q   # один файл
uv run --extra dev ruff check livemedcard tests       # лінтер (як у CI)
LMC_PERSIST_PATH=~/lmc.json uv run uvicorn livemedcard.api:app  # з персистентністю
```

Веб-інтерфейс (`livemedcard/static/index.html`) — самодостатня SPA, яку віддає
FastAPI за `/`: таймлайн показників зі спарклайнами, секція «витягнуто, без
стандартного коду» (факти без LOINC — показані, але без меж і сигналів), панель
безпеки TRACE (сигнали + рішення L3), список ліків, **список поданих документів**
(клік → оригінал), **граф знань** (модалка), білінгвальна **довідка**, grounded
Q&A і експорт FHIR. Ендпойнти: `/state?lang=`, `/ingest`, `/ingest/image` (OCR
зображення → текст → `ingest`), `/ingest/samples?lang=uk|en`, `/document/{id}`,
`/graph`, `/temporal`, `/reset`, `/timeline/{loinc}`, `/signals`, `/ask`,
`/review`, `/export` (FHIR Bundle), `/import`, `/audit`, `/metrics`,
`/metrics/snapshot`, `/metrics/history`.
Персистентність — через env `LMC_PERSIST_PATH` (local-first JSON-знімок).

**Демо-документи** (`data/sample_docs.json`) — два **дзеркальні** набори, `uk` і
`en`: ті самі значення й призначення, тож сигнали безпеки збігаються, різниця лише
в мові. Картка стартує **порожньою**; набір вантажиться кнопкою й **за мовою
інтерфейсу** (`LMC_SEED_SAMPLES` лишається як опція для локального сіду).

**OCR через локальну LLM:** `/ingest/image` вимагає запущеного LM Studio (або
іншого OpenAI-сумісного сервера) з завантаженою vision-моделлю. Налаштування —
env `LMC_LMSTUDIO_URL` (типово `http://127.0.0.1:1234/v1`) і `LMC_OCR_MODEL`
(типово `medgemma-1.5-4b-it`). Без сервера ендпойнт віддає `503`. Тести
(`tests/test_ocr.py`) мокають `urllib`, тож реального сервера не потребують.

**OCR без LLM (PP-OCR):** `LMC_IMAGE_OCR=ppocr` веде `/ingest/image` через
`livemedcard/ppocr.py` — детекція рядків + розпізнавання, offline, без сервера.
`uv sync --extra ppocr`; env `LMC_PPOCR_TIER` (`tiny|small|medium`, типово
`small`) і `LMC_PPOCR_LANG` (типово `uk`). Мова обирає розпізнавач: у PP-OCRv6
кирилиці немає, тож для `uk/ru/be/bg` береться `eslav_PP-OCRv5_mobile_rec`.
**Не заміна 4B-VLM:** рукопис 2/4 проти 4/4, зате друковані таблиці доходять до
фактів, а не лише до тексту (`docs/ocr_stage1_benchmark.md`). Бенчмарк —
`bench/run_ppocr_bench.py`; тести (`tests/test_ppocr.py`) paddleocr не
потребують.

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

Усі `hackathon/build_*.py` **не запустяться на цьому Mac без правок**:
- шрифти DejaVu очікуються за `/usr/share/fonts/truetype/dejavu/` (на macOS немає; локальна альтернатива — знайти TTF через `fc-list` або скористатися `DejaVuSans.ttf` з пакета reportlab/matplotlib);
- `build_submit.py` пише вивід у `/sessions/.../mnt/outputs/...` — шлях контейнера; замінити на локальний.

Мова контенту PDF — українська, тому шрифт обовʼязково з підтримкою кирилиці (стандартні PDF-шрифти reportlab не підійдуть).

## Архітектура (спільна для скелета і текстів)

Скелет `hackathon/livemedcard_fhir_skeleton.py` інстанціює фреймворк **TRACE** (Zabolotnii, arXiv:2605.03838) — шари знизу вгору:

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
