"""FastAPI-фасад над конвеєром LiveMedCard (демо-сервер, стан у пам'яті).

Ендпойнти:
  GET  /                  — веб-інтерфейс користувача (SPA)
  POST /ingest            — прогнати один документ
  POST /ingest/samples    — прогнати демо-таймлайн
  POST /reset             — очистити картку (новий пацієнт)
  GET  /timeline/{loinc}  — часовий ряд показника
  GET  /signals           — поточні детерміновані сигнали
  GET  /state             — агрегований стан картки (для UI)
  POST /ask               — grounded Q&A
  GET  /export            — експорт у FHIR Bundle
  POST /import            — імпорт з FHIR Bundle
  POST /review            — дія людини (L4) над сигналом чи документом
  GET  /audit             — журнали: рішення L3, дії людини, перетини межі
  GET  /metrics           — агреговані метрики

Стан одного пацієнта інкапсульовано в `_State` і серіалізовано `threading.Lock`:
sync-ендпойнти FastAPI виконуються в пулі потоків, тож без лока конкурентні
запити (кілька вкладок) гонялися б за спільною карткою. Жодного `global`.

Опційна персистентність: env `LMC_PERSIST_PATH` → автозбереження/автозавантаження
знімка картки у локальний JSON-файл (local-first). Пошкоджений файл знімка валить
старт сервера явно (див. `repository.load_card`) — це захист від мовчазного
перезапису даних порожнім станом.
"""
from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from datetime import date, datetime, timezone
from typing import Literal, Optional

import hashlib

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import economics, governance, history
from .calibration import calibration_summary
from .bundle import from_bundle, to_bundle
from .config import (
    EXTRACTOR,
    IMAGE_OCR,
    MAX_DOC_CHARS,
    METRICS_HISTORY_PATH,
    PERSIST_PATH,
    SEED_SAMPLES,
    STATIC_DIR,
)
from .dto import BoundaryEvent, Document, DocKind, ReviewEntry
from .lang import detect_lang
from .layers import l3_router
from .layers import l2b_nuextract, l2b_ocrlab
from .layers.l2b_ocrlab import OcrLabError
from .layers.l2b_extractor import (
    Extractor,
    LoraStage2Extractor,
    NuExtractExtractor,
    OcrLabExtractor,
    StubExtractor,
    TwoStageExtractor,
)
from .llm_client import LLMError
from .pipeline import LiveMedCard
from .qa import Answer, QA
from .fhir import Patient, rxnorm_of
from .reference import (
    load_dose_limits,
    load_interactions,
    load_sample_docs,
    range_for,
    ruleset_version,
)
from .repository import load_card, save_card
from .ocr import OcrService, OcrError
from . import ppocr
from .ppocr import PpOcrError


app = FastAPI(title="LiveMedCard", version="0.1.0")

_STATIC = STATIC_DIR


def _new_patient() -> Patient:
    return Patient(id="mom-001", gender="female", birthDate=date(1950, 1, 1))


def _make_extractor() -> Extractor:
    """Екстрактор L2b: 'ocrlab' | 'nuextract' | 'two_stage_lora' | 'two_stage' | 'stub'."""
    if EXTRACTOR == "ocrlab":
        return OcrLabExtractor()
    if EXTRACTOR == "nuextract":
        return NuExtractExtractor()
    if EXTRACTOR == "two_stage_lora":
        return LoraStage2Extractor()
    if EXTRACTOR == "two_stage":
        return TwoStageExtractor()
    return StubExtractor()


def _photo_ocr_mode() -> str:
    """Куди піде завантажене фото: 'cloud' | 'local' | 'none'.

    Публічне демо тримає детермінований `stub` для тексту, але фото віддає в хмарний
    ZeroGPU Lab — і UI мусить показувати саме це, а не загальне «без хмари».
    'none' — лише явне `LMC_IMAGE_OCR=none`: тоді SPA ховає завантаження фото.
    """
    if IMAGE_OCR == "none":
        return "none"
    backend = IMAGE_OCR or EXTRACTOR
    if backend == "ocrlab":
        return "cloud"
    # Решта читає фото на пристрої; для `stub` це vision-LLM через OcrService.
    return "local"


