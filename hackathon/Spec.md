# LiveMedCard OCR Lab — Software Requirements Specification

**Проєкт:** LiveMedCard / OCR & Structured Extraction Lab  
**Версія:** 0.1  
**Дата:** 2026-07-10  
**Статус:** Draft for review  
**Власник:** LiveMedCard project team  
**Цільова платформа:** Hugging Face Spaces ZeroGPU (публічний sandbox)

> Цей документ описує окремий OCR/structured-extraction сервіс на ZeroGPU. Він
> не замінює і не змінює local-first LiveMedCard. Публічний Space призначений
> лише для синтетичних або повністю деідентифікованих документів.

## 1. Призначення та межі

### 1.1. Мета

OCR Lab має дати користувачу можливість завантажити зображення медичного
документа, отримати дослівну транскрипцію або markdown-представлення, а також
структуровані факти у форматі, який може бути переданий у LiveMedCard.

Сервіс відповідає лише за розпізнавання та структурування. Він не визначає
клінічну небезпеку, не ставить діагнозів, не змінює лікування і не замінює
детерміновані шари L1/L2a та політику ескалації L3.

### 1.2. Цілі релізу

1. Показати наскрізний сценарій image → text/markdown → KVT4 → LiveMedCard.
2. Зберегти чітку межу: модель витягує факти, а безпекові сигнали формуються
   поза моделлю на FactStore.
3. Дати відтворюваний публічний demo на ZeroGPU без внесення змін до основної
   FastAPI-архітектури.
4. Підготувати адаптер, який у майбутньому зможе працювати з локальним
   llama-server, приватним GPU-endpoint або іншим OpenAI-сумісним backend без
   зміни DTO LiveMedCard.

### 1.3. Нецілі

- Прийом реальних персональних медичних даних у публічному Space.
- Постановка діагнозів або формування рекомендацій щодо лікування.
- Автоматичне визначення взаємодій ліків, дозових меж чи клінічних ризиків.
- Повна відповідність FHIR R4, MDR, EU AI Act, GDPR або іншому режиму
  регуляторної сертифікації.
- Гарантування клінічної точності без окремої валідації на контрольованому
  наборі документів.
- Постійне високонавантажене API: ZeroGPU є демонстраційною інфраструктурою з
  чергою та квотами.

## 2. Контекст продукту

Основний LiveMedCard уже має два шляхи обробки:

1. stub — офлайн-детермінований екстрактор для CI, демонстрації та
   відтворюваних тестів.
2. локальні LLM-режими (two_stage, two_stage_lora, nuextract) через
   OpenAI-сумісні локальні сервери.

OCR Lab є третім, зовнішнім шляхом, який вмикається явно:

~~~mermaid
flowchart LR
    U[Користувач з синтетичним документом] --> UI[Gradio OCR Lab]
    UI --> M[NuExtract3 або MedGemma]
    M --> T[Markdown transcription]
    M --> K[KVT4 facts JSON]
    K --> A[OCR Lab adapter]
    A --> LMC[LiveMedCard ingest]
    LMC --> F[FactStore]
    LMC --> G[KnowledgeGraph]
    F --> S[L1/L2a deterministic signals]
    S --> R[L3 routing to human]
~~~

### 2.1. Ролі

| Роль | Відповідальність |
|---|---|
| Demo user | Завантажує синтетичний документ і переглядає результат |
| Caregiver persona | Оцінює зрозумілість timeline та provenance |
| Developer | Запускає тести, оновлює шаблон і model adapter |
| Maintainer | Контролює модель, версію Space, квоти та журнали |
| Human reviewer | Перевіряє сумнівні або високоризикові факти в LiveMedCard |

## 3. Архітектура рішення

### 3.1. Компоненти

| Компонент | Роль | Технологія/режим |
|---|---|---|
| ocr_lab/app.py | Gradio UI та orchestration | Python, Gradio |
| Image validator | Розмір, MIME, декодування, RGB-нормалізація | Pillow |
| Model adapter | Єдиний контракт для NuExtract3/MedGemma | Python Protocol |
| NuExtract3 adapter | image→markdown та image/text→JSON | Transformers/PyTorch |
| MedGemma adapter | Альтернативний medical vision backend | Transformers/PyTorch, gated model |
| Schema validator | Перевірка template output і типів | Pydantic або JSON Schema |
| KVT4 normalizer | Нормалізація keyword, value, unit, timestamp | Python |
| Gradio API client adapter | Опційна інтеграція з LiveMedCard | gradio_client або HTTP wrapper |
| LiveMedCard | Факти, сигнали, Q&A та provenance | існуючий livemedcard/ |

