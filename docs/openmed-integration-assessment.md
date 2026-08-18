# OpenMed у LiveMedCard: оцінка інтеграції

| Поле | Значення |
|---|---|
| Статус | Запропоновано |
| Дата | 2026-08-18 |
| Аудиторія | Розробники та власник продукту LiveMedCard |

## Контекст

[OpenMed](https://github.com/maziyarpanahi/openmed) — Apache-2.0 SDK для локального клінічного NER і деідентифікації. Він повертає сутності з confidence та символьними offset-ами, підтримує локальні CPU/CUDA/MLX/ONNX runtime-и й має FHIR-експорт.

LiveMedCard уже має власний FHIR-сумісний домен, span-рівневий provenance, OCR і L2b-екстрактори, а сигнали L1/L2a детерміновано обчислюються на FactStore. Інтеграція не повинна порушити цю межу TRACE.

## Рішення

OpenMed **не включати до основного клінічного extraction path** і не використовувати як заміну `deterministic_extract`, NuExtract/LoRA Stage 2, моделей `livemedcard.fhir` або FHIR Bundle.

Доцільний сценарій — окремий опційний **privacy gateway**:

```text
OCR/текст -> OpenMed PII spans -> локальний audit
                              -> маскована транспортна копія -> cloud OCR Lab
Оригінал  -> локальний DocumentReference та provenance
```

Результати OpenMed можуть впливати лише на маскування транспортної копії. Вони не повинні безпосередньо створювати сигнали L1/L2a або рішення L3. Клінічні сутності мають пройти наявне кодування в LOINC/RxNorm/SNOMED і перевірку provenance.

## Обґрунтування

- PII-spans природно зіставляються з `Provenance.source_span`.
- Local-first inference відповідає моделі приватності LiveMedCard.
- NER-сутність не є повним KV-T фактом: їй бракує гарантованої комбінації коду, значення, одиниці, дати та джерела.
- OpenMed FHIR використовує, зокрема, `MedicationStatement`, тоді як LiveMedCard має власні моделі, стабільні ID, `MedicationRequest` і snapshot round-trip.
- Документація OpenMed маршрутизує українську на multilingual PII model, але model card не включає українську до 16 мов навчання. Якість українських ПІБ і клінічного тексту потребує окремої перевірки.
- ML runtime додає важкі залежності; тому OpenMed має залишатися optional extra або окремим локальним сервісом.

## Пілот і критерії прийняття

**PILOT-OM-001 — українська деідентифікація.**

- **Given** 50–100 синтетичних українських фрагментів із ПІБ, адресами, телефонами, email, датами народження, РНОКПП та номерами медичних документів,
- **When** OpenMed формує PII-spans і масковану копію,
- **Then** recall для критичних PII має бути не нижче 0.98, offset-и мають точно відтворювати вихідні spans, а жоден оригінальний PII-фрагмент не повинен потрапити в cloud payload.

Окремо виміряти precision, latency, RAM, розмір моделей і поведінку на OCR-помилках. До завершення пілота інтеграція не є production-ready.

## Реалізаційні обмеження

1. Не додавати OpenMed до базових dependencies.
2. Ізолювати адаптер за конфігураційним прапорцем; default і CI лишити offline без моделі.
3. Pin-ити версію SDK і окремо фіксувати модель, її hash та ліцензію.
4. Зберігати оригінальний текст тільки локально; масковану копію не використовувати як джерело provenance.
5. При помилці OpenMed забороняти cloud-відправлення, а не переходити до немаскованого fallback.

## Підсумок

Рекомендація: **пілотувати OpenMed як privacy gateway перед хмарними маршрутами; не використовувати як клінічне джерело істини або шар безпеки TRACE**.

## Джерела

- [OpenMed README](https://github.com/maziyarpanahi/openmed)
- [OpenMed language support](https://github.com/maziyarpanahi/openmed/blob/master/docs/languages.md)
- [Multilingual model card](https://huggingface.co/OpenMed/privacy-filter-multilingual)
- [OpenMed FHIR grounded exporter](https://github.com/maziyarpanahi/openmed/blob/master/openmed/clinical/exporters/fhir/grounded.py)