class _State:
    """Тримач картки одного пацієнта: доступ лише під `lock`."""

    def __init__(self, card: LiveMedCard) -> None:
        self.card = card
        self.lock = threading.Lock()

    def autosave(self) -> None:
        """Зберегти знімок, якщо ввімкнено персистентність. Кличеться під lock."""
        if PERSIST_PATH:
            save_card(self.card, PERSIST_PATH)


def _seeded_card() -> LiveMedCard:
    """Свіжа картка; для публічного демо — одразу із сід-таймлайном (stub, миттєво)."""
    card = LiveMedCard(_new_patient(), _make_extractor())
    if SEED_SAMPLES:
        card.ingest_all([Document(**d) for d in load_sample_docs()])
    return card


def _initial_card() -> LiveMedCard:
    if PERSIST_PATH:
        loaded = load_card(PERSIST_PATH, _make_extractor())
        if loaded is not None:
            return loaded
    return _seeded_card()


# Дві моделі стану:
#  • PERSIST_PATH заданий → персональний local-first режим: ОДНА персистентна
#    картка на весь сервер (як раніше);
#  • інакше (публічне демо) → картка НА СЕСІЮ (cookie), щоб відвідувачі не бачили
#    й не скидали картки одне одного. LRU-кеп обмежує памʼять.
_SESSION_COOKIE = "lmc_sid"
_MAX_SESSIONS = 300
_single_state: "_State | None" = _State(_initial_card()) if PERSIST_PATH else None
_sessions: "OrderedDict[str, _State]" = OrderedDict()
_sessions_lock = threading.Lock()


def _get_state(request: Request, response: Response) -> _State:
    """Стан для цього запиту: персональний (persist) або per-session (демо)."""
    if _single_state is not None:
        return _single_state
    sid = request.cookies.get(_SESSION_COOKIE)
    if sid:
        with _sessions_lock:
            state = _sessions.get(sid)
            if state is not None:
                _sessions.move_to_end(sid)  # LRU: свіжий доступ — у кінець
                return state
    # Новий сеанс — сідаємо картку ПОЗА глобальним локом (мінімум контенції).
    sid = secrets.token_urlsafe(16)
    state = _State(_seeded_card())
    with _sessions_lock:
        _sessions[sid] = state
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.popitem(last=False)  # витісняємо найдавніший
    # HF рендерить Space у cross-site iframe (huggingface.co → *.hf.space), тож для
    # HTTPS потрібен SameSite=None; Secure, інакше cookie не долетить і кожен запит
    # плодив би нову сесію. Локально (http) — Lax. Проксі HF ставить X-Forwarded-Proto.
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(
        _SESSION_COOKIE, sid, max_age=86400, httponly=True,
        samesite="none" if https else "lax", secure=https,
    )
    return state


class ReviewRequest(BaseModel):
    """Дія людини над сигналом (`signal_key`) або документом (`document_id`)."""

    action: Literal["acknowledged", "overridden", "referred"]
    actor_role: Literal["caregiver", "clinician"] = "caregiver"
    signal_key: Optional[str] = None
    document_id: Optional[str] = None
    note: str = Field(default="", max_length=2000)
    seconds_to_act: Optional[float] = Field(default=None, ge=0)


class AskRequest(BaseModel):
    question: str


@app.get("/")
def index() -> FileResponse:
    # Без сесійної залежності: liveness-полінг платформи не має плодити сесії.
    return FileResponse(_STATIC / "index.html")


@app.post("/ingest")
def ingest(doc: Document, state: _State = Depends(_get_state)):
    if len(doc.text) > MAX_DOC_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"Документ завеликий (> {MAX_DOC_CHARS} символів)",
        )
    with state.lock:
        report = state.card.ingest(doc)
        state.autosave()
    return report


class ImageIngestRequest(BaseModel):
    image_base64: str
    kind: DocKind = "note"