### 3.2. Інваріанти

1. OCR Lab не викликає L1/L2a/L3 і не генерує Signal або Decision.
2. Непрочитане значення маркується як unreadable/null; модель не повинна
   вигадувати число.
3. Кожен факт має посилання на source_document_id і спосіб отримання.
   **Реалізовано як інваріант** у `pipeline._assert_provenance`: факт без
   `source_doc_id` відхиляється на межі екстракції (`ValueError`), тож кожен
   триплет KV-T гарантовано має документ-джерело й провенанс-ребро графа.
4. Структурований результат не вважається доведеним фактом, поки його не
   прийняв downstream validator і не збережено з provenance.
5. Публічна версія не повинна зберігати завантажені зображення або повний текст
   документа у власному persistent storage.
6. Відмова OCR Lab не повинна ламати або очищати поточний стан LiveMedCard.

### 3.3. Розгортання

ZeroGPU-прослойка має бути окремим Space, а не модифікацією поточного Docker
Space LiveMedCard. ZeroGPU викликається тільки всередині GPU-декорованої
функції; модель завантажується під час старту застосунку за правилами платформи.

Рекомендована початкова конфігурація:

| Параметр | Значення |
|---|---|
| SDK | Gradio 4+ |
| Hardware | ZeroGPU large |
| GPU function | @spaces.GPU(duration=120) для OCR/extraction POC |
| Python | 3.12 або версія, дозволена поточним ZeroGPU |
| Model | numind/NuExtract3 (основний) |
| Alternative | google/medgemma-1.5-4b-it після прийняття умов моделі |
| Storage | ephemeral; persistent volume не використовувати |
| Public data policy | synthetic/de-identified only |

Значення duration=120 є цільовим верхнім лімітом функції, а не гарантією
затримки. Реальний час залежить від черги, cold start, розміру зображення та
довжини відповіді.

## 4. Функціональні вимоги

### 4.1. Застосунок OCR Lab

**FR-OCR-001: Завантаження зображення**  
Система повинна приймати одне зображення документа через Gradio.

- **Given:** користувач відкрив OCR Lab.
- **When:** він обирає PNG, JPEG, WebP або HEIC, підтриманий runtime.
- **Then:** система показує preview та переходить до валідації.

**FR-OCR-002: Попередження про дані**  
До завантаження система повинна показувати помітне попередження: «Не
завантажуйте реальні персональні медичні дані. Це публічний demonstration
sandbox».

- **Given:** користувач бачить форму завантаження.
- **When:** він натискає Submit.
- **Then:** UI показує попередження до початку inference.

**FR-OCR-003: Валідація зображення**  
Система повинна відхиляти порожні, нерозкодовані, непідтримувані або надмірно
великі зображення.

- Максимальний розмір файлу: 15 MB.
- Максимальний розмір після декодування: 4096×4096 px.
- Повідомлення про помилку не повинно містити stack trace або secrets.

**FR-OCR-004: Дослівна транскрипція**  
У режимі transcription система повинна повертати текст/markdown документа зі
збереженням оригінальної мови, чисел, одиниць і дат.

- **Given:** документ успішно декодований.
- **When:** виконується inference.
- **Then:** результат містить лише транскрипцію без діагнозу чи поради.

**FR-OCR-005: Структуроване витягання**  
У режимі structured система повинна повертати JSON за фіксованим шаблоном
KVT4.

- **Given:** документ містить лабораторні дані.
- **When:** модель і validator завершили роботу.
- **Then:** результат відповідає схемі KVT4 або повертає явну помилку валідації.

**FR-OCR-006: Два результати одного документа**  
Система повинна показувати markdown та структурований JSON одночасно, якщо
обрано режим full.

- Markdown є доказовою free-text іпостассю.
- JSON є candidate facts і не є самостійним клінічним висновком.

**FR-OCR-007: Невпевненість**  
Для нерозбірливих ділянок система повинна повертати null або unreadable, а не
число, отримане з припущення.

**FR-OCR-008: Повторний запуск**  
Однакове зображення та однаковий model/template version повинні давати
ідентичний або еквівалентний результат у режимі deterministic inference.

### 4.2. KVT4 та downstream integration

