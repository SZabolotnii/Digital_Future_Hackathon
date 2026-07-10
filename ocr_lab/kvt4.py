"""KVT4 — схема, normalizer і детермінований keyword→LOINC міст для OCR Lab.

Дві форми KVT4 (за Spec.md §4.2):
  1. raw model output  — те, що NuExtract повертає за шаблоном;
  2. normalized fact   — після normalizer: розбите value/unit, доданий
     source_document_id / confidence / unreadable, змапований LOINC.

Джерело істини для мосту keyword→LOINC — `livemedcard/layers/l2b_stage2_lora.py`
(_KEYWORD_LOINC / _CGM_LOINC / _resolve_loinc / _split_value / _parse_timestamp).
Тут — самодостатня дзеркальна копія, щоб коди на cloud- і local-шляхах збігались
(Spec.md FR-MODEL-004 / FR-KVT-003). Оновлюєш там — синхронізуй тут.
"""
from __future__ import annotations

import re
from typing import Optional

CLUSTERS = ("lab", "vital", "medication", "condition", "other")

# ── keyword → LOINC (дзеркало l2b_stage2_lora._KEYWORD_LOINC) ─────────────────
_KEYWORD_LOINC: dict[str, tuple[str, str]] = {
    "creatinine": ("2160-0", "Creatinine"),
    "креатинін": ("2160-0", "Creatinine"),
    "glucose": ("2345-7", "Glucose"),
    "глюкоза": ("2345-7", "Glucose"),
    "potassium": ("2823-3", "Potassium"),
    "калій": ("2823-3", "Potassium"),
    "sodium": ("2951-2", "Sodium"),
    "натрій": ("2951-2", "Sodium"),
    "hemoglobin": ("718-7", "Hemoglobin"),
    "гемоглобін": ("718-7", "Hemoglobin"),
    "hba1c": ("4548-4", "Hemoglobin A1c"),
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

# CGM/AGP — substring-матчинг (лише headline-коди, в яких упевнені).
_CGM_LOINC: list[tuple[tuple[str, ...], str, str]] = [
    (("glucose management indicator", "gmi"), "97506-0", "Glucose Management Indicator"),
    (("average glucose", "mean glucose"), "97507-8", "Mean glucose (CGM)"),
]

_VALUE_NUM_RE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*(.*)$")


_TRAIL_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def resolve_loinc(keyword: str) -> Optional[tuple[str, str]]:
    """LOINC для keyword. Консервативно, щоб НЕ мапити хибно (Spec.md §10):
    точний збіг → зняти кінцеве уточнення в дужках і точний збіг → CGM substring →
    None (free-form, без сигналу). Свідомо НЕ робимо fuzzy substring по лабах —
    «глікований гемоглобін» не має колапсувати у «гемоглобін»."""
    low = keyword.lower().strip()
    exact = _KEYWORD_LOINC.get(low)
    if exact is not None:
        return exact
    stripped = _TRAIL_PAREN_RE.sub("", low).strip()  # «калій (k*)» → «калій»
    if stripped != low:
        exact = _KEYWORD_LOINC.get(stripped)
        if exact is not None:
            return exact
    for toks, code, display in _CGM_LOINC:
        if any(t in low for t in toks):
            return code, display
    return None


def split_value(value: object) -> tuple[Optional[float], Optional[str]]:
    """'135 мкмоль/л' → (135.0, 'мкмоль/л'); '7.6 %' → (7.6, '%'); 'Так' → (None, None)."""
    if value is None:
        return None, None
    m = _VALUE_NUM_RE.match(str(value))
    if m is None:
        return None, None
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2).strip() or None
    return num, unit


def _norm_cluster(c: object) -> str:
    s = str(c or "").lower().strip()
    return s if s in CLUSTERS else "other"


def normalize(raw_facts: list[dict], source_document_id: str) -> list[dict]:
    """raw model output → normalized facts (Spec.md §4.2).

    Нічого не вигадуємо: value, що не парситься в число → unreadable=true, value=null,
    але keyword/timestamp зберігаються. LOINC додаємо лише за підтвердженим мостом.
    """
    out: list[dict] = []
    for f in raw_facts:
        if not isinstance(f, dict):
            continue
        keyword = str(f.get("keyword", "")).strip()
        if not keyword:
            continue
        raw_val = f.get("value")
        num, parsed_unit = split_value(raw_val)
        unit = f.get("unit") or parsed_unit
        unreadable = num is None
        bridge = resolve_loinc(keyword)
        out.append({
            "cluster": _norm_cluster(f.get("cluster")),
            "keyword": keyword,
            "value": num,
            "unit": unit,
            "timestamp": f.get("timestamp") or None,
            "unreadable": unreadable,
            "source_span": f.get("source_span"),
            "source_document_id": source_document_id,
            "loinc": bridge[0] if bridge else None,
            "loinc_display": bridge[1] if bridge else None,
            "confidence": None,
        })
    return out


# NuExtract-шаблон KVT4 (raw). Leaf-типи — типи NuExtract; cluster — enum.
FACTS_TEMPLATE = {
    "facts": [
        {
            "cluster": list(CLUSTERS),
            "keyword": "verbatim-string",
            "value": "verbatim-string",
            "unit": "verbatim-string",
            "timestamp": "date",
        }
    ]
}