@app.post("/ingest/image")
def ingest_image(req: ImageIngestRequest, state: _State = Depends(_get_state)):
    if IMAGE_OCR == "none":
        # FR-CNT-005: жодного вихідного запиту (LM Studio, HF), лише чесна відмова.
        raise HTTPException(
            status_code=503,
            detail="Розпізнавання фото в цьому розгортанні вимкнено",
        )
    if not req.image_base64.strip():
        raise HTTPException(
            status_code=422,
            detail="Зображення порожнє або некоректне",
        )

    # Бекенд OCR для фото відокремлений від текстового EXTRACTOR (config.IMAGE_OCR):
    # публічне демо тримає `stub` для сіду/тексту (миттєво, детерміновано, 0 хмари),
    # а фото — реальний OCR через хмарний Lab. `nuextract`/`ocrlab` самі роблять
    # image→markdown, тож окремий OCR не потрібен. Витяг фактів для цього документа
    # робить відповідний екстрактор (override), не змінюючи дефолт картки.
    img_backend = IMAGE_OCR or EXTRACTOR
    img_extractor: Extractor | None = None
    try:
        if img_backend == "ocrlab":
            text = l2b_ocrlab.image_to_markdown(req.image_base64)
            # Зображення передаємо в екстрактор: якщо транскрипція вийшла неповною
            # і фактів у ній нема, він добере їх прямо з фото (запасний шлях).
            img_extractor = OcrLabExtractor(image_base64=req.image_base64)
        elif img_backend == "nuextract":
            text = l2b_nuextract.image_to_markdown(req.image_base64)
            img_extractor = NuExtractExtractor()
        elif img_backend == "ppocr":
            # PP-OCR лише транскрибує (детекція рядків + розпізнавання), тож факти
            # з нього бере дефолтний детермінований екстрактор картки — без LLM.
            text = ppocr.image_to_text(req.image_base64)
        else:
            text = OcrService.perform_ocr(req.image_base64)
    except (OcrError, LLMError, OcrLabError, PpOcrError) as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Помилка під час OCR: {exc}",
        )

    if not text:
        raise HTTPException(
            status_code=422,
            detail="Не вдалося розпізнати текст із зображення",
        )

    if len(text) > MAX_DOC_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"Розпізнаний текст документа завеликий (> {MAX_DOC_CHARS} символів)",
        )

    doc_id = f"doc-ocr-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}"
    doc = Document(id=doc_id, text=text, kind=req.kind)

    try:
        with state.lock:
            report = state.card.ingest(doc, extractor=img_extractor)
            state.autosave()
    except OcrLabError as exc:  # витяг фактів Stage 2 у хмарному Lab
        raise HTTPException(status_code=503, detail=str(exc))
    return report



@app.get("/samples")
def samples(lang: str = "uk"):
    """Демо-документи як вхідні дані — БЕЗ завантаження в картку.

    Дає журі побачити сирий документ до того, як картка з нього щось витягне:
    спершу вхід, потім результат. Сесія не потрібна — нічого не змінюємо.
    """
    return {"documents": load_sample_docs(lang)}


@app.get("/samples/image")
def sample_image() -> FileResponse:
    """Синтетичний бланк лабораторії — фото-шлях (OCR) для того самого порівняння."""
    return FileResponse(_STATIC / "sample_lab_report.jpg", media_type="image/jpeg")


@app.post("/ingest/samples")
def ingest_samples(lang: str = "uk", state: _State = Depends(_get_state)):
    """Демо-документи мовою `lang` (`uk`|`en`) — набори дзеркальні за значеннями."""
    docs = [Document(**d) for d in load_sample_docs(lang)]
    with state.lock:
        reports = state.card.ingest_all(docs)
        state.autosave()
    return reports


@app.post("/reset")
def reset(state: _State = Depends(_get_state)):
    with state.lock:
        state.card = LiveMedCard(_new_patient(), _make_extractor())
        state.autosave()
        return {"status": "reset", "patient": state.card.patient.id}


