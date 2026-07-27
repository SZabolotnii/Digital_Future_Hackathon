"""Темпоральний шар: картка як граф сутностей у часі, а не список фактів.

Третя проєкція над тими самими FHIR-ресурсами (поряд із `FactStore` і
`KnowledgeGraph`). Відповідає на питання, які плоский граф структурно не може:
коли показник з'явився вперше, що змінилося між епізодами, які препарати були
активні одночасно.

**Дві осі часу.** Розрізняємо `valid` (коли факт був істинним для пацієнта:
`effectiveDateTime`, `onsetDateTime`) і `record` (коли ми про це дізналися:
`Provenance.extracted_at`). Прапорець `date_stated` каже, чи перша вісь справді
взята з документа, чи підставлена з другої. Датувати торішній аналіз днем
завантаження — тиха помилка, яка ламає і тренд, і довіру; тут вона видима.

**Межа детермінізму.** Цей модуль НЕ породжує сигналів безпеки і навмисно не
імпортує `Signal` (закріплено архітектурним тестом). Він описовий: навігація,
провенанс, метрики, темпоральні запити для Q&A. Детерміновані сигнали над
темпоральною проєкцією живуть у `layers/l2a_temporal.py` і рахуються з фактів,
а не обходом графа.

Обґрунтування рішень — `docs/temporal-graph.md`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal, Optional

from pydantic import BaseModel, Field

from .config import EPISODE_GAP_DAYS
from .fhir import (
    Condition,
    MedicationRequest,
    Observation,
    loinc_of,
    rxnorm_of,
)

__all__ = [
    "Interval",
    "TemporalEntity",
    "TemporalEdge",
    "Episode",
    "TemporalMetrics",
    "TemporalGraph",
    "build",
    "entity_key",
]

EntityKind = Literal["analyte", "drug", "problem"]
TimeAxis = Literal["valid", "record"]

# Порядок для стабільного сортування сутностей одного моменту часу.
_KIND_ORDER: dict[str, int] = {"problem": 0, "drug": 1, "analyte": 2}


def _aware(ts: datetime) -> datetime:
    """Нормалізація до tz-aware UTC.

    Джерела дають різне: демо-семпли — naive, екстрактори — aware. Без цього
    сортування таймлайну падає на порівнянні при їх змішуванні.
    """
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def entity_key(kind: EntityKind, code: Optional[str], label: str) -> str:
    """Стабільний ключ сутності: код, якщо він є, інакше — нормалізована назва.

    Факт без стандартного коду не викидаємо: він потрапляє в граф під назвою.
    Витягнутий і невидимий факт виглядає точно так само, як не витягнутий, а це
    різні речі — але й ототожнювати «Креатинін» з `2160-0` без коду не можна.
    """
    return f"{kind}:{code}" if code else f"{kind}:~{label.strip().lower()}"


class Interval(BaseModel):
    """Інтервал валідності факту, **напіввідкритий**: ``[start, end)``.

    ``end=None`` — відкритий (триває). Напіввідкритість тут не педантизм, а
    єдиний спосіб коректно виразити «препарат уже не приймається». Про відмінений
    препарат ми знаємо лише те, що на дату документа він активним НЕ був; при
    включеному кінці той самий момент читався б як «ще приймає», і відмінений
    варфарин потрапляв би в підрахунок одночасних препаратів.
    """

    model_config = {"frozen": True}

    start: datetime
    end: Optional[datetime] = None

    @property
    def is_empty(self) -> bool:
        """Порожній інтервал — «активним не був» (напр. відмінене призначення)."""
        return self.end is not None and _aware(self.end) <= _aware(self.start)

    def contains(self, ts: datetime) -> bool:
        ts = _aware(ts)
        if self.is_empty or ts < _aware(self.start):
            return False
        return self.end is None or ts < _aware(self.end)

    def overlaps(self, other: "Interval") -> bool:
        """Чи перетинаються інтервали (відкритий кінець = до нескінченності)."""
        if self.is_empty or other.is_empty:
            return False
        a_start, b_start = _aware(self.start), _aware(other.start)
        a_end = _aware(self.end) if self.end else None
        b_end = _aware(other.end) if other.end else None
        if a_end is not None and a_end <= b_start:
            return False
        if b_end is not None and b_end <= a_start:
            return False
        return True

    @property
    def duration_days(self) -> Optional[float]:
        """Тривалість у днях; None для відкритого інтервалу — довжина невідома."""
        if self.end is None:
            return None
        return (_aware(self.end) - _aware(self.start)).total_seconds() / 86400


class TemporalEntity(BaseModel):
    """Клінічна сутність, агрегована по всіх своїх входженнях у картці.

    Рівень над `Observation/{id}`: чотири виміри креатиніну — це одна сутність
    `analyte:2160-0` з чотирма входженнями, а не чотири незв'язані вузли з
    однаковим підписом.
    """

    model_config = {"frozen": True}

    key: str
    kind: EntityKind
    label: str
    code: Optional[str] = None
    # valid time: коли факт був істинним для пацієнта
    first_valid: datetime
    last_valid: datetime
    # record time: коли картка про це дізналася (перше входження)
    first_recorded: datetime
    occurrences: int = 1
    # Чи ВСІ входження мали дату, заявлену в тексті документа. False → таймлайн
    # цієї сутності частково спирається на дату документа/завантаження.
    dated: bool = True
    # Скільки входжень НІ ДО ЧОГО не прив'язані в часі: дати в тексті немає, і
    # valid збігається з record. Це не те саме, що `not dated`: лабораторний
    # бланк без дати в тексті, але поданий із власною датою документа, датовано
    # коректно. Фікція — лише коли обидві осі показують момент обробки.
    unanchored: int = 0
    doc_ids: tuple[str, ...] = ()
    interval: Optional[Interval] = None  # для ліків — період активності

    @property
    def span_days(self) -> float:
        return (_aware(self.last_valid) - _aware(self.first_valid)).total_seconds() / 86400


class TemporalEdge(BaseModel):
    """Ребро з інтервалом валідності.

    Лише два типи, обидва зчитані, а не виведені: `treats` (з
    `MedicationRequest.reason`) і `co_active` (перетин інтервалів активності
    двох препаратів). Ребра «X передувало Y» свідомо немає — див.
    `docs/temporal-graph.md`.
    """

    model_config = {"frozen": True}

    source: str
    target: str
    rel: Literal["treats", "co_active"]
    interval: Interval
    basis: str = ""  # чому ребро існує — для пояснення в інтерфейсі


class Episode(BaseModel):
    """Сегмент історії — аналог «розділу» в наративному графі.

    Межа епізоду — розрив у часі більший за `EPISODE_GAP_DAYS`. Сегментація
    евристична (як і регекси розділів в оригінальному підході); поріг
    конфігурований, і різні пороги дають різну картину.
    """

    model_config = {"frozen": True}

    index: int
    start: datetime
    end: datetime
    doc_ids: tuple[str, ...] = ()
    entity_keys: tuple[str, ...] = ()
    new_entity_keys: tuple[str, ...] = ()  # сутності, що вперше з'явились тут
    n_facts: int = 0

    @property
    def span_days(self) -> float:
        return (_aware(self.end) - _aware(self.start)).total_seconds() / 86400


class TemporalMetrics(BaseModel):
    """Метрики топології історії. Описові — жодна не є сигналом безпеки."""

    span_days: float = 0.0
    n_entities: int = 0
    n_analytes: int = 0
    n_drugs: int = 0
    n_problems: int = 0
    n_episodes: int = 0
    # Мультиморбідність — кількість станів, не позначених як вирішені.
    multimorbidity: int = 0
    # Найбільша кількість препаратів, активних одночасно (підстава поліфармації).
    max_concurrent_medications: int = 0
    active_medications: int = 0
    # Частка фактів, чия дата справді стоїть у ТЕКСТІ документа.
    dated_fraction: float = 1.0
    # Частка фактів, прив'язаних до якоїсь реальної дати — з тексту або з дати
    # самого документа. Це і є міра довіри таймлайну: 0.4 означає, що більшість
    # точок стоять там, де їх поставила обробка, а не клініка.
    anchored_fraction: float = 1.0
    # Скільки НОВИХ сутностей приносить кожен епізод — аналог швидкості появи
    # персонажів у наративному графі.
    novelty_by_episode: list[int] = Field(default_factory=list)


class TemporalGraph(BaseModel):
    """Темпоральна проєкція картки: сутності, ребра з інтервалами, епізоди."""

    entities: dict[str, TemporalEntity] = Field(default_factory=dict)
    edges: list[TemporalEdge] = Field(default_factory=list)
    episodes: list[Episode] = Field(default_factory=list)
    horizon: Optional[datetime] = None  # найсвіжіше свідчення в картці

    # ── темпоральні запити ────────────────────────────────────────────────────
    def first_appearance(self, key: str, axis: TimeAxis = "valid") -> Optional[datetime]:
        """Коли сутність з'явилася вперше — клінічно або в записі."""
        e = self.entities.get(key)
        if e is None:
            return None
        return e.first_valid if axis == "valid" else e.first_recorded

    def as_of(self, ts: datetime, axis: TimeAxis = "valid") -> list[TemporalEntity]:
        """Що було відомо/істинно станом на момент `ts`.

        `axis="valid"` — що вже сталося з пацієнтом; `axis="record"` — що картка
        вже встигла дізнатись. Різниця важлива, коли документи подають заднім
        числом: клінічно факт існував торік, а в картці — з учора.
        """
        ts = _aware(ts)
        field = "first_valid" if axis == "valid" else "first_recorded"
        return _sorted(e for e in self.entities.values() if _aware(getattr(e, field)) <= ts)

    def since(self, ts: datetime, axis: TimeAxis = "valid") -> list[TemporalEntity]:
        """Сутності, що з'явилися ПІСЛЯ моменту `ts` — «що нового з тоді»."""
        ts = _aware(ts)
        field = "first_valid" if axis == "valid" else "first_recorded"
        return _sorted(e for e in self.entities.values() if _aware(getattr(e, field)) > ts)

    def of_kind(self, kind: EntityKind) -> list[TemporalEntity]:
        return _sorted(e for e in self.entities.values() if e.kind == kind)

    def episode_at(self, ts: datetime) -> Optional[Episode]:
        ts = _aware(ts)
        for ep in self.episodes:
            if _aware(ep.start) <= ts <= _aware(ep.end):
                return ep
        return None

    def concurrent_medications(self, ts: Optional[datetime] = None) -> list[TemporalEntity]:
        """Препарати, активні на момент `ts` (типово — горизонт картки)."""
        point = _aware(ts) if ts is not None else self.horizon
        if point is None:
            return []
        return _sorted(
            e
            for e in self.entities.values()
            if e.kind == "drug" and e.interval is not None and e.interval.contains(point)
        )

    def co_active_pairs(self) -> list[TemporalEdge]:
        return [e for e in self.edges if e.rel == "co_active"]

    # ── метрики ───────────────────────────────────────────────────────────────
    def metrics(self) -> TemporalMetrics:
        ents = list(self.entities.values())
        if not ents:
            return TemporalMetrics()
        starts = [_aware(e.first_valid) for e in ents]
        ends = [_aware(e.last_valid) for e in ents]
        occurrences = sum(e.occurrences for e in ents)
        dated_occ = sum(e.occurrences for e in ents if e.dated)
        unanchored_occ = sum(e.unanchored for e in ents)
        drugs = [e for e in ents if e.kind == "drug"]
        return TemporalMetrics(
            span_days=(max(ends) - min(starts)).total_seconds() / 86400,
            n_entities=len(ents),
            n_analytes=sum(1 for e in ents if e.kind == "analyte"),
            n_drugs=len(drugs),
            n_problems=sum(1 for e in ents if e.kind == "problem"),
            n_episodes=len(self.episodes),
            multimorbidity=sum(1 for e in ents if e.kind == "problem"),
            max_concurrent_medications=_max_overlap(
                [d.interval for d in drugs if d.interval is not None]
            ),
            active_medications=len(self.concurrent_medications()),
            dated_fraction=(dated_occ / occurrences) if occurrences else 1.0,
            anchored_fraction=(
                (occurrences - unanchored_occ) / occurrences if occurrences else 1.0
            ),
            novelty_by_episode=[len(ep.new_entity_keys) for ep in self.episodes],
        )

    # ── серіалізація для API/UI ───────────────────────────────────────────────
    def to_payload(self) -> dict:
        """JSON-сумісне подання для інтерфейсу (нічого не рахує понад метрики)."""
        return {
            "entities": [e.model_dump(mode="json") for e in _sorted(self.entities.values())],
            "edges": [e.model_dump(mode="json") for e in self.edges],
            "episodes": [ep.model_dump(mode="json") for ep in self.episodes],
            "metrics": self.metrics().model_dump(mode="json"),
            "horizon": self.horizon.isoformat() if self.horizon else None,
        }