**FR-KVT-001: Формат KVT4**  
KVT4 існує у двох формах, які не можна плутати:

1. **Raw model output** — те, що модель повертає строго за шаблоном `kvt4-v1`
   (§7.1): `cluster, keyword, value, unit, timestamp, unreadable, source_span`.
2. **Normalized fact** — результат KVT4 normalizer, який додає
   `source_document_id`, `confidence` і (опційно) код-мапінг, нічого не
   вигадуючи. Саме normalized fact передається downstream.

Приклад normalized fact:

~~~json
{
  "cluster": "lab",
  "keyword": "creatinine",
  "value": 121.0,
  "unit": "µmol/L",
  "timestamp": "2024-04-09",
  "unreadable": false,
  "source_span": null,
  "source_document_id": "ocr-demo-001",
  "confidence": null
}
~~~

Обов'язкові поля (обидві форми): cluster, keyword, value, timestamp.
`unreadable` обов'язкове, коли value = null (див. FR-KVT-002). `unit` і
`source_span` походять із raw output; `source_document_id` та `confidence`
додає normalizer, якщо їх можна визначити без вигадування.

**FR-KVT-002: Типи**

- cluster — enum: lab, vital, medication, condition, other.
- keyword — непорожній рядок, максимум 200 символів.
- value — число або null лише разом із unreadable=true.
- timestamp — ISO-8601 date/date-time або null з ознакою date_missing.
- unit — оригінальна одиниця, не автоматично конвертована без надійного
  mapping.

**FR-KVT-003: Міст keyword→LOINC/RxNorm/SNOMED**  
Нормалізація кодів повинна бути окремим детермінованим кроком. Якщо keyword не
має підтвердженого mapping, факт зберігається як free-form і не використовується
для L1/L2a сигналу, доки код не підтверджено. Використовувати наявний
детермінований міст keyword→LOINC із `l2b_stage2_lora` (`_KEYWORD_LOINC`,
`_resolve_loinc`), а не новий, щоб коди збігалися з local-шляхом.

**FR-KVT-004: Передача у LiveMedCard**  
Інтеграційний адаптер повинен передавати до основного застосунку тільки
validated facts і markdown provenance. При недоступності ZeroGPU endpoint:

- UI показує помилку та можливість повторити;
- існуючий стан картки залишається незмінним;
- користувач може перейти до текстового stub-режиму.

### 4.3. Model adapter

**FR-MODEL-001: Єдиний контракт**  
Кожна модель повинна реалізовувати логічний контракт:

~~~python
class DocumentModel(Protocol):
    def image_to_markdown(self, image: Image.Image) -> str: ...
    def extract_facts(self, text_or_image: object, template: dict) -> dict: ...
~~~

**FR-MODEL-002: Версіювання**  
Результат повинен містити model_id, model_revision, template_version і
runtime_mode (zerogpu, local, stub).

**FR-MODEL-003: Model failure**  
Timeout, OOM, порожня відповідь або невалідний JSON повинні повертатися як
типізована помилка InferenceError, ValidationError або TimeoutError.

**FR-MODEL-004: Повторне використання наявного коду**  
Адаптери НЕ реалізують екстракцію заново, а обгортають наявні модулі
LiveMedCard:

- `livemedcard/layers/l2b_nuextract.py` — `image_to_markdown()` (image→markdown)
  та `extract_facts()` (text→KVT4) через OpenAI-сумісний ендпойнт;
- `livemedcard/layers/l2b_stage2_lora.py` — детермінований міст keyword→LOINC
  (`_KEYWORD_LOINC`, `_resolve_loinc`) і `to_observations()` для KVT4→FHIR.

Відмінність ZeroGPU-адаптера від локального — лише транспорт (in-process
GPU-виклик замість HTTP на llama-server); контракт, шаблон і normalizer спільні.
Це усуває ризик розбіжності логіки між cloud і local шляхами.

## 5. Інтерфейси

### 5.1. Gradio UI

UI має містити:

1. попередження про synthetic/de-identified data;
2. image upload і preview;
3. режими transcription, structured, full;
4. поле або read-only preview JSON template;
5. кнопку Submit і стани queued / processing / success / error;
6. вкладки Markdown, KVT4 JSON, Diagnostics;
7. кнопку Copy/Download результату без автоматичного збереження на сервері;
8. model/version/runtime badge.

### 5.2. Логічний API-контракт