@app.get("/export")
def export_bundle(state: _State = Depends(_get_state)):
    """Експорт картки як FHIR Bundle (для передачі лікарю / еЗдоров'ю).

    Єдиний GET, що пише в стан — і саме тому: експорт є моментом, коли картка
    йде назовні й починає жити без нашого інтерфейсу. Якщо в ній лишились
    непереглянуті сигнали класу `mandatory_review`, рекомендація виходить у світ
    як рішення, і це фіксується як перетин задекларованої межі автономії (ABC).
    Експорт не блокуємо — це нагляд, а не блокування; але подія лишається.
    """
    with state.lock:
        card = state.card
        pending = governance.unreviewed_mandatory(
            card.current_signals(), card.reviewed_signal_keys()
        )
        if pending:
            card.record_boundary_event(BoundaryEvent(
                ts=datetime.now(timezone.utc),
                kind="export_with_unreviewed",
                detail=(
                    f"Картку експортовано з {len(pending)} непереглянутими "
                    f"сигналами, що вимагають рішення людини"
                ),
                signal_keys=[s.key for s in pending],
            ))
            state.autosave()
        return to_bundle(card)


@app.post("/review")
def review(req: ReviewRequest, state: _State = Depends(_get_state)):
    """Записати дію людини (L4) над сигналом або рішенням щодо документа."""
    if (req.signal_key is None) == (req.document_id is None):
        raise HTTPException(
            status_code=422,
            detail="Потрібно рівно одне з двох: signal_key або document_id",
        )
    with state.lock:
        card = state.card
        if req.signal_key is not None:
            if req.signal_key not in {s.key for s in card.current_signals()}:
                raise HTTPException(
                    status_code=404,
                    detail=f"Сигнал {req.signal_key} у картці відсутній",
                )
        elif req.document_id not in card.documents:
            raise HTTPException(
                status_code=404, detail=f"Документ {req.document_id} у картці відсутній"
            )
        entry = card.review(ReviewEntry(
            ts=datetime.now(timezone.utc),
            actor_role=req.actor_role,
            action=req.action,
            signal_key=req.signal_key,
            document_id=req.document_id,
            note=req.note,
            seconds_to_act=req.seconds_to_act,
        ))
        state.autosave()
        return entry.model_dump(mode="json")


@app.post("/import")
def import_bundle(bundle: dict = Body(...), state: _State = Depends(_get_state)):
    """Імпорт картки з FHIR Bundle (замінює поточний стан)."""
    try:
        card = from_bundle(bundle, _make_extractor())
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Некоректний Bundle: {exc}")
    with state.lock:
        state.card = card
        state.autosave()
        return {"status": "imported", "documents": state.card.document_count}


@app.get("/audit")
def audit(state: _State = Depends(_get_state)):
    """Журнали спостережуваності (EU AI Act ст. 12).

    Три різні події — три списки: що вирішив конвеєр, що зробила людина, де
    систему використали поза задекларованою межею автономії.
    """
    with state.lock:
        card = state.card
        return {
            "decisions": card.audit_log(),
            "reviews": card.review_log(),
            "boundary_events": card.boundary_log(),
        }


def _path_cost(log: list[dict]) -> dict:
    """TCC — вартість шляху виконання, за класом шляху.

    Групуємо за екстрактором, бо саме він задає порядок вартості: детермінований
    регекс і VLM відрізняються не на відсотки. Це та половина CPR, яку можна
    міряти в проді; друга половина (чи дешевша модель упоралася б) міряється
    офлайн бенчмарком і записана в `docs/cpr.md`.
    """
    by_path: dict[str, list[float]] = {}
    for a in log:
        by_path.setdefault(a.get("extractor") or "unknown", []).append(
            a.get("latency_ms") or 0.0
        )
    retried = [a for a in log if (a.get("attempts") or 1) > 1]
    resolved = [a for a in retried if not a["escalate"]]
    return {
        "paths": {
            path: {
                "documents": len(xs),
                "mean_latency_ms": round(sum(xs) / len(xs), 1),
                "max_latency_ms": round(max(xs), 1),
            }
            for path, xs in sorted(by_path.items())
        },
        "reinvocations": len(retried),
        # FPA — частка повторних спроб, які зняли ескалацію. Це те, що політика
        # повторного виклику купує: людину не викликали, бо система розібралась
        # сама. `None` без жодної спроби — порожній знаменник, не нуль.
        "false_positive_attenuation": (
            round(len(resolved) / len(retried), 3) if retried else None
        ),
    }


