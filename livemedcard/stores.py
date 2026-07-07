"""Два сховища над спільними FHIR-ресурсами.

* ``FactStore`` (KV-T) — джерело істини для кількісного; на ньому детерміновано
  рахуються сигнали L2a/L1.
* ``KnowledgeGraph`` — семантичні зв'язки та провенанс; живить grounded Q&A.

Межа: сигнали безпеки формуються на FactStore детерміновано, а не обходом графа
генеративною моделлю.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import networkx as nx
from pydantic import BaseModel

from .fhir import (
    CodeableConcept,
    Condition,
    Encounter,
    MedicationRequest,
    Observation,
    Patient,
    loinc_of,
)


class KVTFact(BaseModel):
    """Атомарний факт keyword–value–timestamp (проекція Observation)."""

    obs_id: Optional[str] = None
    keyword: str
    loinc: Optional[str]
    value: float
    unit: Optional[str]
    ts: datetime
    source_doc_id: Optional[str] = None


def _concept_label(cc: Optional[CodeableConcept], fallback: str) -> str:
    """Людська назва з CodeableConcept: text → перший display → fallback.

    Гарантує рядок навіть коли є лише коди без ``text`` (напр. з реального
    екстрактора), уникаючи None-міток вузлів графа.
    """
    if cc is not None:
        if cc.text:
            return cc.text
        for c in cc.coding:
            if c.display:
                return c.display
    return fallback


def observation_to_kvt(o: Observation) -> Optional[KVTFact]:
    """Проекція Observation → KVTFact (None, якщо немає значення/часу)."""
    if o.valueQuantity is None or o.effectiveDateTime is None:
        return None
    kw = o.code.text or (o.code.coding[0].display if o.code.coding else "obs")
    return KVTFact(
        obs_id=o.id,
        keyword=kw,
        loinc=loinc_of(o),
        value=o.valueQuantity.value,
        unit=o.valueQuantity.unit,
        ts=o.effectiveDateTime,
        source_doc_id=o.provenance.source_doc_id if o.provenance else None,
    )


class FactStore:
    """KV-T факт-стор — джерело істини для кількісних показників.

    Ідемпотентний за id обзервації: повторне додавання тієї самої обзервації
    (напр. повторний прогін документа) не дублює факт.
    """

    def __init__(self) -> None:
        self._facts: list[KVTFact] = []
        self._seen_ids: set[str] = set()
        # Індекс за LOINC: series()/loincs() без лінійного проходу всього стору.
        self._by_loinc: dict[str, list[KVTFact]] = {}

    def add_observation(self, o: Observation) -> Optional[KVTFact]:
        if o.id in self._seen_ids:
            return None
        fact = observation_to_kvt(o)
        if fact is not None:
            self._facts.append(fact)
            self._seen_ids.add(o.id)
            if fact.loinc:
                self._by_loinc.setdefault(fact.loinc, []).append(fact)
        return fact

    def add_observations(self, obs: list[Observation]) -> None:
        for o in obs:
            self.add_observation(o)

    def all_facts(self) -> list[KVTFact]:
        return list(self._facts)

    def series(self, loinc: str) -> list[KVTFact]:
        """Часовий ряд одного показника, відсортований за часом (свіжа копія)."""
        return sorted(self._by_loinc.get(loinc, []), key=lambda f: f.ts)

    def loincs(self) -> list[str]:
        """Унікальні LOINC-коди, наявні у сторі (у порядку першої появи)."""
        return list(self._by_loinc)


class KnowledgeGraph:
    """Граф контексту (networkx DiGraph) — зв'язки + провенанс."""

    def __init__(self) -> None:
        self.g = nx.DiGraph()

    def add_patient(self, p: Patient) -> str:
        nid = f"Patient/{p.id}"
        self.g.add_node(nid, type="Patient", gender=p.gender)
        return nid

    def add_encounter(self, e: Encounter, patient_id: str) -> str:
        nid = f"Encounter/{e.id}"
        self.g.add_node(nid, type="Encounter", status=e.status)
        self.g.add_edge(f"Patient/{patient_id}", nid, rel="has_encounter")
        return nid

    def add_observation(self, o: Observation, patient_id: str) -> str:
        nid = f"Observation/{o.id}"
        label = _concept_label(o.code, "obs")
        self.g.add_node(nid, type="Observation", label=label)
        self.g.add_edge(f"Patient/{patient_id}", nid, rel="has_observation")
        if o.provenance:
            self.g.add_edge(
                nid, f"Document/{o.provenance.source_doc_id}", rel="derived_from"
            )
        return nid

    def add_condition(self, c: Condition, patient_id: str) -> str:
        nid = f"Condition/{c.id}"
        label = _concept_label(c.code, "condition")
        self.g.add_node(nid, type="Condition", label=label)
        self.g.add_edge(f"Patient/{patient_id}", nid, rel="has_condition")
        if c.provenance:
            self.g.add_edge(
                nid, f"Document/{c.provenance.source_doc_id}", rel="derived_from"
            )
        return nid

    def add_medication(self, m: MedicationRequest, patient_id: str) -> str:
        nid = f"MedicationRequest/{m.id}"
        label = _concept_label(m.medicationCodeableConcept, "medication")
        self.g.add_node(nid, type="MedicationRequest", label=label, status=m.status)
        self.g.add_edge(f"Patient/{patient_id}", nid, rel="prescribed")
        if m.reason:
            self.g.add_edge(nid, m.reason.reference, rel="indicated_for")
        if m.provenance:
            self.g.add_edge(
                nid, f"Document/{m.provenance.source_doc_id}", rel="derived_from"
            )
        return nid

    # ── запити ────────────────────────────────────────────────────────────────
    def provenance_of(self, node_id: str) -> list[str]:
        """Документи-джерела, з яких виведено вузол."""
        return [
            v
            for _, v, d in self.g.out_edges(node_id, data=True)
            if d.get("rel") == "derived_from"
        ]

    def indication_of(self, medication_node_id: str) -> Optional[str]:
        """Стан (Condition), заради якого призначено препарат."""
        for _, v, d in self.g.out_edges(medication_node_id, data=True):
            if d.get("rel") == "indicated_for":
                return v
        return None

    def nodes_of_type(self, node_type: str) -> list[str]:
        return [n for n, d in self.g.nodes(data=True) if d.get("type") == node_type]