Це логічний контракт між адаптером і Space; конкретна Gradio API-назва може
змінюватися без зміни схеми.

**Request**

~~~json
{
  "image": "multipart upload or Gradio file handle",
  "mode": "full",
  "template_version": "kvt4-v1",
  "source_document_id": "ocr-demo-001"
}
~~~

**Response**

~~~json
{
  "status": "success",
  "markdown": "# Laboratory report\n...",
  "facts": [
    {
      "cluster": "lab",
      "keyword": "creatinine",
      "value": 121.0,
      "unit": "µmol/L",
      "timestamp": "2024-04-09",
      "unreadable": false,
      "source_span": null,
      "source_document_id": "ocr-demo-001",
      "confidence": null
    }
  ],
  "diagnostics": {
    "model_id": "numind/NuExtract3",
    "model_revision": "pinned revision",
    "template_version": "kvt4-v1",
    "runtime_mode": "zerogpu",
    "latency_ms": 18200,
    "warnings": []
  }
}
~~~

**Error response**

~~~json
{
  "status": "error",
  "error_code": "INVALID_IMAGE | MODEL_TIMEOUT | INVALID_OUTPUT | QUEUED_OUT",
  "message": "Human-readable safe message",
  "retryable": true,
  "diagnostics": {
    "model_id": "numind/NuExtract3",
    "template_version": "kvt4-v1"
  }
}
~~~

### 5.3. Інтерфейс до поточного LiveMedCard

Адаптер повинен мапити результат до наявного DTO:

~~~text
markdown → DocumentReference/content.data
KVT4     → Observation / MedicationRequest / Condition candidate
source   → Provenance.source_doc_id
facts    → FactStore після валідації
signals  → тільки LiveMedCard L1/L2a
~~~

OCR Lab не повинен напряму записувати в FactStore, обходити LiveMedCard.ingest
або створювати Signal.

## 6. Нефункціональні вимоги

### 6.1. Продуктивність

| ID | Вимога | Ціль POC |
|---|---|---|
| NFR-PERF-001 | Час обробки warm request | p95 ≤ 45 s |
| NFR-PERF-002 | Cold start/model load | показувати окремо; не маскувати під inference |
| NFR-PERF-003 | Timeout | не перевищувати налаштований ZeroGPU duration |
| NFR-PERF-004 | Payload | ≤15 MB upload, ≤4096×4096 px |
| NFR-PERF-005 | Queue feedback | UI показує queued/processing state |

Ці значення є цілями демонстраційного POC, а не SLA.

### 6.2. Safety

**NFR-SAFE-001:** результат OCR не повинен містити лікувальних рекомендацій.  
**NFR-SAFE-002:** якщо модель повертає медичний висновок, він має бути
відкинутий із попередженням, а не показаний як результат extraction.  
**NFR-SAFE-003:** жоден candidate fact не може самостійно викликати L1/L2a без
проходження schema/code validation.  
**NFR-SAFE-004:** timestamp, unit і number мають зберігатися дослівно, якщо
нормалізація не має підтвердженого правила.  
**NFR-SAFE-005:** у UI і документації всюди використовувати термін
«candidate fact» до моменту прийняття downstream pipeline.

### 6.3. Privacy та security

**NFR-SEC-001:** публічний Space має явно забороняти реальні PHI/PII.  
**NFR-SEC-002:** зображення й повний OCR output не записуються в application
logs, telemetry або persistent volume.  
**NFR-SEC-003:** secrets і model tokens зберігаються тільки в HF Secrets або
environment variables, ніколи не в репозиторії.  
**NFR-SEC-004:** усі зовнішні виклики використовують HTTPS.  
**NFR-SEC-005:** передача з основного LiveMedCard у ZeroGPU можлива тільки у
явному cloud_demo режимі.  
**NFR-SEC-006:** приватний/production deployment повинен мати authentication,
patient isolation, encryption at rest, retention/deletion policy і audit trail.

### 6.4. Надійність та підтримуваність

**NFR-QUAL-001:** модель і template pin-яться за revision/version.  
**NFR-QUAL-002:** кожна зміна моделі проходить golden corpus і schema tests.  
**NFR-QUAL-003:** основний LiveMedCard має працювати, коли OCR Lab недоступний.  
**NFR-QUAL-004:** код адаптера не повинен залежати від UI-деталей Gradio.  
**NFR-QUAL-005:** результати та помилки мають бути детерміновано серіалізовані.  
**NFR-QUAL-006:** public demo має мати health/version diagnostics без розкриття
вхідних даних.