def _sorted(entities: Iterable[TemporalEntity]) -> list[TemporalEntity]:
    """Стабільний порядок: за першою появою, потім тип, потім ключ."""
    return sorted(
        entities,
        key=lambda e: (_aware(e.first_valid), _KIND_ORDER.get(e.kind, 9), e.key),
    )


def _max_overlap(intervals: list[Interval]) -> int:
    """Максимальна кількість інтервалів, що перетинаються в одній точці.

    Класичний sweep-line по подіях початку/кінця. Інтервали напіввідкриті, тож
    закриття обробляється ПЕРЕД відкриттям у ту саму мітку часу: препарат,
    відмінений тим самим документом, яким призначено інший, разом із ним не
    приймався. Порожні інтервали (відмінені призначення) не рахуються взагалі.
    """
    events: list[tuple[datetime, int]] = []
    for iv in intervals:
        if iv.is_empty:
            continue
        events.append((_aware(iv.start), 1))
        if iv.end is not None:
            events.append((_aware(iv.end), -1))
    if not events:
        return 0
    events.sort(key=lambda ev: (ev[0], ev[1]))
    best = current = 0
    for _, delta in events:
        current += delta
        best = max(best, current)
    return best


# ── побудова ──────────────────────────────────────────────────────────────────
class _Acc:
    """Акумулятор входжень однієї сутності під час обходу ресурсів."""

    __slots__ = (
        "kind", "label", "code", "valid", "recorded",
        "n", "dated", "unanchored", "docs", "interval",
    )

    def __init__(self, kind: EntityKind, label: str, code: Optional[str]) -> None:
        self.kind = kind
        self.label = label
        self.code = code
        self.valid: list[datetime] = []
        self.recorded: list[datetime] = []
        self.n = 0
        self.dated = True
        self.unanchored = 0
        self.docs: dict[str, None] = {}
        self.interval: Optional[Interval] = None

    def add(
        self,
        valid: datetime,
        recorded: datetime,
        doc_id: Optional[str],
        dated: bool,
        anchored: bool,
    ) -> None:
        valid, recorded = _aware(valid), _aware(recorded)
        self.valid.append(valid)
        self.recorded.append(recorded)
        self.n += 1
        self.dated = self.dated and dated
        if not anchored:
            self.unanchored += 1
        if doc_id:
            self.docs.setdefault(doc_id, None)

    def build(self, key: str) -> TemporalEntity:
        return TemporalEntity(
            key=key,
            kind=self.kind,
            label=self.label,
            code=self.code,
            first_valid=min(self.valid),
            last_valid=max(self.valid),
            first_recorded=min(self.recorded),
            occurrences=self.n,
            dated=self.dated,
            unanchored=self.unanchored,
            doc_ids=tuple(self.docs),
            interval=self.interval,
        )


