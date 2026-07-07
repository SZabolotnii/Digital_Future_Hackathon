"""Оркестрація конвеєра TRACE: документ → витяг → сховища → сигнали → рішення.

Головний інваріант: сигнали безпеки (L1/L2a) рахуються детерміновано на факт-сторі,
а L2b (екстрактор) лише витягує факти й не робить висновків про безпеку.

`LiveMedCard` — джерело істини для FHIR-ресурсів пацієнта; `FactStore` і
`KnowledgeGraph` — похідні проєкції над ними. Це дає знімок стану (snapshot),
експорт у FHIR Bundle і аудит рішень.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .dto import AuditEntry, Document, ExtractionResult, Signal, TraceReport
from .fhir import Condition, MedicationRequest, Observation, Patient
from .layers import l1_safety, l2a_trends, l3_router
from .layers.l2b_extractor import Extractor, StubExtractor
from .stores import FactStore, KnowledgeGraph


class LiveMedCard:
    """Стан однієї медичної картки пацієнта + прогін документів крізь TRACE."""

    def __init__(self, patient: Patient, extractor: Optional[Extractor] = None) -> None:
        self.patient = patient
        self.extractor: Extractor = extractor or StubExtractor()
        self.factstore = FactStore()
        self.graph = KnowledgeGraph()
        self.graph.add_patient(patient)
        # Джерело істини — утримувані FHIR-ресурси (дедуплікація за id).
        self._observations: list[Observation] = []
        self._conditions: list[Condition] = []
        self._medications: list[MedicationRequest] = []
        self._obs_ids: set[str] = set()
        self._cond_ids: set[str] = set()
        self._med_ids: set[str] = set()
        self._document_ids: set[str] = set()
        self._audit: list[AuditEntry] = []

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

    # ── L1 + L2a на поточному стані сховищ ────────────────────────────────────
    def current_signals(self) -> list[Signal]:
        """Детерміновані сигнали над усім накопиченим станом."""
        return l1_safety.run(self._medications) + l2a_trends.run(
            self.factstore, self.patient.gender
        )

    def ingest(self, doc: Document) -> TraceReport:
        """Прогнати один документ і повернути повний TRACE-звіт поточного стану."""
        # Без мітки часу обзервації не потрапляють у факт-стор і зникають з
        # таймлайну (і з перевірок безпеки). Проставляємо час прийому документа.
        if doc.received_at is None:
            doc = doc.model_copy(update={"received_at": datetime.now(timezone.utc)})
        self._document_ids.add(doc.id)
        ext = self.extractor.extract(doc, self.patient.id)
        self._absorb(ext)
        signals = self.current_signals()
        decision = l3_router.route(signals, ext.mean_confidence, doc.kind)
        self._record_audit(doc, ext, signals, decision)
        return TraceReport(
            document_id=doc.id, extraction=ext, signals=signals, decision=decision
        )

    def ingest_all(self, docs: list[Document]) -> list[TraceReport]:
        return [self.ingest(d) for d in docs]

    # ── аудит рішень L3 ───────────────────────────────────────────────────────
    def _record_audit(self, doc, ext, signals, decision) -> None:
        self._audit.append(AuditEntry(
            ts=datetime.now(timezone.utc),
            document_id=doc.id,
            kind=doc.kind,
            extraction_confidence=ext.mean_confidence,
            n_signals=len(signals),
            route_to=decision.route_to,
            escalate=decision.escalate,
        ))

    def audit_log(self) -> list[dict]:
        """Журнал рішень L3 як JSON-сумісні dict (для API/метрик)."""
        return [a.model_dump(mode="json") for a in self._audit]

    # ── знімок / відновлення (персистентність, FHIR Bundle) ───────────────────
    def snapshot(self) -> dict:
        """Серіалізований стан картки (FHIR-ресурси + метадані)."""
        return {
            "patient": self.patient.model_dump(mode="json", by_alias=True),
            "observations": [o.model_dump(mode="json") for o in self._observations],
            "conditions": [c.model_dump(mode="json") for c in self._conditions],
            "medications": [m.model_dump(mode="json") for m in self._medications],
            "documents": sorted(self._document_ids),
            "audit": [a.model_dump(mode="json") for a in self._audit],
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
        card._document_ids.update(data.get("documents", []))
        # Валідація журналу при відновленні: биті записи → явна помилка, не сміття.
        card._audit.extend(AuditEntry(**a) for a in data.get("audit", []))
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
    def document_count(self) -> int:
        return len(self._document_ids)
