"""Часовий ряд метрик довіри — вхід для OSI (Operational Stability Index).

Решта метрик відповідає на питання «як воно зараз». Це питання неповне: система,
яка тихо деградує, у кожен окремий момент виглядає прийнятною. OSI ловить саме
рух — падіння покриття правил після оновлення довідника, зростання латентності,
сповзання OvR до нуля.

**Знімки робить оператор, а не система.** `POST /metrics/snapshot` — свідомий
вибір проти автозапису на кожен `GET /metrics`: інтерфейс опитує метрики раз на
кілька секунд, і ряд із такою щільністю показував би не дрейф, а активність
вкладки. Правильна каденція залежить від розгортання (реліз, доба, тиждень), і
вгадувати її за оператора тут нема підстав.

Формат — JSONL: дозапис одним рядком, читання без парсингу всього файла,
переживає обірваний запис. Шлях — env `LMC_METRICS_HISTORY`; порожньо → вимкнено.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__all__ = ["TRACKED", "flatten", "append", "load", "drift"]

# Плоскі ключі, за якими справді варто стежити. Не всі метрики: ряд із сорока
# чисел ніхто не читає, а дрейф видно по кількох.
TRACKED: tuple[str, ...] = (
    "escalation_rate",
    "rule_coverage.coverage",
    "evidence.fact_trail",
    "evidence.signal_trail",
    "supervision.override_rate",
    "supervision.review_coverage",
    "supervision.signal_to_noise",
    "supervision.review_burden_seconds",
    "supervision.boundary_excursions",
)


def _dig(data: dict, path: str):
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def flatten(metrics: dict) -> dict:
    """Знімок відстежуваних метрик + версії, що могли на них вплинути."""
    snapshot = {key: _dig(metrics, key) for key in TRACKED}
    snapshot["ruleset_version"] = _dig(metrics, "evidence.ruleset_version")
    snapshot["documents"] = metrics.get("documents")
    return snapshot


def append(metrics: dict, path: str) -> dict:
    """Дозаписати знімок; повертає записаний рядок."""
    row = {"ts": datetime.now(timezone.utc).isoformat(), **flatten(metrics)}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def load(path: str) -> list[dict]:
    """Прочитати ряд. Побиті рядки пропускаємо мовчки — на відміну від знімка
    картки, тут утрата одного запису не спотворює дані, а падіння на ньому
    позбавило б доступу до решти історії."""
    target = Path(path)
    if not target.exists():
        return []
    rows = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def drift(rows: list[dict], *, tolerance: float = 0.05) -> dict:
    """Що зрушило між першим і останнім знімком.

    `tolerance` — мінімальна зміна, яку варто називати зрушенням; менші коливання
    на малих картках — шум, і позначати їх означало б привчити ігнорувати
    показник. Метрики, яких не було в котромусь із кінців ряду, не порівнюються:
    `None` означає «не міряли», і різниця з ним не число.
    """
    if len(rows) < 2:
        return {"snapshots": len(rows), "changes": {}, "ruleset_changed": False}

    first, last = rows[0], rows[-1]
    changes = {}
    for key in TRACKED:
        a, b = first.get(key), last.get(key)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        delta = b - a
        if abs(delta) >= tolerance:
            changes[key] = {"from": a, "to": b, "delta": round(delta, 3)}
    return {
        "snapshots": len(rows),
        "from": first.get("ts"),
        "to": last.get("ts"),
        "changes": changes,
        # Зміна правил — перше, на що дивляться, коли метрики поїхали.
        "ruleset_changed": first.get("ruleset_version") != last.get("ruleset_version"),
    }


def configured_path(value: Optional[str]) -> str:
    return value or ""