def _valid_and_record(resource, fallback: datetime) -> tuple[datetime, datetime, bool]:
    """Витягти (valid, record, dated) з ресурсу.

    `record` — `Provenance.extracted_at` (коли картка дізналась). `dated` — чи
    valid справді заявлений у документі, чи це підставлена дата завантаження.
    """
    prov = getattr(resource, "provenance", None)
    record = _aware(prov.extracted_at) if prov is not None else _aware(fallback)
    dated = prov.date_stated if prov is not None else True
    valid = (
        getattr(resource, "effectiveDateTime", None)
        or getattr(resource, "onsetDateTime", None)
    )
    return (_aware(valid) if valid else record), record, bool(dated and valid)


def _label_of(cc, fallback: str) -> str:
    if cc is None:
        return fallback
    if cc.text:
        return cc.text
    for c in cc.coding:
        if c.display:
            return c.display
    return fallback


def build(
    observations: list[Observation],
    conditions: list[Condition],
    medications: list[MedicationRequest],
    *,
    document_dates: Optional[dict[str, datetime]] = None,
    gap_days: float = EPISODE_GAP_DAYS,
) -> TemporalGraph:
    """Зібрати темпоральну проєкцію з FHIR-ресурсів картки.

    `document_dates` — дати подання документів (valid-time якір для ресурсів без
    власної дати, насамперед призначень: `MedicationRequest` не має поля дати).
    """
    doc_dates = {k: _aware(v) for k, v in (document_dates or {}).items()}
    accs: dict[str, _Acc] = {}

    def _acc(kind: EntityKind, code: Optional[str], label: str) -> _Acc:
        key = entity_key(kind, code, label)
        if key not in accs:
            accs[key] = _Acc(kind, label, code)
        return accs[key]

    now = datetime.now(timezone.utc)

    def _anchored(doc_id: Optional[str], valid: datetime, record: datetime) -> bool:
        """Чи стоїть факт на реальній даті, а не на моменті обробки.

        Дві незалежні підстави, і жодна не покладається на чесність екстрактора
        щодо `date_stated` (детермінований stub, наприклад, завжди ставить True):

        1. документ прийшов із власною датою — вона в `doc_dates`;
        2. valid помітно розходиться з record — отже дату взято з вмісту, а не
           підставлено «зараз».
        """
        if doc_id and doc_id in doc_dates:
            return True
        return abs((_aware(valid) - _aware(record)).total_seconds()) >= 86400

    for o in observations:
        valid, record, dated = _valid_and_record(o, now)
        doc_id = o.provenance.source_doc_id if o.provenance else None
        acc = _acc("analyte", loinc_of(o), _label_of(o.code, "показник"))
        acc.add(valid, record, doc_id, dated, _anchored(doc_id, valid, record))

    for c in conditions:
        valid, record, dated = _valid_and_record(c, now)
        doc_id = c.provenance.source_doc_id if c.provenance else None
        # Стан без onsetDateTime датуємо документом, а не «зараз»: дата подання
        # виписки ближча до клінічної правди, ніж момент обробки.
        if getattr(c, "onsetDateTime", None) is None and doc_id in doc_dates:
            valid, dated = doc_dates[doc_id], False
        acc = _acc("problem", _code_of(c.code), _label_of(c.code, "стан"))
        acc.add(valid, record, doc_id, dated, _anchored(doc_id, valid, record))

    for m in medications:
        prov = m.provenance
        record = _aware(prov.extracted_at) if prov else now
        doc_id = prov.source_doc_id if prov else None
        # MedicationRequest не має поля дати — якір це дата документа.
        valid = doc_dates.get(doc_id or "", record)
        acc = _acc("drug", rxnorm_of(m), _label_of(m.medicationCodeableConcept, "препарат"))
        anchored = bool(doc_id and doc_id in doc_dates)
        acc.add(valid, record, doc_id, dated=anchored, anchored=anchored)
        # Інтервал активності: «active» → відкритий (триває); будь-який інший
        # статус → закритий датою документа. Ми знаємо лише те, що на цю дату
        # препарат уже не був активним; коли саме його відмінили — невідомо, і
        # вигадувати цю дату не можна.
        end = None if m.status == "active" else valid
        start = acc.interval.start if acc.interval else valid
        acc.interval = Interval(start=min(_aware(start), valid), end=end)

    entities = {k: a.build(k) for k, a in accs.items()}
    edges = _build_edges(entities, medications, conditions)
    episodes = _segment(entities, gap_days)
    horizon = max((_aware(e.last_valid) for e in entities.values()), default=None)
    return TemporalGraph(
        entities=entities, edges=edges, episodes=episodes, horizon=horizon
    )