## 7. Дані та provenance

### 7.1. KVT4 template v1 (raw model output)

Це шаблон, за яким модель повертає raw output. KVT4 normalizer перетворює
кожен елемент на normalized fact (§4.2, FR-KVT-001), додаючи
`source_document_id` і `confidence` та не вигадуючи відсутніх значень.

~~~json
{
  "facts": [
    {
      "cluster": "lab | vital | medication | condition | other",
      "keyword": "",
      "value": null,
      "unit": null,
      "timestamp": null,
      "unreadable": false,
      "source_span": null
    }
  ]
}
~~~

source_span є опційним у POC, але бажаним для перевірки людиною. Якщо модель
не може підтвердити span, поле залишається null. **Наскрізно (реалізовано):**
для детермінованого екстрактора (Stub/TwoStage) span рахується з regex-offset-ів
і доходить до `Provenance.source_span` → `KVTFact.source_span`, вказуючи у
збережену free-text іпостась (`DocumentReference.content.data`); для LLM-KVT-
шляхів (LoRA/NuExtract/OCR Lab) лишається null.

### 7.2. Provenance

Для кожного запуску зберігаються лише metadata:

- source_document_id;
- model_id та pinned revision;
- template_version;
- runtime_mode;
- час запуску та latency;
- статус і код помилки;
- hash зображення за потреби для дедуплікації.

Повний input image hash не є анонімізацією. Він не повинен використовуватися
як заміна de-identification.

## 8. Тестування та критерії приймання

### 8.1. Test layers

1. **Unit:** image validation, MIME, size limits, KVT4 schema, enum values,
   timestamp parsing, unreadable handling.
2. **Golden corpus:** синтетичні UA/EN документи з відомими values, units, dates,
   tables, multi-column layouts і deliberately unreadable fields.
3. **Model regression:** value accuracy, unit accuracy, date accuracy, field
   recall, invalid-output rate, unsafe-claim rate.
4. **Integration:** Gradio adapter → mocked LiveMedCard ingest; ZeroGPU error;
   timeout; retry; fallback to stub.
5. **Browser smoke:** upload, queue state, result tabs, copy/download, error UI.
6. **Privacy smoke:** application logs не містять image bytes, OCR text,
   base64 payload або full JSON result.

### 8.2. Мінімальні acceptance criteria

Реліз POC приймається, якщо:

- усі unit/schema/integration tests проходять;
- 10 синтетичних документів обробляються без ручного втручання;
- жодне deliberately unreadable numeric поле не перетворюється на число;
- усі повернуті факти мають source_document_id;
- malformed output моделі не потрапляє в LiveMedCard;
- ZeroGPU timeout не змінює стан картки;
- працює fallback на існуючий stub режим;
- README/Space card містить warning про synthetic data;
- у звіті є model revision, template version і дата eval.

### 8.3. Метрики

Обов'язково вимірювати окремо:

- value_accuracy — правильне числове значення;
- unit_accuracy;
- date_accuracy;
- fact_precision і fact_recall;
- invalid_json_rate;
- unreadable_false_positive_rate;
- unsafe_claim_rate;
- p50/p95 latency та timeout rate.

Одне число «90% accuracy» без визначення знаменника, corpus, model revision і
split не вважається прийнятним результатом.

## 9. Моніторинг та операції

Окремо збираються технічні counters:

- total requests;
- success/error/timeout;
- queue wait time;
- inference time;
- model load time;
- output validation failures;
- fallback count.

Не збирати повні зображення, OCR text або candidate facts у звичайну telemetry.
Для debugging використовувати synthetic fixtures і redacted traces.

### 9.1. Відновлення після збою

Якщо ZeroGPU Space недоступний:

1. UI показує retryable error.
2. Main LiveMedCard не очищається і не перезаписується.
3. Demo продовжується через заздалегідь підготовлений stub сценарій.
4. Incident записується лише як технічна подія без input data.

## 10. Ризики та пом'якшення

