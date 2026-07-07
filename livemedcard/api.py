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

import threading
from datetime import date

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .bundle import from_bundle, to_bundle
from .config import MAX_DOC_CHARS, PERSIST_PATH, STATIC_DIR
from .dto import Document
from .fhir import Patient
from .layers import l3_router
from .pipeline import LiveMedCard
from .qa import Answer, QA
from .reference import load_sample_docs, range_for
from .repository import load_card, save_card

app = FastAPI(title="LiveMedCard", version="0.1.0")

_STATIC = STATIC_DIR


def _new_patient() -> Patient:
    return Patient(id="mom-001", gender="female", birthDate=date(1950, 1, 1))


class _State:
    """Тримач картки одного пацієнта: доступ лише під `lock`."""

    def __init__(self, card: LiveMedCard) -> None:
        self.card = card
        self.lock = threading.Lock()

    def autosave(self) -> None:
        """Зберегти знімок, якщо ввімкнено персистентність. Кличеться під lock."""
        if PERSIST_PATH:
            save_card(self.card, PERSIST_PATH)


def _initial_card() -> LiveMedCard:
    if PERSIST_PATH:
        loaded = load_card(PERSIST_PATH)
        if loaded is not None:
            return loaded
    return LiveMedCard(_new_patient())


_state = _State(_initial_card())


class AskRequest(BaseModel):
    question: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.post("/ingest")
def ingest(doc: Document):
    if len(doc.text) > MAX_DOC_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"Документ завеликий (> {MAX_DOC_CHARS} символів)",
        )
    with _state.lock:
        report = _state.card.ingest(doc)
        _state.autosave()
    return report


@app.post("/ingest/samples")
def ingest_samples():
    docs = [Document(**d) for d in load_sample_docs()]
    with _state.lock:
        reports = _state.card.ingest_all(docs)
        _state.autosave()
    return reports


@app.post("/reset")
def reset():
    with _state.lock:
        _state.card = LiveMedCard(_new_patient())
        _state.autosave()
        return {"status": "reset", "patient": _state.card.patient.id}


@app.get("/export")
def export_bundle():
    """Експорт картки як FHIR Bundle (для передачі лікарю / еЗдоров'ю)."""
    with _state.lock:
        return to_bundle(_state.card)


@app.post("/import")
def import_bundle(bundle: dict = Body(...)):
    """Імпорт картки з FHIR Bundle (замінює поточний стан)."""
    try:
        card = from_bundle(bundle)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Некоректний Bundle: {exc}")
    with _state.lock:
        _state.card = card
        _state.autosave()
        return {"status": "imported", "documents": _state.card.document_count}


@app.get("/audit")
def audit():
    """Журнал рішень L3 (спостережуваність)."""
    with _state.lock:
        return _state.card.audit_log()


@app.get("/metrics")
def metrics():
    """Агреговані метрики роботи конвеєра."""
    with _state.lock:
        card = _state.card
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
def timeline(loinc: str):
    with _state.lock:
        return _state.card.factstore.series(loinc)


@app.get("/signals")
def signals():
    with _state.lock:
        return _state.card.current_signals()


@app.get("/state")
def state():
    """Агрегований знімок картки для інтерфейсу."""
    with _state.lock:
        card = _state.card
        sigs = card.current_signals()
        # Рішення L3 над поточним станом (сигнали детерміновані → впевненість 1.0).
        decision = l3_router.route(sigs, 1.0, "стан картки")
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
                    {"ts": p.ts.date().isoformat(), "value": p.value, "source": p.source_doc_id}
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
def ask(req: AskRequest) -> Answer:
    with _state.lock:
        card = _state.card
        qa = QA(card.factstore, card.graph, card.current_signals(), sex=card.patient.gender)
        return qa.ask(req.question)


@app.get("/health")
def health():
    with _state.lock:
        return {
            "status": "ok",
            "patient": _state.card.patient.id,
            "documents": _state.card.document_count,
        }