def _code_of(cc) -> Optional[str]:
    if cc is None or not cc.coding:
        return None
    return cc.coding[0].code


def _build_edges(
    entities: dict[str, TemporalEntity],
    medications: list[MedicationRequest],
    conditions: list[Condition],
) -> list[TemporalEdge]:
    """Ребра `treats` (зчитані з `reason`) і `co_active` (перетин інтервалів)."""
    edges: list[TemporalEdge] = []
    by_condition_id = {c.id: c for c in conditions}

    for m in medications:
        if m.reason is None:
            continue
        cond = by_condition_id.get(m.reason.reference.split("/", 1)[-1])
        if cond is None:
            continue
        drug_key = entity_key(
            "drug", rxnorm_of(m), _label_of(m.medicationCodeableConcept, "препарат")
        )
        problem_key = entity_key("problem", _code_of(cond.code), _label_of(cond.code, "стан"))
        drug = entities.get(drug_key)
        if drug is None or problem_key not in entities or drug.interval is None:
            continue
        edge = TemporalEdge(
            source=drug_key,
            target=problem_key,
            rel="treats",
            interval=drug.interval,
            basis="MedicationRequest.reason",
        )
        if edge not in edges:
            edges.append(edge)

    drugs = [e for e in _sorted(entities.values()) if e.kind == "drug" and e.interval]
    for i, a in enumerate(drugs):
        for b in drugs[i + 1:]:
            if a.interval is None or b.interval is None or not a.interval.overlaps(b.interval):
                continue
            start = max(_aware(a.interval.start), _aware(b.interval.start))
            ends = [_aware(x.end) for x in (a.interval, b.interval) if x.end is not None]
            edges.append(
                TemporalEdge(
                    source=a.key,
                    target=b.key,
                    rel="co_active",
                    interval=Interval(start=start, end=min(ends) if ends else None),
                    basis="перетин інтервалів активності",
                )
            )
    return edges