| Ризик | Вплив | Пом'якшення |
|---|---|---|
| ZeroGPU queue/cold start | Зламаний live demo | Не робити його єдиним demo path; warm-up перед показом |
| Витік PHI у public Space | Критичний privacy risk | Synthetic-only policy, warning, no persistence |
| OCR hallucination | Хибний факт у timeline | unreadable/null policy, schema validation, human review |
| Невірний keyword→LOINC | Хибний L2a signal | deterministic mapping, unknown→free-form |
| Модель генерує пораду | Safety/regulatory risk | post-validator, reject unsafe output, strict prompt |
| Зміна model revision | Regression | pin revision, golden corpus, model card |
| Невалідний JSON | Pipeline failure | typed error, no partial ingest |
| Різний формат між Gradio і FastAPI | Integration debt | adapter contract + mocked integration tests |
| Gated MedGemma access | Blocked deployment | NuExtract3 as default; accept model terms separately |
| Висока вартість/квота | Непередбачуване використання | synthetic demo, rate limit, separate public sandbox |

## 11. План реалізації

### Phase 0 — Hackathon-safe demo

- окремий Gradio Space;
- NuExtract3 або підготовлений mocked result;
- synthetic document gallery;
- markdown + KVT4 JSON;
- ручний copy/paste у LiveMedCard;
- README із чітким disclaimer;
- live-demo fallback через основний stub Space.

### Phase 1 — Developer integration

- gradio_client adapter;
- versioned KVT4 schema;
- automated golden corpus;
- cloud_demo opt-in flag у клієнті;
- typed errors, timeout і fallback.

### Phase 2 — Private pilot

- private/protected Space або власний GPU endpoint;
- authentication і patient isolation;
- encrypted transient storage;
- retention/deletion policy;
- de-identified validation set;
- audit and monitoring.

### Phase 3 — Production research track

- клінічна та prospective validation;
- формальна risk management документація;
- профіль FHIR R4 із validator;
- regulatory assessment до будь-яких clinical claims.

## 12. Traceability matrix

| Ціль | Вимоги | Тест/доказ |
|---|---|---|
| Транскрипція документа | FR-OCR-001…004 | golden OCR corpus |
| Структуровані факти | FR-OCR-005…008, FR-KVT-001…003 | schema + regression tests |
| Безпечна інтеграція | FR-KVT-004, NFR-SAFE-001…005 | mocked adapter + failure tests |
| Privacy-first sandbox | FR-OCR-002, NFR-SEC-001…006 | UI/privacy smoke tests |
| Відтворюваність | FR-MODEL-002, NFR-QUAL-001…005 | pinned revision + metadata |
| Демо-надійність | NFR-PERF-001…005 | warm-up, timeout, fallback runbook |

## 13. Definition of Done

Документ і реалізація вважаються готовими до POC, коли:

- Space запускається на ZeroGPU з pinned model revision;
- warning про synthetic/de-identified data видно до upload;
- transcription, structured і full режими працюють;
- KVT4 JSON проходить schema validation;
- markdown має provenance metadata;
- unknown keywords не дають автоматичного L1/L2a сигналу;
- помилки ZeroGPU не змінюють стан LiveMedCard;
- є fallback на stub;
- є golden corpus, regression report і latency report;
- у пітчі чітко розділено local-first product mode та public OCR sandbox.

## 14. Глосарій

| Термін | Значення |
|---|---|
| OCR | Optical Character Recognition; розпізнавання тексту із зображення |
| VLM | Vision-Language Model |
| KVT4 | cluster–keyword–value–timestamp structured fact format |
| Candidate fact | Факт, запропонований моделлю, але ще не прийнятий pipeline |
| Provenance | Інформація про джерело та спосіб отримання факту |
| ZeroGPU | Динамічно виділювана GPU-інфраструктура Hugging Face Spaces |
| Stub mode | Детермінований offline extractor для демо і тестів |
| Local-first | Медичні дані обробляються локально за замовчуванням |
| L1/L2a | Детерміновані safety/trend шари LiveMedCard |
| L3/L4 | Policy routing та human review у TRACE |

## 15. References

- [LiveMedCard README](README.md)
- [LiveMedCard architecture](ARCHITECTURE.md)
- [LiveMedCard review and roadmap](REVIEW_AND_ROADMAP.md)
- [Hugging Face ZeroGPU documentation](https://huggingface.co/docs/hub/main/en/spaces-zerogpu)
- [NuExtract3 model card](https://huggingface.co/numind/NuExtract3)
- [MedGemma 1.5 model card](https://huggingface.co/google/medgemma-1.5-4b-it)
- [HL7 FHIR R4 Observation](https://hl7.org/fhir/R4/observation.html)