def _rule_coverage(card: LiveMedCard) -> dict:
    """RCR — яку частку витягнутого правила взагалі здатні побачити.

    Найдешевша чесна метрика в системі. Довідники демо-масштабу (4 пари
    взаємодій, 4 дозові межі, 8 діапазонів), і без цього числа легко переплутати
    «показник у нормі» з «показник, для якого норми не існує». Некодовані факти
    рахуються в знаменнику саме тому: вони витягнуті, але для шару безпеки їх
    ніби нема.
    """
    coded = card.factstore.loincs()
    uncoded = {f.keyword for f in card.factstore.uncoded()}
    with_range = [loinc for loinc in coded if range_for(loinc, None) is not None]

    limits = load_dose_limits()
    known_pairs = {c for entry in load_interactions() for c in entry.pair()}
    rx = [rxnorm_of(m) for m in card.medications]
    covered_meds = [
        code for code in rx if code is not None and (code in limits or code in known_pairs)
    ]

    def _ratio(num: int, den: int) -> "float | None":
        return round(num / den, 3) if den else None

    n_labs, n_meds = len(coded) + len(uncoded), len(rx)
    return {
        "labs_total": n_labs,
        "labs_with_reference_range": len(with_range),
        "lab_coverage": _ratio(len(with_range), n_labs),
        "medications_total": n_meds,
        "medications_with_rule": len(covered_meds),
        "medication_coverage": _ratio(len(covered_meds), n_meds),
        "coverage": _ratio(len(with_range) + len(covered_meds), n_labs + n_meds),
    }


def _resolves(subject: str, card: LiveMedCard) -> bool:
    """Чи веде посилання сигналу на щось справді збережене.

    Три форми, бо шари посилаються по-різному: L1 — на FHIR-ресурс, L2a-тренди —
    на LOINC у факт-сторі, темпоральні — на ключ сутності графа.
    """
    if "/" in subject:
        return card.graph.g.has_node(subject)
    if ":" in subject:
        return subject in card.temporal().entities
    return subject in set(card.factstore.loincs())


def _evidence_metrics(card: LiveMedCard) -> dict:
    """ETC — частка тверджень із відновлюваним ланцюгом «дані → висновок».

    Для фактів вона близька до 1.0 за інваріантом `_assert_provenance`; сенс її
    вимірювати в тому, що інваріант охоплює лише момент витягу. Сигнал може
    посилатися на ресурс, якого вже нема в картці, — і тоді ланцюг рветься там,
    де його ніхто не перевіряв.
    """
    facts = [f for loinc in card.factstore.loincs() for f in card.factstore.series(loinc)]
    facts += card.factstore.uncoded()
    grounded = [
        f for f in facts
        if f.source_doc_id and card.graph.g.has_node(f"Document/{f.source_doc_id}")
    ]
    signals = card.current_signals()
    complete = [
        s for s in signals
        if s.ruleset_version and all(_resolves(x, card) for x in s.subjects)
    ]

    def _ratio(num: int, den: int) -> "float | None":
        return round(num / den, 3) if den else None

    return {
        "facts": len(facts),
        "facts_with_source_document": len(grounded),
        "fact_trail": _ratio(len(grounded), len(facts)),
        "signals": len(signals),
        "signal_trail": _ratio(len(complete), len(signals)),
        "ruleset_version": ruleset_version(),
    }


