"""Grounded Q&A: детермінований ретрив по FactStore + графу з посиланням на джерела.

LLM тут НЕ робить висновків про безпеку — відповіді будуються з фактів і провенансу.
Інтерфейс лишає місце під майбутню генеративну модель (L2b) для формулювання,
але фактаж і посилання завжди детерміновані.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .dto import Signal
from .lang import Lang, detect_lang
from .reference import range_for
from .retrieval import Embedder, chunk, rank
from .stores import FactStore, KnowledgeGraph


def _config_embed() -> Optional[Embedder]:
    """Embedder із конфігу для семантичного ретриву, або None (лексичний дефолт).

    Ліниві імпорти: коли `SEMANTIC_RETRIEVAL` вимкнено (CI/демо), llm_client не
    імпортується і мережа не чіпається.
    """
    from .config import SEMANTIC_RETRIEVAL

    if not SEMANTIC_RETRIEVAL:
        return None
    from . import llm_client
    from .config import EMBED_MODEL

    return lambda texts: llm_client.embeddings(texts, model=EMBED_MODEL)

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
    # EN-аліаси (специфічні — раніше за загальні, щоб «glycated hemoglobin»
    # не перехопився «hemoglobin»)
    "glycated hemoglobin": "4548-4",
    "a1c": "4548-4",
    "diabetes": "4548-4",
    "kidney": "2160-0",
    "creatinine": "2160-0",
    "hemoglobin": "718-7",
    "glucose": "2345-7",
    "sugar": "2345-7",
    "potassium": "2823-3",
    "blood pressure": "8480-6",
    "pressure": "8480-6",
    "cholesterol": "2093-3",
    "alt": "1742-6",
}

_MED_KEYWORDS = (
    "ліки", "препарат", "таблет", "взаємод", "приймає", "приймати",
    "medication", "medicine", "drug", "pill", "interaction", "taking",
)

# Каталог фраз відповіді за мовою питання (автовибір через detect_lang).
_MSG: dict[Lang, dict[str, str]] = {
    "uk": {
        "lab_none": "У картці немає вимірювань показника «{name}».",
        "lab_latest": "{name}: останнє значення {v} {u} ({d}).",
        "lab_trend": "Динаміка за {n} вимірювань: {a}→{b} ({d1}..{d2}), {dir}.",
        "dir_up": "стабільно зростає",
        "dir_down": "стабільно знижується",
        "dir_flat": "змінюється нерівномірно",
        "above": "Останнє значення ВИЩЕ норми ({lo}–{hi} {u}).",
        "within": "Останнє значення в межах норми ({lo}–{hi} {u}).",
        "below": "Останнє значення НИЖЧЕ норми ({lo}–{hi} {u}).",
        "meds_list": "Призначені препарати: {list}.",
        "meds_none": "У картці немає призначених препаратів.",
        "interactions": "⚠ Виявлені взаємодії:",
        "no_interactions": "Небезпечних взаємодій між препаратами не виявлено.",
        "freetext_header": "За текстом виписок:",
        "fallback": (
            "Не вдалося зіставити питання з наявними даними. Спробуйте спитати "
            "про показник (нирки/креатинін, HbA1c, калій, тиск) або про ліки та взаємодії."
        ),
    },
    "en": {
        "lab_none": "No measurements of «{name}» in the record.",
        "lab_latest": "{name}: latest value {v} {u} ({d}).",
        "lab_trend": "Trend over {n} measurements: {a}→{b} ({d1}..{d2}), {dir}.",
        "dir_up": "steadily increasing",
        "dir_down": "steadily decreasing",
        "dir_flat": "changing unevenly",
        "above": "Latest value is ABOVE the reference range ({lo}–{hi} {u}).",
        "within": "Latest value is within the reference range ({lo}–{hi} {u}).",
        "below": "Latest value is BELOW the reference range ({lo}–{hi} {u}).",
        "meds_list": "Prescribed medications: {list}.",
        "meds_none": "No prescribed medications in the record.",
        "interactions": "⚠ Detected interactions:",
        "no_interactions": "No dangerous drug interactions detected.",
        "freetext_header": "Per document text:",
        "fallback": (
            "Could not match the question to available data. Try asking about a lab "
            "value (kidney/creatinine, HbA1c, potassium, blood pressure) or about "
            "medications and interactions."
        ),
    },
}


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
        embed: Optional[Embedder] = None,
    ) -> None:
        self.factstore = factstore
        self.graph = graph
        self.signals = signals or []
        self.sex = sex
        # None → лексичний ретрив; інакше — переданий/конфігурований семантичний.
        self._embed = embed

    def ask(self, question: str) -> Answer:
        q = question.lower()
        lang = detect_lang(question)  # мова відповіді = мова питання

        # Наміри про ліки/взаємодії перевіряємо ПЕРШИМИ: інакше питання про ліки,
        # що містить лабораторну підстроку (напр. «...при тиску»), помилково
        # перехоплювалося б лабораторною гілкою.
        if any(k in q for k in _MED_KEYWORDS):
            return self._answer_medications(lang)

        loinc = self._match_loinc(q)
        if loinc is not None:
            return self._answer_lab(loinc, lang)

        # Fallback на free-text іпостась (Stage 1 summary у вузлах-документах):
        # структурного збігу немає, але відповідь може бути в тексті виписки.
        freetext = self._answer_freetext(q, lang)
        if freetext is not None:
            return freetext

        return Answer(text=_MSG[lang]["fallback"])

    # ── маршрутизація ─────────────────────────────────────────────────────────
    def _match_loinc(self, q: str) -> Optional[str]:
        for kw, loinc in _KEYWORD_TO_LOINC.items():
            if kw in q:
                return loinc
        return None

    def _answer_lab(self, loinc: str, lang: Lang) -> Answer:
        m = _MSG[lang]
        series = self.factstore.series(loinc)
        ref = range_for(loinc, self.sex)
        # Назва показника — з довідника мовою питання (той самий каталог, що й у
        # сигналів). Без цього EN-відповідь починалася українською назвою.
        if ref:
            name = (ref.keyword_en or ref.keyword) if lang == "en" else ref.keyword
        else:
            name = series[0].keyword if series else loinc
        if not series:
            return Answer(text=m["lab_none"].format(name=name))

        latest = series[-1]
        first = series[0]
        unit = latest.unit or (ref.unit if ref else "")
        parts = [m["lab_latest"].format(name=name, v=latest.value, u=unit, d=latest.ts.date())]
        if len(series) >= 2:
            # Напрям — за монотонністю всього ряду (узгоджено з L2a), а не лише
            # за крайніми точками, щоб не називати «трендом» немонотонну динаміку.
            vals = [f.value for f in series]
            if all(a <= b for a, b in zip(vals, vals[1:])) and vals[-1] > vals[0]:
                direction = m["dir_up"]
            elif all(a >= b for a, b in zip(vals, vals[1:])) and vals[-1] < vals[0]:
                direction = m["dir_down"]
            else:
                direction = m["dir_flat"]
            parts.append(m["lab_trend"].format(
                n=len(series), a=first.value, b=latest.value,
                d1=first.ts.date(), d2=latest.ts.date(), dir=direction,
            ))
        if ref is not None:
            key = "above" if latest.value > ref.high else "below" if latest.value < ref.low else "within"
            parts.append(m[key].format(lo=ref.low, hi=ref.high, u=unit))

        sources = _unique(f.source_doc_id for f in series if f.source_doc_id)
        return Answer(text=" ".join(parts), sources=sources)

    def _answer_freetext(self, q: str, lang: Lang) -> Optional[Answer]:
        """Ранжований ретрив по free-text іпостасі (Stage 1 summary / сирий текст).

        Розбиває тексти документів на пасажі (`retrieval.chunk`) і повертає
        найрелевантніші до питання з посиланням на документ-джерело. Дефолт —
        лексичне ранжування; семантичне — коли сконфігуровано embedder. None, якщо
        збігів немає (тоді — загальний fallback).
        """
        passages = []
        for nid in self.graph.nodes_of_type("Document"):
            text = self.graph.g.nodes[nid].get("text") or ""
            passages.extend(chunk(nid.split("/", 1)[-1], text))
        if not passages:
            return None
        embed = self._embed if self._embed is not None else _config_embed()
        hits = rank(q, passages, top_k=5, embed=embed)
        if not hits:
            return None
        text = _MSG[lang]["freetext_header"] + "\n" + "\n".join(
            f"— {h.text}" for h in hits
        )
        return Answer(text=text, sources=_unique(h.doc_id for h in hits))

    def _answer_medications(self, lang: Lang) -> Answer:
        m = _MSG[lang]
        med_nodes = self.graph.nodes_of_type("MedicationRequest")
        names = [self.graph.g.nodes[n].get("label", n) for n in med_nodes]
        parts = []
        if names:
            parts.append(m["meds_list"].format(list=", ".join(sorted(set(names)))))
        else:
            parts.append(m["meds_none"])

        interactions = [s for s in self.signals if s.kind == "interaction"]
        if interactions:
            parts.append(m["interactions"])
            parts.extend(f"— {s.detail} ({s.severity})" for s in interactions)
        else:
            parts.append(m["no_interactions"])

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
