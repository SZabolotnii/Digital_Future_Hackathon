"""L2b — розуміння вільного тексту.

У MVP детермінований stub замість MedGemma; екстрактор НЕ робить висновків
про безпеку, лише витягує факти (лабораторні показники, ліки, діагнози)
з українського вільного тексту у FHIR-ресурси.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Optional, Protocol, TypeVar

from ..config import (
    STAGE1_MODEL,
    STAGE1_URL,
    STAGE2_MODEL,
    STAGE2_URL,
    STUB_CONFIDENCE,
    UNVERIFIED_CONFIDENCE,
)
from ..calibration import confidence_for
from ..dto import Document, ExtractionResult
from ..fhir import (
    Attachment,
    CodeableConcept,
    Coding,
    Condition,
    DocumentReference,
    MedicationRequest,
    Observation,
    Provenance,
    Quantity,
    Reference,
    Sys,
)
from ..config import NUEXTRACT_MODEL, NUEXTRACT_URL, OCRLAB_SPACE
from ..reference import load_vocab
from ..units import UNIT_PATTERN
from ..versioning import agent
from . import l2b_nuextract, l2b_ocrlab, l2b_stage1, l2b_stage2_lora

_T = TypeVar("_T")

# Значення показника і дозу шукаємо ТІЛЬКИ впритул після терміна (між ними —
# лише роздільники), а не пошуком уперед по всьому тексту. Інакше показник чи
# ліки без власного числа «крадуть» число наступного терміна (напр. систолічний
# тиск забирав би значення калію, а варфарин — дозу аспірину).
_SEP = r"[\s:=–—-]*"
# Одиницю читаємо З ДОКУМЕНТА, а не беремо зі словника. Це не косметика:
# «Fasting glucose 122 mg/dL», записана як 122 mmol/L, дає major-сигнал
# `range_breach` на майже нормальному значенні, а запобіжник `unit_mismatch`
# (для якого й писався `units.py`) не спрацьовує ніколи — бо одиниця факту за
# побудовою дорівнює референсній. Знайшов це `bench/run_extraction_bench.py`.
_LAB_VALUE_RE = re.compile(
    _SEP + r"(\d+(?:\.\d+)?)\s*(" + UNIT_PATTERN + r")?", re.IGNORECASE
)
_DOSE_MG_RE = re.compile(_SEP + r"(\d+(?:\.\d+)?)\s*(?:мг|mg)\b", re.IGNORECASE)
# Кратність прийому: «×2», «x2», «2 рази», «двічі», «тричі», «twice», «3 times»
# → множник за добу. Словник білінгвальний, тож і доза має читатися двома мовами:
# інакше в англомовному документі доза просто не знаходиться, і сигнал
# перевищення добової межі (L1) тихо не спрацьовує.
_FREQ_RE = re.compile(
    # «трич», не «тріч»: у слові «тричі» стоїть «и». Через цю одну літеру
    # кратність не знаходилась, і добова доза виходила втричі меншою за
    # призначену — тобто перевищення межі могло тихо не спрацювати.
    r"[×xх]\s*(\d+)|(\d+)\s*раз|(двіч)|(трич)|(\d+)\s*times|(twice)|(three times)",
    re.IGNORECASE,
)
# Дата вимірювання на факт: ISO «2024-03-15» або «15.03.2024» у тому самому рядку.
_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{2})\.(\d{2})\.(\d{4})\b")


def _stable_id(prefix: str, *parts: object) -> str:
    """Детермінований id ресурсу від його змісту, а не порядку знахідки.

    Стабільний між запусками і версіями словника: зміна порядку матчингу не
    змінює id, тож ідемпотентність FactStore переживає повторний витяг і
    відновлення зі старих знімків. Ідентичний зміст (той самий документ, код,
    значення, час) навмисно дає той самий id — це один факт.
    """
    key = "|".join(str(p) for p in parts)
    return f"{prefix}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"


def _parse_effective_date(
    text: str, pos: int, fallback: Optional[datetime]
) -> Optional[datetime]:
    """Дата факту з решти поточного рядка (після значення); інакше `fallback`.

    Шукаємо ISO/dotted-дату лише до кінця рядка, щоб дата з наступного факту не
    «перетікала» на попередній. Повертаємо tz-aware UTC-опівніч.
    """
    eol = text.find("\n", pos)
    segment = text[pos:] if eol == -1 else text[pos:eol]
    m = _DATE_RE.search(segment)
    if m is None:
        return fallback
    if m.group(1):  # ISO YYYY-MM-DD
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:  # DD.MM.YYYY
        d, mo, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
    try:
        return datetime(y, mo, d, tzinfo=timezone.utc)
    except ValueError:  # напр. 2024-13-40 — ігноруємо, лишаємо fallback
        return fallback


def _parse_daily_dose_mg(text: str, pos: int) -> Optional[float]:
    """Добова доза в мг: разова доза «N мг» × кратність прийому (за замовч. 1)."""
    m = _DOSE_MG_RE.match(text, pos)
    if m is None:
        return None
    single = float(m.group(1))
    freq = 1
    fm = _FREQ_RE.search(text, m.end(), m.end() + 25)  # вікно тієї самої фрази
    if fm:
        if fm.group(1):
            freq = int(fm.group(1))
        elif fm.group(2):
            freq = int(fm.group(2))
        elif fm.group(3):
            freq = 2  # двічі
        elif fm.group(4):
            freq = 3  # тричі
        elif fm.group(5):
            freq = int(fm.group(5))  # «2 times»
        elif fm.group(6):
            freq = 2  # twice
        elif fm.group(7):
            freq = 3  # three times
    return single * freq


class Extractor(Protocol):
    """Контракт L2b-екстрактора."""

    def extract(self, doc: Document, patient_id: str) -> ExtractionResult: ...


def _find_vocab_terms(
    text: str, vocab_terms: dict[str, _T]
) -> list[tuple[str, _T, re.Match[str]]]:
    """Знаходить УСІ входження кожного терміна в тексті.

    Довші (багатослівні) терміни перевіряються першими, щоб виграти при
    перекритті зі коротшими. Результат впорядкований за позицією входження
    в тексті.
    """
    terms_by_length_desc = sorted(vocab_terms.items(), key=lambda kv: -len(kv[0]))
    claimed_spans: list[tuple[int, int]] = []
    found: list[tuple[int, str, _T, re.Match[str]]] = []
    for term, info in terms_by_length_desc:
        pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
        # Усі входження терміна, а не лише перше — щоб не губити повторні
        # вимірювання одного показника в межах документа.
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < claimed_end and end > claimed_start for claimed_start, claimed_end in claimed_spans):
                continue
            claimed_spans.append((start, end))
            found.append((start, term, info, match))
    found.sort(key=lambda item: item[0])
    return [(term, info, match) for _, term, info, match in found]


def deterministic_extract(
    text: str, doc: Document, patient_id: str, *,
    extracted_by: str = "StubExtractor", prompt_id: str | None = None,
) -> ExtractionResult:
    """Детермінований Stage 2: текст → FHIR-ресурси (LOINC/RxNorm/SNOMED).

    Спільне ядро для `StubExtractor` (вхід — сирий `doc.text`) і `TwoStageExtractor`
    (вхід — Stage 1 SGR-summary). Дата обзервації береться з рядка факту, інакше
    fallback на `doc.received_at` — тож на тексті без дат id/час не змінюються.
    """
    vocab = load_vocab()
    subject = Reference(reference=f"Patient/{patient_id}")

    observations: list[Observation] = []
    seen_obs: set[str] = set()
    for term, info, match in _find_vocab_terms(text, vocab.labs):
        number_match = _LAB_VALUE_RE.match(text, match.end())
        if number_match is None:
            continue
        eff_date = _parse_effective_date(text, number_match.end(), doc.received_at)
        obs_id = _stable_id(
            "obs", doc.id, info.loinc, number_match.group(1), eff_date
        )
        if obs_id in seen_obs:  # ідентична повторна згадка — той самий факт
            continue
        seen_obs.add(obs_id)
        observations.append(
            Observation(
                id=obs_id,
                status="final",
                code=CodeableConcept(
                    coding=[
                        Coding(
                            system=Sys.LOINC,
                            code=info.loinc,
                            display=info.display,
                        )
                    ],
                    text=term.capitalize(),
                ),
                subject=subject,
                effectiveDateTime=eff_date,
                valueQuantity=Quantity(
                    # Одиниці немає в тексті → None, а не підставлена зі
                    # словника: L2a тоді чесно скаже «перевірку меж не виконано».
                    value=float(number_match.group(1)),
                    unit=number_match.group(2) or None,
                ),
                provenance=Provenance(
                    source_doc_id=doc.id,
                    extracted_by=extracted_by,
                    prompt_id=prompt_id,
                    confidence=STUB_CONFIDENCE,
                    source_span=(match.start(), number_match.end()),
                ),
            )
        )

    medications: list[MedicationRequest] = []
    seen_med: set[str] = set()
    for term, info, match in _find_vocab_terms(text, vocab.medications):
        dose_mg_per_day: Optional[float] = _parse_daily_dose_mg(text, match.end())
        med_id = _stable_id("med", doc.id, info.rxnorm, dose_mg_per_day)
        if med_id in seen_med:
            continue
        seen_med.add(med_id)
        medications.append(
            MedicationRequest(
                id=med_id,
                status="active",
                intent="order",
                medicationCodeableConcept=CodeableConcept(
                    coding=[
                        Coding(
                            system=Sys.RXNORM,
                            code=info.rxnorm,
                            display=info.display,
                        )
                    ],
                    text=term.capitalize(),
                ),
                subject=subject,
                dose_mg_per_day=dose_mg_per_day,
                provenance=Provenance(
                    source_doc_id=doc.id,
                    extracted_by=extracted_by,
                    prompt_id=prompt_id,
                    confidence=STUB_CONFIDENCE,
                    source_span=(match.start(), match.end()),
                ),
            )
        )

    conditions: list[Condition] = []
    seen_cond: set[str] = set()
    for term, info, match in _find_vocab_terms(text, vocab.conditions):
        cond_id = _stable_id("cond", doc.id, info.snomed)
        if cond_id in seen_cond:
            continue
        seen_cond.add(cond_id)
        conditions.append(
            Condition(
                id=cond_id,
                subject=subject,
                code=CodeableConcept(
                    coding=[
                        Coding(
                            system=Sys.SNOMED,
                            code=info.snomed,
                            display=info.display,
                        )
                    ],
                    text=term,
                ),
                clinicalStatus="active",
                onsetDateTime=doc.received_at,
                provenance=Provenance(
                    source_doc_id=doc.id,
                    extracted_by=extracted_by,
                    prompt_id=prompt_id,
                    confidence=STUB_CONFIDENCE,
                    source_span=(match.start(), match.end()),
                ),
            )
        )

    # Порожній витяг означає «нічого не розпізнали», а не «впевнені на 100%».
    # Низька впевненість веде документ на перегляд людиною (L3), а не в auto.
    #
    # Ненульова впевненість — ВИМІРЯНА (нижня межа Вілсона для recall на
    # gold-корпусі), а не константа з конфігу. `STUB_CONFIDENCE` лишається
    # відкатом на випадок, коли калібрування ще не проганяли: поріг ескалації
    # виводиться з вартостей до третього знака, і порівнювати його з
    # непоміряним числом означало б імітувати строгість.
    measured = confidence_for("StubExtractor")
    mean_confidence = (
        (measured if measured is not None else STUB_CONFIDENCE)
        if (observations or medications or conditions) else 0.0
    )
    return ExtractionResult(
        observations=observations,
        conditions=conditions,
        medications=medications,
        mean_confidence=mean_confidence,
    )


class StubExtractor:
    """Детермінований stub-екстрактор: Stage 2 напряму по сирому `doc.text`.

    Сирий текст зберігається як `DocumentReference` (free-text іпостась) — це та
    сама база, у яку вказують `source_span`-и фактів, і джерело freetext-QA у демо.
    """

    def extract(self, doc: Document, patient_id: str) -> ExtractionResult:
        ext = deterministic_extract(doc.text, doc, patient_id)
        ext.document_reference = _build_docref(
            doc, doc.text, patient_id,
            extracted_by="StubExtractor", title=f"raw text: {doc.id}",
        )
        return ext


def _build_docref(
    doc: Document,
    summary_text: str,
    patient_id: str,
    *,
    extracted_by: str,
    prompt_id: str | None = None,
    title: str | None = None,
) -> DocumentReference:
    """FHIR DocumentReference з free-text іпостассю (Stage 1 SGR-summary або сирий
    текст). ``summary_text`` — та база, у яку вказують ``Provenance.source_span``."""
    return DocumentReference(
        id=f"docref-{doc.id}",
        status="current",
        subject=Reference(reference=f"Patient/{patient_id}"),
        date=doc.received_at,
        type=CodeableConcept(text=doc.kind),
        content=Attachment(
            contentType="text/plain",
            data=summary_text,
            title=title or f"SGR summary: {doc.id}",
        ),
        provenance=Provenance(
            source_doc_id=doc.id,
            extracted_by=extracted_by,
            prompt_id=prompt_id,
            confidence=STUB_CONFIDENCE,
        ),
    )


class TwoStageExtractor:
    """Двоетапний L2b: Stage 1 (LLM SGR-стиснення) → Stage 2 (детермінована KV-T).

    Дає дві іпостасі одного документа: `DocumentReference` (free-text summary) і
    структуровані FHIR-факти. Потребує живого локального LLM-сервера (LM Studio).
    """

    def __init__(self, *, model: str | None = None) -> None:
        self._model = model

    def extract(self, doc: Document, patient_id: str) -> ExtractionResult:
        summary_text = l2b_stage1.summarize(doc.text, model=self._model)
        agent_id = agent("two_stage", self._model or STAGE1_MODEL)
        ext = deterministic_extract(
            summary_text, doc, patient_id,
            extracted_by=agent_id, prompt_id=l2b_stage1.prompt_id(),
        )
        # Stage 2 тут детермінований, але читає він LLM-summary: чого модель не
        # внесла у стиснення, того регекс уже не поверне. Отже — «не перевірено».
        ext.mean_confidence = UNVERIFIED_CONFIDENCE if ext.observations else 0.0
        ext.document_reference = _build_docref(
            doc, summary_text, patient_id,
            extracted_by=agent_id, prompt_id=l2b_stage1.prompt_id(),
        )
        return ext


class LoraStage2Extractor:
    """Двоетапний L2b з FT LoRA-Stage 2 (двосерверний llama.cpp-сетап).

    Stage 1 — база (SGR-summary → `DocumentReference`); Stage 2 — база+LoRA
    генерує KVT4, які парсяться у FHIR Observations (часті лаби мостяться у LOINC).
    Потребує двох llama.cpp-серверів (`STAGE1_URL`, `STAGE2_URL`).
    """

    def __init__(
        self,
        *,
        stage1_model: str | None = STAGE1_MODEL,
        stage1_url: str | None = STAGE1_URL,
        stage2_model: str | None = STAGE2_MODEL,
        stage2_url: str | None = STAGE2_URL,
    ) -> None:
        self._s1_model = stage1_model
        self._s1_url = stage1_url
        self._s2_model = stage2_model
        self._s2_url = stage2_url

    def extract(self, doc: Document, patient_id: str) -> ExtractionResult:
        summary_text = l2b_stage1.summarize(
            doc.text, model=self._s1_model, base_url=self._s1_url
        )
        kvt = l2b_stage2_lora.extract_facts(
            summary_text, url=self._s2_url, model=self._s2_model
        )
        # Два промпти в ланцюгу — обидва в провенансі: Stage 1 вирішує, що
        # взагалі доживе до Stage 2, тож ховати його було б неповно.
        prompts = f"{l2b_stage1.prompt_id()}+{l2b_stage2_lora.prompt_id()}"
        observations = l2b_stage2_lora.to_observations(
            kvt, doc, patient_id,
            extracted_by=agent("lora_stage2", self._s2_model),
            prompt_id=prompts,
        )
        return ExtractionResult(
            observations=observations,
            mean_confidence=UNVERIFIED_CONFIDENCE if observations else 0.0,
            document_reference=_build_docref(
                doc, summary_text, patient_id,
                extracted_by=agent("stage1", self._s1_model),
                prompt_id=l2b_stage1.prompt_id(),
            ),
        )


class NuExtractExtractor:
    """L2b через NuExtract3 (VLM). Stage 2 (текст/markdown → KVT-факти за шаблоном)
    → Observations (міст keyword→LOINC). `doc.text` (markdown або звичайний текст)
    зберігається як `DocumentReference` — розширена іпостась.

    Для зображень Stage 1 (image→markdown) робить `/ingest/image` через
    `l2b_nuextract.image_to_markdown`, тож сюди вже приходить markdown у `doc.text`.
    """

    def __init__(self, *, url: str | None = NUEXTRACT_URL, model: str | None = NUEXTRACT_MODEL) -> None:
        self._url = url
        self._model = model

    def extract(self, doc: Document, patient_id: str) -> ExtractionResult:
        kvt = l2b_nuextract.extract_facts(doc.text, url=self._url, model=self._model)
        agent_id = agent("nuextract", self._model)
        observations = l2b_stage2_lora.to_observations(
            kvt, doc, patient_id,
            extracted_by=agent_id, prompt_id=l2b_nuextract.prompt_id(),
        )
        return ExtractionResult(
            observations=observations,
            mean_confidence=UNVERIFIED_CONFIDENCE if observations else 0.0,
            document_reference=_build_docref(
                doc, doc.text, patient_id,
                extracted_by=agent_id, prompt_id=l2b_nuextract.prompt_id(),
            ),
        )


class OcrLabExtractor:
    """L2b через хмарний OCR Lab (ZeroGPU NuExtract3) — opt-in cloud-варіант.

    Дзеркалить `NuExtractExtractor`, але текст→факти йде через `gradio_client` до
    окремого Space (Spec.md FR-MODEL-004: різниця лише в транспорті). Для зображень
    Stage 1 (image→markdown) робить `/ingest/image` через `l2b_ocrlab.image_to_markdown`,
    тож сюди приходить markdown у `doc.text`. Observations будує той самий
    `to_observations` — межа детермінізму збережена.
    """

    def __init__(
        self, *, space: str | None = OCRLAB_SPACE, image_base64: str | None = None
    ) -> None:
        self._space = space
        # Зображення-джерело (коли документ прийшов із фото): дає запасний шлях,
        # якщо з markdown-транскрипції не вийшло витягти жодного факту.
        self._image_base64 = image_base64

    def extract(self, doc: Document, patient_id: str) -> ExtractionResult:
        kvt = l2b_ocrlab.extract_facts(doc.text, space=self._space)
        if not kvt and self._image_base64:
            # Транскрипція буває неповною (модель може обірвати широку таблицю на
            # пів-документі) — тоді фактів у тексті просто немає. Беремо їх прямо
            # із зображення одним проходом; free-text іпостась лишається тією ж.
            kvt = l2b_ocrlab.facts_from_image(self._image_base64, space=self._space)
        agent_id = agent("nuextract_zerogpu", self._space)
        observations = l2b_stage2_lora.to_observations(
            kvt, doc, patient_id, extracted_by=agent_id,
        )
        return ExtractionResult(
            observations=observations,
            mean_confidence=UNVERIFIED_CONFIDENCE if observations else 0.0,
            document_reference=_build_docref(
                doc, doc.text, patient_id, extracted_by=agent_id,
            ),
        )


def extract_documents(
    docs: list[Document], patient_id: str, extractor: Extractor | None = None
) -> ExtractionResult:
    """Об'єднує результати L2b-екстракції з кількох документів."""
    extractor = extractor if extractor is not None else StubExtractor()

    observations: list[Observation] = []
    conditions: list[Condition] = []
    medications: list[MedicationRequest] = []
    confidences: list[float] = []

    for doc in docs:
        result = extractor.extract(doc, patient_id)
        observations.extend(result.observations)
        conditions.extend(result.conditions)
        medications.extend(result.medications)
        confidences.append(result.mean_confidence)

    mean_confidence = sum(confidences) / len(confidences) if confidences else 1.0
    return ExtractionResult(
        observations=observations,
        conditions=conditions,
        medications=medications,
        mean_confidence=mean_confidence,
    )
