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
  GET  /audit             — журнал рішень L3
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
from datetime import date

import hashlib

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .bundle import from_bundle, to_bundle
from .config import (
    EXTRACTOR,
    IMAGE_OCR,
    MAX_DOC_CHARS,
    PERSIST_PATH,
    SEED_SAMPLES,
    STATIC_DIR,
)
from .dto import Document, DocKind
from .fhir import Patient
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
from .reference import load_sample_docs, range_for
from .repository import load_card, save_card
from .ocr import OcrService, OcrError


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
        else:
            text = OcrService.perform_ocr(req.image_base64)
    except (OcrError, LLMError, OcrLabError) as exc:
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



@app.post("/ingest/samples")
def ingest_samples(state: _State = Depends(_get_state)):
    docs = [Document(**d) for d in load_sample_docs()]
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
    """Експорт картки як FHIR Bundle (для передачі лікарю / еЗдоров'ю)."""
    with state.lock:
        return to_bundle(state.card)


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
    """Журнал рішень L3 (спостережуваність)."""
    with state.lock:
        return state.card.audit_log()


@app.get("/metrics")
def metrics(state: _State = Depends(_get_state)):
    """Агреговані метрики роботи конвеєра."""
    with state.lock:
        card = state.card
        log = card.audit_log()
        n = len(log)
        escalated = sum(1 for a in log if a["escalate"])
        return {
            "documents": card.document_count,
            "observations": len(card.observations),
            "medications": len(card.medications),
            "decisions": n,
            "escalated": escalated,
            "auto": n - escalated,
            "escalation_rate": round(escalated / n, 3) if n else 0.0,
            "active_signals": len(card.current_signals()),
        }


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
    """Free-text іпостась документа (вузол графа) — для span-провенансу в UI."""
    with state.lock:
        node = state.card.graph.g.nodes.get(f"Document/{doc_id}")
        if node is None or "text" not in node:
            raise HTTPException(status_code=404, detail=f"Документ {doc_id} не знайдено")
        return {"id": doc_id, "text": node["text"]}


@app.get("/state")
def get_state_endpoint(lang: str = "uk", state: _State = Depends(_get_state)):
    """Агрегований знімок картки для інтерфейсу (`lang=uk|en` — мова текстів сигналів)."""
    lang = "en" if lang == "en" else "uk"
    with state.lock:
        card = state.card
        sigs = card.current_signals(lang)
        # Рішення L3 над поточним станом (сигнали детерміновані → впевненість 1.0).
        doc_kind = "card state" if lang == "en" else "стан картки"
        decision = l3_router.route(sigs, 1.0, doc_kind, lang)
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
                     "source": p.source_doc_id, "span": p.source_span}
                    for p in pts
                ],
            })

        seen: dict[str, None] = {}
        for n in card.graph.nodes_of_type("MedicationRequest"):
            seen.setdefault(card.graph.g.nodes[n].get("label", n), None)
        medications = list(seen)

        return {
            "patient": {"id": card.patient.id, "gender": card.patient.gender},
            "document_count": card.document_count,
            "series": series,
            "medications": medications,
            "signals": [s.model_dump() for s in sigs],
            "decision": decision.model_dump(),
        }


@app.post("/ask", response_model=Answer)
def ask(req: AskRequest, state: _State = Depends(_get_state)) -> Answer:
    with state.lock:
        card = state.card
        qa = QA(card.factstore, card.graph, card.current_signals(), sex=card.patient.gender)
        return qa.ask(req.question)


@app.get("/health")
def health():
    # Без сесійної залежності: не плодимо сесії на кожен liveness-пінг.
    if _single_state is not None:
        return {"status": "ok", "mode": "single", "documents": _single_state.card.document_count}
    return {"status": "ok", "mode": "session", "sessions": len(_sessions)}