def _supervision_metrics(card: LiveMedCard) -> dict:
    """Метрики шару L4 (EU AI Act ст. 14): OvR, RBI, SNR, покриття, ABC.

    Усі рахуються з журналу дій людини. Доки такого журналу не було, ці числа не
    просто не мірялись — їх не існувало, і твердження «людина в контурі» не мало
    жодного спостережуваного наслідку.

    `None` там, де знаменник порожній: нуль означав би виміряний нуль, а це
    різні речі — «ніхто не перекривав» і «ще нікого не просили дивитися».
    """
    reviews = card.review_log()
    audit = card.audit_log()
    escalated = [a for a in audit if a["escalate"]]
    reviewed_docs = {r["document_id"] for r in reviews if r["document_id"]}

    n_rev = len(reviews)
    overridden = sum(1 for r in reviews if r["action"] == "overridden")
    acted = sum(1 for r in reviews if r["action"] in ("overridden", "referred"))
    timed = [r["seconds_to_act"] for r in reviews if r["seconds_to_act"] is not None]
    critical = sum(1 for a in escalated if a["escalation_drive"] in ("risk", "both"))
    pending_mandatory = governance.unreviewed_mandatory(
        card.current_signals(), card.reviewed_signal_keys()
    )

    def _ratio(num: int, den: int) -> "float | None":
        return round(num / den, 3) if den else None

    return {
        "reviews": n_rev,
        "overridden": overridden,
        # OvR: частка виходів AI, які людина відхилила. Нижче ~5% — ознака
        # штампування, вище ~20% — ознака поганого калібрування.
        "override_rate": _ratio(overridden, n_rev),
        # Скільки ескалацій узагалі дочекались людини.
        "review_coverage": _ratio(
            sum(1 for a in escalated if a["document_id"] in reviewed_docs),
            len(escalated),
        ),
        # RBI: середній час на кейс. None, якщо клієнт його не міряв.
        "review_burden_seconds": round(sum(timed) / len(timed), 1) if timed else None,
        # SNR: яка частка потоку до людини була справді про знайдену небезпеку,
        # а не про нерозбірливий текст. Низький SNR → втома від сповіщень.
        "signal_to_noise": _ratio(critical, len(escalated)),
        # EP лише проксі: справжня вимагає ex-post оцінки, чи була ескалація
        # виправдана. Тут — чи людина зробила щось, окрім квитування.
        "ep_proxy": _ratio(acted, sum(1 for a in escalated if a["document_id"] in reviewed_docs)),
        "outstanding_escalations": len(card.outstanding_escalations()),
        "unreviewed_mandatory": len(pending_mandatory),
        # ABC: скільки разів картку використали поза задекларованою межею.
        "boundary_excursions": len(card.boundary_log()),
    }


@app.get("/metrics")
def metrics(state: _State = Depends(_get_state)):
    """Агреговані метрики роботи конвеєра."""
    with state.lock:
        card = state.card
        log = card.audit_log()
        n = len(log)
        escalated = sum(1 for a in log if a["escalate"])

        # Економіка рішень: у що обходиться поточна політика L3 і що саме купує
        # ескалація. `no_escalation` — контрфактичний прогін тієї самої партії
        # документів без жодної передачі людині: різниця і є цінністю шару L3.
        cost_actual = sum(
            economics.expected_cost(
                a["extraction_confidence"], a.get("hazard", "medication"),
                escalated=a["escalate"], high_risk_signal=a.get("high_risk", False),
            )
            for a in log
        )
        cost_no_escalation = sum(
            economics.expected_cost(
                a["extraction_confidence"], a.get("hazard", "medication"),
                escalated=False, high_risk_signal=a.get("high_risk", False),
            )
            for a in log
        )
        return {
            "documents": card.document_count,
            "observations": len(card.observations),
            "medications": len(card.medications),
            "decisions": n,
            "escalated": escalated,
            "auto": n - escalated,
            "escalation_rate": round(escalated / n, 3) if n else 0.0,
            "active_signals": len(card.current_signals()),
            "cost": _path_cost(log),
            "calibration": calibration_summary(),
            "rule_coverage": _rule_coverage(card),
            "evidence": _evidence_metrics(card),
            "supervision": _supervision_metrics(card),
            "economics": {
                **economics.summary(),
                "expected_cost": round(cost_actual, 2),
                "expected_cost_without_escalation": round(cost_no_escalation, 2),
                "escalation_saves": round(cost_no_escalation - cost_actual, 2),
            },
        }


@app.post("/metrics/snapshot")
def metrics_snapshot(state: _State = Depends(_get_state)):
    """Зафіксувати поточні метрики в часовому ряді (вхід для OSI).

    Каденцію задає оператор (реліз / доба / тиждень), а не система: правильна
    залежить від розгортання, і вгадувати її тут нема підстав.
    """
    if not METRICS_HISTORY_PATH:
        raise HTTPException(
            status_code=503,
            detail="Часовий ряд метрик вимкнено (env LMC_METRICS_HISTORY)",
        )
    return history.append(metrics(state), METRICS_HISTORY_PATH)


