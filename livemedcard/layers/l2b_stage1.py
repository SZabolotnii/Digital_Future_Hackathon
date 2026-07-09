"""L2b Stage 1 — SGR-стиснення вільного тексту в стабільний summary.

Локальна MedGemma читає сирий текст (OCR або введений) і повертає стислий,
формат-стабільний SGR-summary з 9 доменними ключами. Це **free-text іпостась**:
згорнута, клінічно структурована, але ще не канонізована. Детермінований Stage 2
(`l2b_extractor.deterministic_extract`) далі витягує з неї KV-T факти.

Метод і промпт-контракт — за статтею автора «LLM StructCore» (Schema-Guided
Reasoning Condensation): LLM лише стискає (стохастика впливає на recall), а
формат/словник гарантує детермінований Stage 2. Адаптовано під лонгітюдну
українську медкартку: без фіксованого CRF, зі збереженням сирих значень і
**дат на факт**.
"""
from __future__ import annotations

import json
import re
from typing import Iterable

from ..config import STAGE1_MODEL
from ..llm_client import chat_completions

STAGE1_KEYS_9: list[str] = [
    "DEMOGRAPHICS",
    "VITALS",
    "LABS",
    "PROBLEMS",
    "SYMPTOMS",
    "MEDICATIONS",
    "PROCEDURES",
    "UTILIZATION",
    "DISPOSITION",
]


def build_stage1_system_prompt() -> str:
    keys_str = ", ".join(f'"{k}"' for k in STAGE1_KEYS_9)
    return f"""You are an expert clinical information compression engine for a
longitudinal medical card. Convert ONE clinical document (in any language,
often Ukrainian) into a compact, reliable intermediate summary.
This is Stage 1 only — a downstream deterministic parser will extract structured facts.

OUTPUT CONTRACT (STRICT):
1) Output MUST be a single valid JSON object. The FIRST character MUST be "{{".
2) Output JSON only: no markdown, no code fences, no extra text.
3) Do NOT output analysis, chain-of-thought, or "thinking".
4) Use EXACTLY these keys (no extra, no missing):
   {keys_str}

VALUE FORMAT (STRICT):
- Each value MUST be a string (never a nested object or array).
- If a cluster has no supported facts: use exactly "not stated".
- For multiple facts use "\\n" (two characters) between short "Key: Value" lines.

CONTENT RULES:
- Keep the document's original language and the EXACT raw values (numbers, units,
  test names). Do NOT translate values, do NOT round, do NOT infer from absence.
- Include a fact ONLY if it is explicitly stated. If unsure, omit the line.
- MEASURED VALUES, NOT SCAFFOLDING: capture the patient's own measured/reported
  values. Do NOT record reference ranges, target thresholds, or chart axis ticks
  (e.g. "Target Range 70-180 mg/dL", "Above 250 mg/dL", axis labels like
  "250 / 180 / 70") as facts — they are scale, not measurements.
- DATES PER FACT: if a measurement/prescription date is stated in the text, append
  it in parentheses at the END of that line in ISO form: "(YYYY-MM-DD)".
  Example: "креатинін: 120 (2024-03-15)". If no date is stated, omit the parentheses.

ANTI-DRIFT (MUST FOLLOW):
- NEVER repeat the same line. Deduplicate. Max ~12 lines and ~700 chars per field.
- If you start repeating, STOP and output the JSON object immediately.

CLUSTER GUIDANCE (9 clusters):
- DEMOGRAPHICS: patient context, past medical history, social/baseline.
- VITALS: BP/HR/RR/Temp/SpO2 and severity cues (raw values).
- LABS: lab/biomarker values (raw numbers + units).
- PROBLEMS: diagnoses and clinical problems (acute/chronic).
- SYMPTOMS: presenting symptoms and course.
- MEDICATIONS: medications with dose/frequency exactly as written.
- PROCEDURES: imaging/tests/procedures performed and findings.
- UTILIZATION: ED events, consults, monitoring, escalation.
- DISPOSITION: discharge outcome/follow-up if stated (else "not stated").

FORMAT SKELETON (structure only; do NOT copy these placeholder values):
{{
  "DEMOGRAPHICS": "not stated",
  "VITALS": "not stated",
  "LABS": "not stated",
  "PROBLEMS": "not stated",
  "SYMPTOMS": "not stated",
  "MEDICATIONS": "not stated",
  "PROCEDURES": "not stated",
  "UTILIZATION": "not stated",
  "DISPOSITION": "not stated"
}}
"""


