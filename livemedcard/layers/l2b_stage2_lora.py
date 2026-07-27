"""L2b Stage 2 (LoRA) — fine-tuned MedGemma генерує KVT4 факти.

Альтернатива детермінованому `deterministic_extract`: замість regex+vocab.json
тут працює FT-модель DocUA/medgemma-1.5-4b-it-...-two-stage (база+LoRA-адаптер),
натренована видавати KVT4 — `{cluster, keyword, value, timestamp}`. Знімає
обмеження словника, узагальнює на будь-який документ.

Межа безпеки збережена: LoRA лише **витягує факти**; сигнали L1/L2a рахуються
детерміновано на FactStore. Часті лабораторні keywords мостяться у LOINC (щоб
потрапляли в таймлайн і перевірки), решта лишаються free-form у summary.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Optional

from ..config import STAGE2_MODEL, STAGE2_URL
from ..dto import Document
from ..fhir import (
    CodeableConcept,
    Coding,
    Observation,
    Provenance,
    Quantity,
    Reference,
    Sys,
)
from ..llm_client import chat_completions
from ..versioning import fingerprint

# ── keyword → LOINC міст (часті лаби readmission-домену) ──────────────────────
# Значення: (LOINC, людська назва). Одиниця береться з value LoRA (fallback — тут).
_KEYWORD_LOINC: dict[str, tuple[str, str]] = {
    # EN (readmission-домен FT) + UA — білінгвальний міст
    "creatinine": ("2160-0", "Creatinine"),
    "serum creatinine": ("2160-0", "Creatinine"),
    "креатинін": ("2160-0", "Creatinine"),
    "glucose": ("2345-7", "Glucose"),
    "fasting glucose": ("2345-7", "Glucose"),
    "blood glucose": ("2345-7", "Glucose"),
    "глюкоза": ("2345-7", "Glucose"),
    "глюкоза натще": ("2345-7", "Glucose"),
    "potassium": ("2823-3", "Potassium"),
    "калій": ("2823-3", "Potassium"),
    "sodium": ("2951-2", "Sodium"),
    "натрій": ("2951-2", "Sodium"),
    "hemoglobin": ("718-7", "Hemoglobin"),
    "гемоглобін": ("718-7", "Hemoglobin"),
    "hba1c": ("4548-4", "Hemoglobin A1c"),
    "hemoglobin a1c": ("4548-4", "Hemoglobin A1c"),
    "glycated hemoglobin": ("4548-4", "Hemoglobin A1c"),
    "глікований гемоглобін": ("4548-4", "Hemoglobin A1c"),
    "hematocrit": ("4544-3", "Hematocrit"),
    "гематокрит": ("4544-3", "Hematocrit"),
    "wbc": ("6690-2", "Leukocytes"),
    "leukocytes": ("6690-2", "Leukocytes"),
    "лейкоцити": ("6690-2", "Leukocytes"),
    "platelet": ("777-3", "Platelets"),
    "тромбоцити": ("777-3", "Platelets"),
    "bun": ("3094-0", "Urea nitrogen"),
    "сечовина": ("3094-0", "Urea nitrogen"),
    "bicarbonate": ("1963-8", "Bicarbonate"),
    "бікарбонат": ("1963-8", "Bicarbonate"),
}

# CGM/AGP-метрики → стандартні LOINC CGM-панелі. Substring-матчинг, бо NuExtract
# дає варіативні формулювання («Glucose Management Indicator (GMI)», «CGM Average
# Glucose»). Даємо ЛИШЕ headline-коди, в яких упевнені; TIR-біни / variability /
# active-time свідомо НЕ мапимо (коди треба звірити з авторитетним LOINC —
# фабрикувати клінічні коди небезпечно). Порядок — специфічніше раніше.
# ⚠ Перед клінічним використанням підтвердити ці коди за офіційним LOINC.
_CGM_LOINC: list[tuple[tuple[str, ...], str, str]] = [
    (("glucose management indicator", "gmi"), "97506-0", "Glucose Management Indicator"),
    (("average glucose", "mean glucose"), "97507-8", "Mean glucose (CGM)"),
]


_TRAIL_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _resolve_loinc(keyword: str) -> tuple[str, str] | None:
    """LOINC для keyword: точний збіг → зняти кінцеве уточнення в дужках і точний
    збіг (напр. «калій (K+)» → «калій») → CGM substring → None (free-form).

    Свідомо БЕЗ fuzzy-substring по лабах, щоб «глікований гемоглобін» не колапсував
    у «гемоглобін». Той самий resolver дзеркалить cloud-шлях (kvt4.resolve_loinc)."""
    low = keyword.lower().strip()
    exact = _KEYWORD_LOINC.get(low)
    if exact is not None:
        return exact
    stripped = _TRAIL_PAREN_RE.sub("", low).strip()
    if stripped != low:
        exact = _KEYWORD_LOINC.get(stripped)
        if exact is not None:
            return exact
    for toks, code, display in _CGM_LOINC:
        if any(t in low for t in toks):
            return code, display
    return None

_THOUGHT_TOKEN_RE = re.compile(r"<unused\d+>", re.IGNORECASE)
_VALUE_NUM_RE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*(.*)$")
# Дата або ідентифікатор: два розділювачі поспіль («2024-10-15», «06/13/2025»).
_DATE_LIKE_RE = re.compile(r"^\s*\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4}")
_TS_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y")


def _stable_id(*parts: object) -> str:
    key = "|".join(str(p) for p in parts)
    return f"obs-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"


def build_stage2_system_prompt() -> str:
    return (
        "You extract clinical facts from a structured Stage-1 summary. "
        "Output ONLY a JSON array of facts, each an object with keys "
        '"cluster", "keyword", "value", "timestamp". '
        "Use the patient's own measured values (numbers with units where present). "
        "If a date/period is stated for a fact, put it in \"timestamp\", else use "
        '"not stated". Do NOT invent facts. Output JSON only, no prose.'
    )


def _extract_json_array(text: str) -> list[dict]:
    """Витягти JSON-масив фактів із відповіді моделі (стрип thought-токенів)."""
    s = _THOUGHT_TOKEN_RE.sub("", text).strip()
    if "```json" in s:
        s = s.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in s:
        s = s.split("```", 1)[1].split("```", 1)[0].strip()
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict)]


def _split_value(value: object) -> tuple[Optional[float], Optional[str]]:
    """'204 mg/dL' → (204.0, 'mg/dL'); '34%' → (34.0, '%'); 'Yes' → (None, None).

    Дати й ідентифікатори — НЕ вимірювання: «2024-10-15» інакше стало б фактом
    «2024.0 -10-15», а «2024-10-15/0412» — номером бланка в таймлайні.
    """
    s = str(value)
    if _DATE_LIKE_RE.match(s):
        return None, None
    m = _VALUE_NUM_RE.match(s)
    if m is None:
        return None, None
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2).strip() or None
    return num, unit


def _parse_timestamp(
    ts: object, fallback: Optional[datetime]
) -> tuple[Optional[datetime], bool]:
    """Дата факту з різних форматів; для періоду «A - B» береться КІНЕЦЬ (B).

    Другий елемент — чи дата справді прочитана з документа. False → повернуто
    fallback (дата завантаження), і це треба показати, а не видати за виміряну.
    """
    s = "" if ts is None else str(ts).strip()
    if not s or s.lower() in ("not stated", "none", "null"):
        return fallback, False
    for sep in (" - ", " – ", " — ", " to "):
        if sep in s:
            s = s.split(sep)[-1].strip()
            break
    for fmt in _TS_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc), True
        except ValueError:
            continue
    return fallback, False


def prompt_id() -> str:
    """Відбиток системного промпту Stage 2 — іде в провенанс фактів."""
    return fingerprint(build_stage2_system_prompt())


def extract_facts(summary_text: str, *, url: str | None = None, model: str | None = None) -> list[dict]:
    """Викликати LoRA-Stage 2 сервер і повернути сирі KVT4-обʼєкти."""
    messages = [
        {"role": "system", "content": build_stage2_system_prompt()},
        {"role": "user", "content": summary_text},
    ]
    raw = chat_completions(
        messages,
        model=model or STAGE2_MODEL,
        base_url=url or STAGE2_URL,
        temperature=0.0,
        max_tokens=768,
        frequency_penalty=0.1,  # ≈ repetition-penalty 1.1 з README
        service_name="LoRA Stage 2",
    )
    return _extract_json_array(raw)


# Keywords, під якими модель віддає дату ЗАБОРУ матеріалу — тобто дату, на яку
# виміряні значення документа. Свідомо вузько: «reported»/«issued» — це коли
# лабораторія видала бланк, а не коли взяли кров, і датувати ним показник хибно.
_DOC_DATE_KEYWORDS = frozenset({
    "collected", "collection date", "date collected", "specimen collected",
    "дата забору", "забір матеріалу", "дата взяття",
})


def _document_date(kvt: list[dict]) -> Optional[datetime]:
    """Дата документа з ЙОГО Ж витягу (напр. «Collected: 2024-10-15»).

    Табличні бланки не повторюють дату в кожному рядку — вона стоїть у шапці, і
    модель віддає її окремим фактом. Тоді значення документа датуються нею: це
    дата, ЗАЯВЛЕНА в документі, а не здогад (на відміну від дати завантаження).
    """
    for item in kvt:
        if str(item.get("keyword", "")).strip().lower() in _DOC_DATE_KEYWORDS:
            ts, ok = _parse_timestamp(item.get("value"), None)
            if ok:
                return ts
    return None


def to_observations(
    kvt: list[dict], doc: Document, patient_id: str, *, extracted_by: str,
    prompt_id: str | None = None,
) -> list[Observation]:
    """KVT4 → FHIR Observations (лише числові; часті лаби мостяться у LOINC)."""
    subject = Reference(reference=f"Patient/{patient_id}")
    doc_date = _document_date(kvt)
    observations: list[Observation] = []
    seen: set[str] = set()
    for item in kvt:
        keyword = str(item.get("keyword", "")).strip()
        if not keyword:
            continue
        num, unit = _split_value(item.get("value"))
        if num is None:  # нечислові (Yes/No, назви) лишаються лише в summary
            continue
        # KVT4 з хмарного Lab несе одиницю ОКРЕМИМ полем ({"value":"135","unit":"µmol/L"}),
        # тоді як локальний шлях вкладає її в рядок значення ("135 мкмоль/л"). Без цього
        # рядка кожен факт із фото приходив без одиниці — і псував FHIR-експорт.
        if unit is None:
            raw_unit = str(item.get("unit") or "").strip()
            unit = raw_unit or None
        eff, date_stated = _parse_timestamp(item.get("timestamp"), None)
        if not date_stated:
            # дата на факт відсутня → дата документа; лише потім — дата завантаження
            eff = doc_date or doc.received_at
            date_stated = doc_date is not None
        bridge = _resolve_loinc(keyword)
        coding = (
            [Coding(system=Sys.LOINC, code=bridge[0], display=bridge[1])]
            if bridge else []
        )
        obs_id = _stable_id(doc.id, keyword.lower(), num, eff)
        if obs_id in seen:
            continue
        seen.add(obs_id)
        observations.append(
            Observation(
                id=obs_id,
                status="final",
                code=CodeableConcept(coding=coding, text=keyword),
                subject=subject,
                effectiveDateTime=eff,
                valueQuantity=Quantity(value=num, unit=unit),
                provenance=Provenance(
                    source_doc_id=doc.id,
                    extracted_by=extracted_by,
                    prompt_id=prompt_id,
                    confidence=None,
                    date_stated=date_stated,
                ),
            )
        )
    return observations
