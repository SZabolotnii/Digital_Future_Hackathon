"""Виміряна впевненість витягу замість константи з конфігу.

Досі `extraction_confidence` була трипозиційним перемикачем: 0.95 для stub, 0.5
для будь-якого LLM-шляху, 0.0 на порожньому витязі. Поріг ескалації при цьому
виводився з вартостей із точністю до третього знака — тобто строга формула
порівнювалась із числом, яке ніхто не міряв. `docs/economics.md` називав це
головною діркою моделі.

Тут вона закривається: число приходить із прогону по gold-корпусу
(`bench/run_extraction_bench.py`).

**Береться нижня межа Вілсона, а не точковий recall.** На корпусі з 60 полів
51/60 — це не 0.85, а «не менше ніж ~0.74 з 95% упевненістю». Асиметрія помилок
тут очевидна: недооцінка коштує зайвої перевірки людиною, переоцінка — тихо
пропущеного факту в медичній картці. Тож консервативний бік обирається не з
обережності взагалі, а тому що ціни різні.

Без файла калібрування поведінка лишається колишньою: `UNVERIFIED_CONFIDENCE`.
Відсутність вимірювання не привід вважати витяг надійним.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Optional

from .config import DATA_DIR, UNVERIFIED_CONFIDENCE

__all__ = ["confidence_for", "calibration_summary"]

_FILE = "extractor_calibration.json"


@lru_cache(maxsize=None)
def _data() -> dict:
    path = DATA_DIR / _FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Бите калібрування трактуємо як його відсутність, а не як привід
        # впасти: система лишається робочою, просто консервативнішою.
        return {}


def confidence_for(extractor: str) -> Optional[float]:
    """Виміряна впевненість екстрактора або None, якщо його не міряли."""
    entry = _data().get("extractors", {}).get(extractor)
    if not entry:
        return None
    value = entry.get("wilson_low")
    return float(value) if isinstance(value, (int, float)) else None


def calibration_summary() -> dict:
    """Що саме виміряно й коли — для `/metrics` і для аудиту.

    Дата вимірювання тут не прикраса: калібрування старіє разом із моделлю та
    промптом, і `null` у ній чесніше за число без дати.
    """
    data = _data()
    extractors = data.get("extractors", {})
    return {
        "measured_at": data.get("measured_at"),
        "gold_version": data.get("gold_version"),
        "extractors": {
            name: {
                "recall": e.get("recall"),
                "precision": e.get("precision"),
                "confidence_used": e.get("wilson_low"),
                "n_fields": e.get("n"),
            }
            for name, e in extractors.items()
        },
        "fallback_confidence": UNVERIFIED_CONFIDENCE,
    }