def build_stage1_user_prompt(note_text: str) -> str:
    return f"Clinical document:\n{note_text}\n\nReturn the Stage 1 JSON now:"


# ── парсинг відповіді моделі ──────────────────────────────────────────────────
def _strip_thinking_wrappers(text: str) -> str:
    # Gemma-family інколи емітить <unusedNN>thought…-обгортки навколо міркувань.
    s = text.strip()
    s = re.sub(r"<unused\d+>thought.*?(?:<unused\d+>|$)", "", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<unused\d+>", "", s, flags=re.IGNORECASE)
    return s.strip()


def extract_json_object(text: str) -> dict:
    """Витягти JSON-обʼєкт із відповіді моделі, з ремонтом обрізаного хвоста.

    Якщо модель урвала вивід перед фінальною "}" (типово через повтори),
    відкочуємось до найдовшого префікса, що парситься як валідний JSON.
    """
    s = _strip_thinking_wrappers(text)
    if "```json" in s:
        s = s.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in s:
        s = s.split("```", 1)[1].split("```", 1)[0].strip()

    if "{" not in s:
        raise ValueError("No JSON object start '{' found.")
    start = s.find("{")
    end = s.rfind("}")
    if end > start:
        return json.loads(s[start : end + 1])

    cand = s[start:]
    max_backtrack = max(0, len(cand) - 20000)  # обмежити роботу на дуже довгих
    for cut in range(len(cand), max_backtrack, -1):
        chunk = cand[:cut].strip()
        if not chunk:
            continue
        chunk2 = chunk if chunk.endswith("}") else chunk + "}"
        try:
            obj = json.loads(chunk2)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    raise ValueError("No JSON object end '}' found.")


def coerce_stage1_summary(obj: dict) -> dict[str, str]:
    """Зробити Stage 1 вивід безпечним і стабільним: усі 9 ключів, лише рядки.

    Відсутні ключі → "not stated"; зайві відкидаються; вкладені dict/list
    сплощуються у "Key: Value" рядки (контракт «лише рядки»).
    """
    def _flatten(v: object) -> str:
        if v is None:
            return "not stated"
        if isinstance(v, str):
            s = v.strip()
            return s or "not stated"
        if isinstance(v, dict):
            lines: list[str] = []
            for kk, vv in v.items():
                kks = str(kk).strip()
                if not kks or vv is None:
                    continue
                if isinstance(vv, (dict, list)):
                    inner = ", ".join(
                        str(x).strip()
                        for x in (vv.values() if isinstance(vv, dict) else vv)
                        if str(x).strip()
                    )
                    if inner:
                        lines.append(f"{kks}: {inner}")
                    continue
                vvs = str(vv).strip()
                if vvs:
                    lines.append(f"{kks}: {vvs}")
            return "\n".join(lines) if lines else "not stated"
        if isinstance(v, list):
            lines = [str(x).strip() for x in v if str(x).strip()]
            return "\n".join(lines) if lines else "not stated"
        s = str(v).strip()
        return s or "not stated"

    return {k: _flatten(obj.get(k, "not stated")) for k in STAGE1_KEYS_9}


def stage1_summary_to_text(summary: dict[str, str], keys: Iterable[str] | None = None) -> str:
    """Компактний стабільний текстовий вигляд summary для Stage 2 та зберігання."""
    parts: list[str] = []
    for k in keys or STAGE1_KEYS_9:
        parts.append(f"## {k}")
        parts.append(summary.get(k, "not stated"))
    return "\n".join(parts) + "\n"


def summarize(
    note_text: str, *, model: str | None = None, base_url: str | None = None
) -> str:
    """Stage 1: сирий текст → SGR-summary (плаский текст із заголовками ## KEY).

    Викликає локальну MedGemma; ремонтує/нормалізує JSON; повертає текст, готовий
    і для зберігання (DocumentReference), і для Stage 2. `base_url` — окремий
    Stage 1-сервер (двосерверний LoRA-сетап).
    """
    messages = [
        {"role": "system", "content": build_stage1_system_prompt()},
        {"role": "user", "content": build_stage1_user_prompt(note_text)},
    ]
    raw = chat_completions(
        messages, model=model or STAGE1_MODEL, timeout=120.0, base_url=base_url
    )
    summary = coerce_stage1_summary(extract_json_object(raw))
    return stage1_summary_to_text(summary)
