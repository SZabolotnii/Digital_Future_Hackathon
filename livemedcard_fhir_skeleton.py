#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LiveMedCard — мінімальний FHIR-вирівняний скелет даних для MVP.

Джерела рішень:
  • fhir-developer-skill (Anthropic healthcare) — cardinality, value sets, coding-system URLs.
    Беремо СТРУКТУРИ РЕСУРСІВ як схему даних; серверний шар (REST/OAuth/Bundle) НЕ беремо.
  • Архітектура TRACE — розподіл на два сховища:
      KV-T (факт-стор)  = джерело істини, живить L2a (тренди) і L1 (взаємодії/дози).
      Граф (контекст)   = зв'язки + провенанс, живить L2b (grounded Q&A) і метрику ETC.

Принцип: сигнали безпеки формуються ДЕТЕРМІНОВАНО на KV-T+L1, а не обходом графа через LLM.

Залежності: pydantic>=2, networkx
"""
from __future__ import annotations
from typing import Literal, Optional
from datetime import datetime, date
from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────────────
# Кодувальні системи (URL з fhir-developer-skill)
# ─────────────────────────────────────────────────────────────────────────────
class Sys:
    LOINC  = "http://loinc.org"
    SNOMED = "http://snomed.info/sct"
    RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
    ICD10  = "http://hl7.org/fhir/sid/icd-10"

# ─────────────────────────────────────────────────────────────────────────────
# FHIR datatypes (мінімальні)
# ─────────────────────────────────────────────────────────────────────────────
class Coding(BaseModel):
    system: str
    code: str
    display: Optional[str] = None

class CodeableConcept(BaseModel):
    coding: list[Coding] = Field(default_factory=list)
    text: Optional[str] = None

class Reference(BaseModel):
    reference: str                    # напр. "Patient/med-001"
    display: Optional[str] = None

class Quantity(BaseModel):
    value: float
    unit: Optional[str] = None
    system: Optional[str] = None
    code: Optional[str] = None

class Provenance(BaseModel):
    """Не-FHIR розширення: звідки взято факт. Основа метрики ETC."""
    source_doc_id: str                # id завантаженого документа-джерела
    extracted_by: str = "MedGemma-4B" # L2b-екстрактор
    extracted_at: datetime = Field(default_factory=datetime.utcnow)
    confidence: Optional[float] = None

# ─────────────────────────────────────────────────────────────────────────────
# FHIR resources — required-поля рівно за cardinality 1..* (fhir-developer-skill)
# ─────────────────────────────────────────────────────────────────────────────
class Patient(BaseModel):
    resourceType: Literal["Patient"] = "Patient"
    id: str
    gender: Optional[Literal["male", "female", "other", "unknown"]] = None
    birthDate: Optional[date] = None

class Encounter(BaseModel):
    resourceType: Literal["Encounter"] = "Encounter"
    id: str
    status: Literal["planned", "arrived", "triaged", "in-progress", "onleave",
                    "finished", "cancelled", "entered-in-error", "unknown"]   # 1..1
    class_: Coding = Field(alias="class")                                     # 1..1 (Coding, НЕ CodeableConcept)
    subject: Optional[Reference] = None                                        # 0..1
    period_start: Optional[date] = None
    model_config = {"populate_by_name": True}

class Observation(BaseModel):
    resourceType: Literal["Observation"] = "Observation"
    id: str
    status: Literal["registered", "preliminary", "final", "amended", "corrected",
                    "cancelled", "entered-in-error", "unknown"]              # 1..1
    code: CodeableConcept                                                     # 1..1
    subject: Optional[Reference] = None
    effectiveDateTime: Optional[datetime] = None
    valueQuantity: Optional[Quantity] = None
    provenance: Optional[Provenance] = None                                   # розширення

class Condition(BaseModel):
    resourceType: Literal["Condition"] = "Condition"
    id: str
    subject: Reference                                                        # 1..1 (єдине обов'язкове!)
    code: Optional[CodeableConcept] = None
    clinicalStatus: Optional[Literal["active", "recurrence", "relapse",
                    "inactive", "remission", "resolved"]] = None
    provenance: Optional[Provenance] = None

class MedicationRequest(BaseModel):
    resourceType: Literal["MedicationRequest"] = "MedicationRequest"
    id: str
    status: Literal["active", "on-hold", "cancelled", "completed",
                    "entered-in-error", "stopped", "draft", "unknown"]        # 1..1
    intent: Literal["proposal", "plan", "order", "original-order", "reflex-order",
                    "filler-order", "instance-order", "option"]               # 1..1
    medicationCodeableConcept: CodeableConcept                                # medication[x] 1..1
    subject: Reference                                                        # 1..1
    reason: Optional[Reference] = None      # → Condition, ребро indicated_for у графі
    provenance: Optional[Provenance] = None

# ─────────────────────────────────────────────────────────────────────────────
# KV-T проекція: Observation → атомарний факт (джерело істини для L2a/L1)
# ─────────────────────────────────────────────────────────────────────────────
class KVTFact(BaseModel):
    keyword: str          # людська назва показника
    loinc: Optional[str]  # код
    value: float
    unit: Optional[str]
    ts: datetime
    source_doc_id: Optional[str] = None

def observation_to_kvt(o: Observation) -> Optional[KVTFact]:
    if o.valueQuantity is None or o.effectiveDateTime is None:
        return None
    loinc = next((c.code for c in o.code.coding if c.system == Sys.LOINC), None)
    kw = o.code.text or (o.code.coding[0].display if o.code.coding else "obs")
    return KVTFact(keyword=kw, loinc=loinc, value=o.valueQuantity.value,
                   unit=o.valueQuantity.unit, ts=o.effectiveDateTime,
                   source_doc_id=o.provenance.source_doc_id if o.provenance else None)

# ─────────────────────────────────────────────────────────────────────────────
# L2a-сигнал: детермінована тренд-детекція на KV-T (НЕ LLM)
# ─────────────────────────────────────────────────────────────────────────────
def detect_monotonic_trend(facts: list[KVTFact], loinc: str, min_points: int = 3
                           ) -> Optional[dict]:
    series = sorted([f for f in facts if f.loinc == loinc], key=lambda f: f.ts)
    if len(series) < min_points:
        return None
    vals = [f.value for f in series]
    rising  = all(b > a for a, b in zip(vals, vals[1:]))
    falling = all(b < a for a, b in zip(vals, vals[1:]))
    if not (rising or falling):
        return None
    return {"loinc": loinc, "keyword": series[0].keyword,
            "direction": "rising" if rising else "falling",
            "points": len(series), "from": vals[0], "to": vals[-1],
            "span": f"{series[0].ts.date()}..{series[-1].ts.date()}"}

# ─────────────────────────────────────────────────────────────────────────────
# L1-чек: детермінована перевірка взаємодій по відкритій БД (заглушка)
# ─────────────────────────────────────────────────────────────────────────────
# У проді — відкрита база взаємодій (напр. RxNorm/NLM). Тут — демо-множина пар.
INTERACTION_DB: set[frozenset[str]] = {
    frozenset({"11289", "8640"}),   # варфарин (RxNorm 11289) × аспірин (8640) — демо
}

def check_interactions(meds: list[MedicationRequest]) -> list[dict]:
    rx = []
    for m in meds:
        code = next((c.code for c in m.medicationCodeableConcept.coding
                     if c.system == Sys.RXNORM), None)
        if code:
            rx.append((code, m))
    hits = []
    for i in range(len(rx)):
        for j in range(i + 1, len(rx)):
            if frozenset({rx[i][0], rx[j][0]}) in INTERACTION_DB:
                hits.append({"a": rx[i][1].medicationCodeableConcept.text,
                             "b": rx[j][1].medicationCodeableConcept.text,
                             "severity": "major"})
    return hits

# ─────────────────────────────────────────────────────────────────────────────
# Граф контексту (networkx) — зв'язки + провенанс, живить L2b/ETC
# ─────────────────────────────────────────────────────────────────────────────
def build_graph(patient: Patient, encounters, observations, conditions, meds):
    import networkx as nx
    g = nx.DiGraph()
    g.add_node(f"Patient/{patient.id}", type="Patient")
    for e in encounters:
        g.add_node(f"Encounter/{e.id}", type="Encounter", status=e.status)
        g.add_edge(f"Patient/{patient.id}", f"Encounter/{e.id}", rel="has_encounter")
    for o in observations:
        g.add_node(f"Observation/{o.id}", type="Observation")
        g.add_edge(f"Patient/{patient.id}", f"Observation/{o.id}", rel="has_observation")
        if o.provenance:
            g.add_edge(f"Observation/{o.id}", f"Document/{o.provenance.source_doc_id}",
                       rel="derived_from")   # ← провенанс для ETC
    for c in conditions:
        g.add_node(f"Condition/{c.id}", type="Condition")
        g.add_edge(f"Patient/{patient.id}", f"Condition/{c.id}", rel="has_condition")
    for m in meds:
        g.add_node(f"MedicationRequest/{m.id}", type="MedicationRequest")
        g.add_edge(f"Patient/{patient.id}", f"MedicationRequest/{m.id}", rel="prescribed")
        if m.reason:                        # каузальний зв'язок для L2b Q&A
            g.add_edge(f"MedicationRequest/{m.id}", m.reason.reference, rel="indicated_for")
    return g

# ─────────────────────────────────────────────────────────────────────────────
# ДЕМО
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    pat = Patient(id="mom-001", gender="female", birthDate=date(1950, 1, 1))

    def creat(oid, val, d, doc):
        return Observation(
            id=oid, status="final",
            code=CodeableConcept(coding=[Coding(system=Sys.LOINC, code="2160-0",
                                 display="Creatinine")], text="Креатинін"),
            subject=Reference(reference="Patient/mom-001"),
            effectiveDateTime=datetime(*d), valueQuantity=Quantity(value=val, unit="µmol/L"),
            provenance=Provenance(source_doc_id=doc, confidence=0.97))

    obs = [creat("o1", 88, (2023, 5, 1), "doc-A"),
           creat("o2", 104, (2023, 11, 3), "doc-B"),
           creat("o3", 121, (2024, 4, 9), "doc-C")]

    cond = Condition(id="c1", subject=Reference(reference="Patient/mom-001"),
                     code=CodeableConcept(text="Фібриляція передсердь"),
                     clinicalStatus="active")

    meds = [
        MedicationRequest(id="m1", status="active", intent="order",
            medicationCodeableConcept=CodeableConcept(
                coding=[Coding(system=Sys.RXNORM, code="11289")], text="Варфарин"),
            subject=Reference(reference="Patient/mom-001"),
            reason=Reference(reference="Condition/c1")),
        MedicationRequest(id="m2", status="active", intent="order",
            medicationCodeableConcept=CodeableConcept(
                coding=[Coding(system=Sys.RXNORM, code="8640")], text="Аспірин"),
            subject=Reference(reference="Patient/mom-001")),
    ]

    # KV-T проекція
    facts = [f for f in (observation_to_kvt(o) for o in obs) if f]

    print("=== KV-T факти ===")
    for f in facts:
        print(f"  {f.keyword} [{f.loinc}] = {f.value} {f.unit} @ {f.ts.date()} ← {f.source_doc_id}")

    print("\n=== L2a сигнал (тренд, детерміновано) ===")
    print(" ", detect_monotonic_trend(facts, loinc="2160-0"))

    print("\n=== L1 чек взаємодій (детерміновано по БД) ===")
    for h in check_interactions(meds):
        print(f"  ⚠ {h['a']} × {h['b']} — {h['severity']}")

    print("\n=== Граф контексту (вузли/ребра) ===")
    g = build_graph(pat, [], obs, [cond], meds)
    print(f"  вузлів: {g.number_of_nodes()}, ребер: {g.number_of_edges()}")
    # приклад L2b-запиту без LLM: чому призначено варфарин?
    for _, tgt, d in g.out_edges("MedicationRequest/m1", data=True):
        if d["rel"] == "indicated_for":
            print(f"  Q: чому Варфарин? → indicated_for → {tgt}")
    # ETC: провенанс кожної обзервації
    prov = [(u, v) for u, v, d in g.edges(data=True) if d["rel"] == "derived_from"]
    print(f"  провенанс-ребер (ETC): {len(prov)} → {prov}")
