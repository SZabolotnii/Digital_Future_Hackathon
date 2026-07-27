"""Оркестрація конвеєра TRACE: документ → витяг → сховища → сигнали → рішення.

Головний інваріант: сигнали безпеки (L1/L2a) рахуються детерміновано на факт-сторі,
а L2b (екстрактор) лише витягує факти й не робить висновків про безпеку.

`LiveMedCard` — джерело істини для FHIR-ресурсів пацієнта; `FactStore` і
`KnowledgeGraph` — похідні проєкції над ними. Це дає знімок стану (snapshot),
експорт у FHIR Bundle і аудит рішень.
"""
from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Optional

from . import economics
from .dto import (
    AuditEntry,
    BoundaryEvent,
    Document,
    ExtractionResult,
    ReviewEntry,
    Signal,
    TraceReport,
    severity_rank,
)
from .fhir import (
    Condition,
    DocumentReference,
    MedicationRequest,
    Observation,
    Patient,
)
from .layers import l1_safety, l2a_temporal, l2a_trends, l3_router
from .layers.l2b_extractor import Extractor, StubExtractor
from .reference import ruleset_version
from .stores import FactStore, KnowledgeGraph
from .temporal import TemporalGraph, build as build_temporal


def _assert_provenance(ext: ExtractionResult) -> None:
    """Інваріант: кожен витягнутий факт має провенанс із ``source_doc_id``.

    Без цього триплет губить зв'язок із документом — рветься провенанс-ребро графа
    і майбутній retrieval. Перевіряємо на межі екстракції (не при відновленні зі
    знімка, де ресурси вже валідовані й можуть бути легасі).
    """
    missing = [
        r.id
        for r in (*ext.observations, *ext.conditions, *ext.medications)
        if r.provenance is None or not r.provenance.source_doc_id
    ]
    if missing:
        raise ValueError(
            "Екстракція без провенансу (source_doc_id) для ресурсів: "
            f"{', '.join(missing)}. Провенанс обов'язковий — кожен факт має "
            "посилатися на документ-джерело."
        )


def _drive(signals: list[Signal], confidence: float, hazard: str) -> str:
    """Який з двох приводів політики L3 спрацював (вхід для SNR і EP).

    Рішення роутера бінарне, а причин у нього дві незалежні. Для нагляду різниця
    істотна: ескалація через знайдену взаємодію і ескалація через нерозбірливе
    фото — це різні черги з різною цінністю часу людини.
    """
    risk = any(severity_rank(s.severity) >= severity_rank("major") for s in signals)
    low = confidence < economics.escalation_threshold(hazard)
    if risk and low:
        return "both"
    if risk:
        return "risk"
    return "confidence" if low else "none"