@app.get("/metrics/history")
def metrics_history():
    """Часовий ряд метрик і що в ньому зрушило (OSI)."""
    if not METRICS_HISTORY_PATH:
        raise HTTPException(
            status_code=503,
            detail="Часовий ряд метрик вимкнено (env LMC_METRICS_HISTORY)",
        )
    rows = history.load(METRICS_HISTORY_PATH)
    return {"rows": rows, "drift": history.drift(rows)}


@app.get("/timeline/{loinc}")
def timeline(loinc: str, state: _State = Depends(_get_state)):
    with state.lock:
        return state.card.factstore.series(loinc)


@app.get("/signals")
def signals(state: _State = Depends(_get_state)):
    with state.lock:
        return state.card.current_signals()


@app.get("/document/{doc_id}")
def document(doc_id: str, state: _State = Depends(_get_state)):
    """Дві іпостасі документа — окремо й підписано (EU AI Act ст. 13).

    ``text`` — те, що подала людина. ``generated`` — стиснення Stage 1, написане
    моделлю; воно є лише на двоетапних шляхах. Раніше обидва жили в одному полі,
    і UI показував текст моделі під заголовком «Джерело»: якби Stage 1
    галюцинував значення, підсвічений «доказ» був би самою галюцинацією.

    ``span_base`` каже, до якої з іпостасей прив'язані offset-и фактів — вони
    рахуються по тій, з якої витягували.
    """
    with state.lock:
        node = state.card.graph.g.nodes.get(f"Document/{doc_id}")
        if node is None:
            raise HTTPException(status_code=404, detail=f"Документ {doc_id} не знайдено")
        generated = node.get("generated_text") or ""
        return {
            "id": doc_id,
            "text": node.get("text") or "",
            "generated": generated,
            "span_base": "generated" if generated else "text",
        }


@app.get("/graph")
def graph(state: _State = Depends(_get_state)):
    """Граф контексту як вузли/ребра — для візуалізації провенансу в інтерфейсі.

    Віддає те, що вже є в networkx-графі; нічого не рахує. Вузол `Document` несе
    лише мітку (сам текст — за `/document/{id}`), щоб відповідь лишалась легкою.
    """
    with state.lock:
        card = state.card
        g = card.graph.g
        # Вузол-вимірювання без значення — це просто повторений підпис («Креатинін»
        # чотири рази поспіль). Значення робить граф читабельним: видно і ЩО, і СКІЛЬКИ.
        values = {
            f"Observation/{o.id}": (
                f"{o.valueQuantity.value:g} {o.valueQuantity.unit or ''}".strip()
            )
            for o in card.observations
            if o.valueQuantity is not None
        }
        nodes = []
        for n, d in g.nodes(data=True):
            # у вузла-документа мітки немає — показуємо його id
            label = d.get("label") or n.split("/", 1)[-1]
            if n in values:
                label = f"{label} {values[n]}"
            nodes.append({"id": n, "type": d.get("type", "?"), "label": label})
        edges = [
            {"source": u, "target": v, "rel": d.get("rel", "")}
            for u, v, d in g.edges(data=True)
        ]
        return {"nodes": nodes, "edges": edges}


@app.get("/temporal")
def temporal_view(state: _State = Depends(_get_state)):
    """Темпоральна проєкція: сутності в часі, епізоди, метрики топології.

    Описова — сигналів не породжує (вони приходять через `/signals`, як і всі
    інші). Дає інтерфейсу дані для доріжок таймлайну й маркерів першої появи.
    """
    with state.lock:
        return state.card.temporal().to_payload()


