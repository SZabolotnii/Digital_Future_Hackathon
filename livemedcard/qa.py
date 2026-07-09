"""Grounded Q&A: детермінований ретрив по FactStore + графу з посиланням на джерела.

LLM тут НЕ робить висновків про безпеку — відповіді будуються з фактів і провенансу.
Інтерфейс лишає місце під майбутню генеративну модель (L2b) для формулювання,
але фактаж і посилання завжди детерміновані.
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

from .dto import Signal
from .reference import range_for
from .stores import FactStore, KnowledgeGraph

# Ключові слова питання → LOINC показника. Кожен термін мапиться на СВІЙ аналіт:
# «глікований»/«hba1c» → HbA1c; «гемоглобін» → звичайний Hb; «цукор»/«глюкоза» →
# глюкоза. Порядок важливий: специфічні терміни («глікован») перед загальними
# («гемоглобін»), бо перший збіг виграє.
_KEYWORD_TO_LOINC: dict[str, str] = {
    "нирк": "2160-0",
    "креатинін": "2160-0",
    "hba1c": "4548-4",
    "глікован": "4548-4",
    "діабет": "4548-4",
    "гемоглобін": "718-7",
    "цукор": "2345-7",
    "глюкоз": "2345-7",
    "калій": "2823-3",
    "тиск": "8480-6",
    "холестерин": "2093-3",
    "алт": "1742-6",
}

_MED_KEYWORDS = ("ліки", "препарат", "таблет", "взаємод", "приймає", "приймати")


class Answer(BaseModel):
    text: str
    sources: list[str] = Field(default_factory=list)  # id документів-джерел


class QA:
    def __init__(
        self,
        factstore: FactStore,
        graph: KnowledgeGraph,
        signals: Optional[list[Signal]] = None,
        sex: Optional[str] = None,
    ) -> None:
        self.factstore = factstore
        self.graph = graph
        self.signals = signals or []
        self.sex = sex

    def ask(self, question: str) -> Answer:
        q = question.lower()

        # Наміри про ліки/взаємодії перевіряємо ПЕРШИМИ: інакше питання про ліки,
        # що містить лабораторну підстроку (напр. «...при тиску»), помилково
        # перехоплювалося б лабораторною гілкою.
        if any(k in q for k in _MED_KEYWORDS):
            return self._answer_medications()

        loinc = self._match_loinc(q)
        if loinc is not None:
            return self._answer_lab(loinc)

        # Fallback на free-text іпостась (Stage 1 summary у вузлах-документах):
        # структурного збігу немає, але відповідь може бути в тексті виписки.
        freetext = self._answer_freetext(q)
        if freetext is not None:
            return freetext

        return Answer(
            text="Не вдалося зіставити питання з наявними даними. "
            "Спробуйте спитати про показник (нирки/креатинін, HbA1c, калій, тиск) "
            "або про ліки та взаємодії."
        )

    # ── маршрутизація ─────────────────────────────────────────────────────────
    def _match_loinc(self, q: str) -> Optional[str]:
        for kw, loinc in _KEYWORD_TO_LOINC.items():
            if kw in q:
                return loinc
        return None

    def _answer_lab(self, loinc: str) -> Answer:
        series = self.factstore.series(loinc)
        ref = range_for(loinc, self.sex)
        name = ref.keyword if ref else (series[0].keyword if series else loinc)
        if not series:
            return Answer(text=f"У картці немає вимірювань показника «{name}».")

        latest = series[-1]
        first = series[0]
        unit = latest.unit or (ref.unit if ref else "")
        parts = [
            f"{name}: останнє значення {latest.value} {unit} "
            f"({latest.ts.date()})."
        ]
        if len(series) >= 2:
            # Напрям — за монотонністю всього ряду (узгоджено з L2a), а не лише
            # за крайніми точками, щоб не називати «трендом» немонотонну динаміку.
            vals = [f.value for f in series]
            if all(a <= b for a, b in zip(vals, vals[1:])) and vals[-1] > vals[0]:
                direction = "стабільно зростає"
            elif all(a >= b for a, b in zip(vals, vals[1:])) and vals[-1] < vals[0]:
                direction = "стабільно знижується"
            else:
                direction = "змінюється нерівномірно"
            parts.append(
                f"Динаміка за {len(series)} вимірювань: {first.value}→{latest.value} "
                f"({first.ts.date()}..{latest.ts.date()}), {direction}."
            )
        if ref is not None:
            if latest.value > ref.high:
                parts.append(f"Останнє значення ВИЩЕ норми ({ref.low}–{ref.high} {unit}).")
            elif latest.value < ref.low:
                parts.append(f"Останнє значення НИЖЧЕ норми ({ref.low}–{ref.high} {unit}).")
            else:
                parts.append(f"Останнє значення в межах норми ({ref.low}–{ref.high} {unit}).")

        sources = _unique(f.source_doc_id for f in series if f.source_doc_id)
        return Answer(text=" ".join(parts), sources=sources)

    def _answer_freetext(self, q: str) -> Optional[Answer]:
        """Пошук у Stage 1 summary (free-text іпостась) за токенами питання.

        Повертає рядки summary, що містять значущі слова питання, з посиланням на
        документ-джерело. None, якщо збігів немає (тоді — загальний fallback).
        """
        tokens = [w for w in re.findall(r"\w+", q.lower()) if len(w) >= 4]
        if not tokens:
            return None
        hits: list[str] = []
        sources: list[str] = []
        for nid in self.graph.nodes_of_type("Document"):
            text = self.graph.g.nodes[nid].get("text") or ""
            doc_id = nid.split("/", 1)[-1]
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("##") or stripped == "not stated":
                    continue
                low = stripped.lower()
                if any(t in low for t in tokens):
                    hits.append(stripped)
                    sources.append(doc_id)
                    if len(hits) >= 5:  # не заливати відповідь
                        break
            if len(hits) >= 5:
                break
        if not hits:
            return None
        text = "За текстом виписок:\n" + "\n".join(f"— {h}" for h in hits)
        return Answer(text=text, sources=_unique(sources))

    def _answer_medications(self) -> Answer:
        med_nodes = self.graph.nodes_of_type("MedicationRequest")
        names = [self.graph.g.nodes[n].get("label", n) for n in med_nodes]
        parts = []
        if names:
            parts.append("Призначені препарати: " + ", ".join(sorted(set(names))) + ".")
        else:
            parts.append("У картці немає призначених препаратів.")

        interactions = [s for s in self.signals if s.kind == "interaction"]
        if interactions:
            parts.append("⚠ Виявлені взаємодії:")
            parts.extend(f"— {s.detail} ({s.severity})" for s in interactions)
        else:
            parts.append("Небезпечних взаємодій між препаратами не виявлено.")

        sources = _unique(
            self.graph.provenance_of(n)[0].split("/", 1)[-1]
            for n in med_nodes
            if self.graph.provenance_of(n)
        )
        return Answer(text="\n".join(parts), sources=sources)


def _unique(items) -> list[str]:
    seen: dict[str, None] = {}
    for it in items:
        if it:
            seen.setdefault(it, None)
    return list(seen)