def _segment(entities: dict[str, TemporalEntity], gap_days: float) -> list[Episode]:
    """Сегментація таймлайну за розривами — аналог розбиття книги на розділи."""
    if not entities:
        return []
    points: list[tuple[datetime, TemporalEntity]] = []
    for e in entities.values():
        points.append((_aware(e.first_valid), e))
        if _aware(e.last_valid) != _aware(e.first_valid):
            points.append((_aware(e.last_valid), e))
    points.sort(key=lambda p: (p[0], _KIND_ORDER.get(p[1].kind, 9), p[1].key))

    gap = timedelta(days=gap_days)
    groups: list[list[tuple[datetime, TemporalEntity]]] = [[points[0]]]
    for point in points[1:]:
        if point[0] - groups[-1][-1][0] > gap:
            groups.append([point])
        else:
            groups[-1].append(point)

    episodes: list[Episode] = []
    seen: set[str] = set()
    for idx, group in enumerate(groups):
        keys: dict[str, None] = {}
        docs: dict[str, None] = {}
        new: list[str] = []
        n_facts = 0
        for ts, ent in group:
            keys.setdefault(ent.key, None)
            for d in ent.doc_ids:
                docs.setdefault(d, None)
            if ent.key not in seen and _aware(ent.first_valid) == ts:
                seen.add(ent.key)
                new.append(ent.key)
                n_facts += ent.occurrences
        episodes.append(
            Episode(
                index=idx,
                start=group[0][0],
                end=group[-1][0],
                doc_ids=tuple(docs),
                entity_keys=tuple(keys),
                new_entity_keys=tuple(new),
                n_facts=n_facts,
            )
        )
    return episodes