@app.get("/state")
def get_state_endpoint(lang: str = "uk", state: _State = Depends(_get_state)):
    """Агрегований знімок картки для інтерфейсу (`lang=uk|en` — мова текстів сигналів)."""
    lang = "en" if lang == "en" else "uk"
    with state.lock:
        card = state.card
        sigs = card.current_signals(lang)
        # Що в картці лишається невирішеним. Не `route` з вигаданою впевненістю
        # 1.0: та підміна мовчки викидала половину політики — ескалація через
        # погано розпізнаний текст зникала зі стану одразу після завантаження.
        reviewed = card.last_review_by_signal()
        decision = l3_router.card_decision(
            sigs, card.outstanding_escalations(), lang, reviewed_keys=reviewed
        )
        sex = card.patient.gender

        series = []
        for loinc in card.factstore.loincs():
            pts = card.factstore.series(loinc)
            ref = range_for(loinc, sex)
            series.append({
                "loinc": loinc,
                "keyword": pts[0].keyword,
                "unit": pts[-1].unit,
                "low": ref.low if ref else None,
                "high": ref.high if ref else None,
                "points": [
                    {"ts": p.ts.date().isoformat(), "value": p.value,
                     "source": p.source_doc_id, "span": p.source_span,
                     "dated": p.date_stated}
                    for p in pts
                ],
            })

        # Факти без LOINC: витягнуті, але некодовані (напр. TIR-бари AGP-звіту).
        # Сигналів не дають — межі порівнювати нема з чим. Показуємо їх окремо:
        # мовчки з'їдений факт неможливо відрізнити від невитягнутого.
        uncoded = [
            {"keyword": f.keyword, "value": f.value, "unit": f.unit,
             "ts": f.ts.date().isoformat(), "source": f.source_doc_id,
             "span": f.source_span, "dated": f.date_stated}
            for f in card.factstore.uncoded()
        ]

        # Подані документи: тип, дата, мова, розмір. Мова — з ТЕКСТУ (detect_lang),
        # а не з назви файлу; без тексту lang=None, і інтерфейс краще не покаже
        # бейджа взагалі, ніж навмання «UA» (detect_lang("") == "uk").
        # Документи з невирішеною ескалацією: її не закрити переглядом сигналу —
        # «текст погано розпізнано» не є сигналом. Закриває її погляд на оригінал,
        # тож інтерфейс має знати, у якого документа він ще потрібен.
        pending_docs = {a.document_id for a in card.outstanding_escalations()}
        docs = {}
        for d in card.document_references:
            did = d.provenance.source_doc_id if d.provenance else d.id
            text = d.content.data or ""
            docs[did] = {
                "lang": detect_lang(text) if text.strip() else None,
                "kind": d.type.text if d.type else None,
                "date": d.date.date().isoformat() if d.date else None,
                "chars": len(text),
                "escalated": did in pending_docs,
            }

        seen: dict[str, None] = {}
        for n in card.graph.nodes_of_type("MedicationRequest"):
            seen.setdefault(card.graph.g.nodes[n].get("label", n), None)
        medications = list(seen)

        return {
            "patient": {"id": card.patient.id, "gender": card.patient.gender},
            "document_count": card.document_count,
            "series": series,
            "uncoded": uncoded,
            "docs": docs,
            "medications": medications,
            # До кожного сигналу — його клас автономії і чи вже переглянутий.
            # Інтерфейс не має вирішувати, що обов'язкове: межа задекларована в
            # `governance.py`, а не розсіяна по кнопках.
            "signals": [
                {
                    **s.model_dump(),
                    "autonomy": governance.autonomy_class(s),
                    "reviewed": (
                        reviewed[s.key].model_dump(mode="json")
                        if s.key in reviewed else None
                    ),
                }
                for s in sigs
            ],
            "decision": decision.model_dump(),
            # Куди піде ФОТО. Інтерфейс мусить казати про це правду: у публічному
            # демо текст/сід/безпека лишаються на сервері, а фото їде в хмарний Lab.
            "photo_ocr": _photo_ocr_mode(),
        }


@app.post("/ask", response_model=Answer)
def ask(req: AskRequest, state: _State = Depends(_get_state)) -> Answer:
    with state.lock:
        card = state.card
        qa = QA(
            card.factstore,
            card.graph,
            card.current_signals(),
            sex=card.patient.gender,
            temporal=card.temporal(),
        )
        return qa.ask(req.question)


@app.get("/health")
def health():
    # Без сесійної залежності: не плодимо сесії на кожен liveness-пінг.
    if _single_state is not None:
        return {"status": "ok", "mode": "single", "documents": _single_state.card.document_count}
    return {"status": "ok", "mode": "session", "sessions": len(_sessions)}
