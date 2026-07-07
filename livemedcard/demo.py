"""E2E демонстратор LiveMedCard у терміналі.

Прогін: демо-документи → витяг → таймлайн → детерміновані сигнали → рішення L3 →
grounded Q&A з посиланням на джерела.

Запуск: ``uv run python -m livemedcard.demo``
"""
from __future__ import annotations

from datetime import date

from .dto import Document, TraceReport
from .fhir import Patient
from .pipeline import LiveMedCard
from .qa import QA
from .reference import load_sample_docs


def _print_report(r: TraceReport) -> None:
    e = r.extraction
    print(f"\n=== Документ {r.document_id} ===")
    print(
        f"  Витяг (L2b): observations={len(e.observations)} "
        f"conditions={len(e.conditions)} medications={len(e.medications)} "
        f"впевненість={e.mean_confidence:.2f}"
    )
    if r.signals:
        print("  Сигнали (L1/L2a, детерміновано):")
        for s in r.signals:
            print(f"    [{s.layer}/{s.kind}/{s.severity}] {s.detail}")
    else:
        print("  Сигналів немає.")
    d = r.decision
    mark = "⛳ ЕСКАЛАЦІЯ ДО ЛЮДИНИ (L4)" if d.escalate else "auto"
    print(f"  Рішення (L3): {d.route_to} — {mark}")
    for reason in d.reasons:
        print(f"    · {reason}")


def main() -> None:
    patient = Patient(id="mom-001", gender="female", birthDate=date(1950, 1, 1))
    card = LiveMedCard(patient)

    docs = [Document(**d) for d in load_sample_docs()]
    print("LiveMedCard — E2E демо (TRACE)")
    print(f"Пацієнт: {patient.id}, документів: {len(docs)}")

    reports = card.ingest_all(docs)
    for r in reports:
        _print_report(r)

    print("\n=== Grounded Q&A (з посиланням на документи-джерела) ===")
    qa = QA(card.factstore, card.graph, card.current_signals(), sex=patient.gender)
    for question in (
        "Що в мами з нирками за останній рік?",
        "Який був калій?",
        "Які ліки приймає і чи є взаємодії?",
    ):
        ans = qa.ask(question)
        print(f"\nQ: {question}")
        print(f"A: {ans.text}")
        if ans.sources:
            print(f"   Джерела: {', '.join(ans.sources)}")


if __name__ == "__main__":
    main()