class LiveMedCard:
    """Стан однієї медичної картки пацієнта + прогін документів крізь TRACE."""

    def __init__(
        self,
        patient: Patient,
        extractor: Optional[Extractor] = None,
        fallback_extractor: Optional[Extractor] = None,
    ) -> None:
        self.patient = patient
        self.extractor: Extractor = extractor or StubExtractor()
        # Альтернативний L2 для повторної спроби при невпевненому витязі. None →
        # re-invocation вимкнено: без другого шляху політиці нема чим спробувати
        # ще раз, і вдавати наявність запасного варіанта не варто.
        self.fallback_extractor: Optional[Extractor] = fallback_extractor
        self.factstore = FactStore()
        self.graph = KnowledgeGraph()
        self.graph.add_patient(patient)
        # Джерело істини — утримувані FHIR-ресурси (дедуплікація за id).
        self._observations: list[Observation] = []
        self._conditions: list[Condition] = []
        self._medications: list[MedicationRequest] = []
        self._doc_refs: list[DocumentReference] = []
        self._obs_ids: set[str] = set()
        self._cond_ids: set[str] = set()
        self._med_ids: set[str] = set()
        self._docref_ids: set[str] = set()
        self._document_ids: set[str] = set()
        # Дати подання документів — valid-time якір для ресурсів без власної дати
        # (насамперед `MedicationRequest`, у якого поля дати немає взагалі).
        self._document_dates: dict[str, datetime] = {}
        # Оригінальний текст документа — окремо від того, що з нього зробила
        # модель. Без цього після перезавантаження картки «джерелом» лишався б
        # лише Stage-1 summary, і звірити факт із поданим було б нічим.
        self._document_texts: dict[str, str] = {}
        self._audit: list[AuditEntry] = []
        # Дії людини (L4) і перетини межі автономії — окремі журнали від аудиту
        # конвеєра: різні події, різні схеми, різні читачі.
        self._reviews: list[ReviewEntry] = []
        self._boundary: list[BoundaryEvent] = []
        # Ключі major+ сигналів, які картка вже колись показувала. Потрібні, щоб
        # `high_risk` в аудиті означав «цей документ вніс НОВУ небезпеку», а не
        # «в картці вона є» — інакше кожен наступний документ платив би за ту
        # саму взаємодію ще раз.
        self._seen_major_keys: set[str] = set()

    # ── L2b → сховища ───────────────────────────────────────────────────────
    def _absorb(self, ext: ExtractionResult) -> None:
        for o in ext.observations:
            self.factstore.add_observation(o)
            self.graph.add_observation(o, self.patient.id)
            if o.id not in self._obs_ids:
                self._observations.append(o)
                self._obs_ids.add(o.id)
        for c in ext.conditions:
            self.graph.add_condition(c, self.patient.id)
            if c.id not in self._cond_ids:
                self._conditions.append(c)
                self._cond_ids.add(c.id)
        for m in ext.medications:
            self.graph.add_medication(m, self.patient.id)
            if m.id not in self._med_ids:
                self._medications.append(m)
                self._med_ids.add(m.id)
        if ext.document_reference is not None:
            self._absorb_docref(ext.document_reference)

    def _absorb_docref(self, dref: DocumentReference) -> None:
        """Зберегти free-text іпостась + додати текст у вузол-документ графа.

        Чи це оригінал, чи текст, написаний моделлю, каже `prompt_id`: він є лише
        там, де в ланцюгу була генеративна модель. Отже підпис під текстом не
        здогадка інтерфейсу, а факт із провенансу.
        """
        if dref.provenance is not None:
            self.graph.add_document(
                dref.provenance.source_doc_id, dref.content.data or "",
                generated=dref.provenance.prompt_id is not None,
            )
        if dref.id not in self._docref_ids:
            self._doc_refs.append(dref)
            self._docref_ids.add(dref.id)

    # ── темпоральна проєкція ──────────────────────────────────────────────────
    def temporal(self) -> TemporalGraph:
        """Темпоральна проєкція поточного стану (сутності в часі, епізоди).

        Рахується на вимогу, а не тримається інкрементально: картка невелика, а
        похідний стан, який можна забути оновити, — джерело тихих розбіжностей
        між проєкціями.
        """
        return build_temporal(
            self._observations,
            self._conditions,
            self._medications,
            document_dates=self._document_dates,
        )

    # ── L1 + L2a на поточному стані сховищ ────────────────────────────────────
    def current_signals(self, lang: str = "uk") -> list[Signal]:
        """Детерміновані сигнали над усім накопиченим станом (тексти — UA/EN)."""
        signals = (
            l1_safety.run(self._medications, lang)
            + l2a_trends.run(self.factstore, self.patient.gender, lang)
            + l2a_temporal.run(
                self.factstore, self.temporal(), self.patient.gender, lang
            )
        )
        # Версію правил ставимо тут, а не в кожному з восьми місць конструювання:
        # L1 і L2a читають з тих самих файлів, тож версія спільна, а одне місце
        # неможливо забути оновити при появі нового детектора.
        version = ruleset_version()
        return [s.model_copy(update={"ruleset_version": version}) for s in signals]

    def ingest(self, doc: Document, *, extractor: "Extractor | None" = None) -> TraceReport:
        """Прогнати один документ і повернути повний TRACE-звіт поточного стану.

        `extractor` — разовий override L2b для цього документа (не змінює дефолт
        картки). Використовується для завантаження фото через хмарний OCR Lab,
        коли текстовий шлях лишається детермінованим `stub`.

        Якщо витяг вийшов невпевненим і зареєстровано `fallback_extractor`, L3
        дає другу спробу (`should_reinvoke`) перш ніж кликати людину: час людини
        дорожчий за повторний прогін, і кожна ескалація, знята автоматично,
        лишає увагу для тих, де вона справді потрібна.
        """
        # Дату реєструємо ДО автопростановки: якщо документ прийшов без неї, то
        # `received_at` нижче — це момент обробки, а не дата документа. Класти
        # такий штамп у якорі таймлайну означало б вигадати клінічну дату.
        if doc.received_at is not None:
            self._document_dates[doc.id] = doc.received_at
        # Без мітки часу обзервації не потрапляють у факт-стор і зникають з
        # таймлайну (і з перевірок безпеки). Проставляємо час прийому документа.
        if doc.received_at is None:
            doc = doc.model_copy(update={"received_at": datetime.now(timezone.utc)})
        self._document_ids.add(doc.id)
        # Оригінал зберігаємо завжди й окремо від того, що напише модель.
        # Двоетапний L2b кладе у вузол-документ Stage-1 summary, і без цього
        # рядка інтерфейс показував би текст моделі під заголовком «Джерело».
        self._document_texts[doc.id] = doc.text
        active = extractor or self.extractor
        started = perf_counter()
        ext = active.extract(doc, self.patient.id)
        used = type(active).__name__
        attempts = 1

        # ── L3: друга спроба перед тим, як кликати людину ──────────────────────
        # Витяг ще не поглинуто: невпевнений результат не має потрапити у факт-стор
        # раніше, ніж з'ясується, що альтернатива не дала кращого. Інакше картка
        # накопичувала б обидві версії тих самих фактів.
        hazard = economics.hazard_of(
            has_medications=bool(ext.medications),
            has_observations=bool(ext.observations),
        )
        if self.fallback_extractor is not None and l3_router.should_reinvoke(
            ext.mean_confidence, hazard, attempts
        ):
            attempts += 1
            retry = self.fallback_extractor.extract(doc, self.patient.id)
            # Беремо кращий результат, а не останній: альтернатива може виявитись
            # гіршою, і тоді повторна спроба має коштувати лише часу.
            if retry.mean_confidence > ext.mean_confidence:
                ext = retry
                used = type(self.fallback_extractor).__name__
                hazard = economics.hazard_of(
                    has_medications=bool(ext.medications),
                    has_observations=bool(ext.observations),
                )

        latency_ms = (perf_counter() - started) * 1000
        _assert_provenance(ext)
        self._absorb(ext)
        self.graph.add_document(doc.id, doc.text)
        signals = self.current_signals()
        decision = l3_router.route(signals, ext.mean_confidence, doc.kind, hazard=hazard)
        self._record_audit(
            doc, ext, signals, decision, hazard,
            high_risk=self._absorb_major_keys(signals),
            escalation_drive=_drive(signals, ext.mean_confidence, hazard),
            extractor=used,
            latency_ms=round(latency_ms, 1),
            attempts=attempts,
        )
        return TraceReport(
            document_id=doc.id, extraction=ext, signals=signals, decision=decision
        )

    def _absorb_major_keys(self, signals: list[Signal]) -> bool:
        """Чи з'явилася в картці НОВА небезпека major+; запам'ятати побачене."""
        major = {
            s.key for s in signals
            if severity_rank(s.severity) >= severity_rank("major")
        }
        introduced = major - self._seen_major_keys
        self._seen_major_keys |= major
        return bool(introduced)

    def ingest_all(self, docs: list[Document]) -> list[TraceReport]:
        return [self.ingest(d) for d in docs]

    # ── аудит рішень L3 ───────────────────────────────────────────────────────
    def _record_audit(
        self, doc, ext, signals, decision, hazard: str = "medication",
        high_risk: bool = False, escalation_drive: str = "none",
        extractor: str = "", latency_ms: float = 0.0, attempts: int = 1,
    ) -> None:
        self._audit.append(AuditEntry(
            ts=datetime.now(timezone.utc),
            document_id=doc.id,
            kind=doc.kind,
            extraction_confidence=ext.mean_confidence,
            n_signals=len(signals),
            route_to=decision.route_to,
            escalate=decision.escalate,
            hazard=hazard,
            high_risk=high_risk,
            escalation_drive=escalation_drive,
            extractor=extractor,
            latency_ms=latency_ms,
            attempts=attempts,
        ))

    def audit_log(self) -> list[dict]:
        """Журнал рішень L3 як JSON-сумісні dict (для API/метрик)."""
        return [a.model_dump(mode="json") for a in self._audit]

    # ── L4: нагляд людини ─────────────────────────────────────────────────────
    def review(self, entry: ReviewEntry) -> ReviewEntry:
        """Записати дію людини над сигналом або рішенням щодо документа."""
        self._reviews.append(entry)
        return entry

    def review_log(self) -> list[dict]:
        """Журнал дій людини як JSON-сумісні dict."""
        return [r.model_dump(mode="json") for r in self._reviews]

    def reviewed_signal_keys(self) -> set[str]:
        """Ключі сигналів, щодо яких людина вже висловилась."""
        return {r.signal_key for r in self._reviews if r.signal_key is not None}

    def last_review_by_signal(self) -> dict[str, ReviewEntry]:
        """Остання дія людини по кожному сигналу (пізніша заміняє попередню)."""
        out: dict[str, ReviewEntry] = {}
        for r in self._reviews:
            if r.signal_key is not None:
                out[r.signal_key] = r
        return out

    def outstanding_escalations(self) -> list[AuditEntry]:
        """Ескалації, які досі нікому не адресовані.

        Ескалація за низькою впевненістю витягу стосується документа, а не
        сигналу: не буває «сигналу про те, що текст погано розпізнано». Тож
        закриває її перегляд того самого документа.
        """
        seen = {r.document_id for r in self._reviews if r.document_id is not None}
        return [a for a in self._audit if a.escalate and a.document_id not in seen]

    def boundary_log(self) -> list[dict]:
        """Журнал перетинів межі автономії (вхід для ABC)."""
        return [b.model_dump(mode="json") for b in self._boundary]

    def record_boundary_event(self, event: BoundaryEvent) -> BoundaryEvent:
        self._boundary.append(event)
        return event

    # ── знімок / відновлення (персистентність, FHIR Bundle) ───────────────────
    def snapshot(self) -> dict:
        """Серіалізований стан картки (FHIR-ресурси + метадані)."""
        return {
            "patient": self.patient.model_dump(mode="json", by_alias=True),
            "observations": [o.model_dump(mode="json") for o in self._observations],
            "conditions": [c.model_dump(mode="json") for c in self._conditions],
            "medications": [m.model_dump(mode="json") for m in self._medications],
            "document_references": [
                d.model_dump(mode="json") for d in self._doc_refs
            ],
            "documents": sorted(self._document_ids),
            # Дати подання окремо від списку id: без них відновлена картка
            # втрачає valid-time якір призначень, і темпоральна проєкція
            # «злипається» в один епізод на момент відновлення.
            "document_dates": {
                k: v.isoformat() for k, v in sorted(self._document_dates.items())
            },
            "document_texts": dict(sorted(self._document_texts.items())),
            "audit": [a.model_dump(mode="json") for a in self._audit],
            # Нагляд людини переживає перезапуск разом з рішеннями, які він
            # закриває: журнал ескалацій без журналу відповідей на них показував
            # би картку вічно непереглянутою.
            "reviews": [r.model_dump(mode="json") for r in self._reviews],
            "boundary_events": [b.model_dump(mode="json") for b in self._boundary],
        }

    @classmethod
    def from_snapshot(
        cls, data: dict, extractor: Optional[Extractor] = None
    ) -> "LiveMedCard":
        """Відновити картку зі знімка (`snapshot`)."""
        card = cls(Patient(**data["patient"]), extractor)
        card._absorb(ExtractionResult(
            observations=[Observation(**o) for o in data.get("observations", [])],
            conditions=[Condition(**c) for c in data.get("conditions", [])],
            medications=[MedicationRequest(**m) for m in data.get("medications", [])],
        ))
        for d in data.get("document_references", []):
            card._absorb_docref(DocumentReference(**d))
        card._document_ids.update(data.get("documents", []))
        # Оригінали повертаються у вузли графа окремим полем — щоб і після
        # відновлення «Джерело» показувало подане, а не переказ моделі.
        for doc_id, text in (data.get("document_texts") or {}).items():
            card._document_texts[doc_id] = text
            card.graph.add_document(doc_id, text)
        # Старі знімки поля не мають — тоді якорем лишається provenance-час.
        for doc_id, iso in (data.get("document_dates") or {}).items():
            card._document_dates[doc_id] = datetime.fromisoformat(iso)
        # Валідація журналів при відновленні: биті записи → явна помилка, не сміття.
        card._audit.extend(AuditEntry(**a) for a in data.get("audit", []))
        card._reviews.extend(ReviewEntry(**r) for r in data.get("reviews", []))
        card._boundary.extend(
            BoundaryEvent(**b) for b in data.get("boundary_events", [])
        )
        # Побачені небезпеки відновлюємо з відновленого стану, а не зі знімка:
        # так множина завжди узгоджена з тим, що картка показує зараз, навіть
        # якщо знімок зроблено іншою версією правил.
        card._absorb_major_keys(card.current_signals())
        return card

    # ── зручні акселератори для API/Q&A ───────────────────────────────────────
    @property
    def observations(self) -> list[Observation]:
        return list(self._observations)

    @property
    def conditions(self) -> list[Condition]:
        return list(self._conditions)

    @property
    def medications(self) -> list[MedicationRequest]:
        return list(self._medications)

    @property
    def document_references(self) -> list[DocumentReference]:
        return list(self._doc_refs)

    @property
    def documents(self) -> set[str]:
        return set(self._document_ids)

    @property
    def document_count(self) -> int:
        return len(self._document_ids)
