"""Вимірювання витягу L2b на gold-корпусі: precision / recall / F1 по полях.

Свідомо НЕ так, як `run_bench.py`, де метрика — входження підрядка. Там це було
доречно (перевіряли транскрипцію), але для витягу таке міряло б не те: хибний
факт не карається взагалі, а `121` знайдеться в тексті й тоді, коли модель
приписала його не тому показнику.

Тут збіг — це кортеж `(LOINC, значення ± допуск, одиниця)`. Одиниця входить у
ключ, бо «глюкоза 122» у mg/dL і в mmol/L — різні твердження, і плутанина між
ними саме та помилка, яку шар L2a потім чесно назве `unit_mismatch`.

Результат — вхід для калібрування (`livemedcard/data/extractor_calibration.json`):
впевненість витягу перестає бути константою з конфігу й починає бути виміряною
величиною. Береться нижня межа Вілсона, а не точковий recall — недооцінювати
себе безпечно, переоцінювати ні.

    uv run python bench/run_extraction_bench.py                  # stub
    uv run python bench/run_extraction_bench.py --write-calibration
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from math import sqrt
from pathlib import Path

from livemedcard.config import DATA_DIR
from livemedcard.dto import Document
from livemedcard.fhir import loinc_of, rxnorm_of
from livemedcard.layers.l2b_extractor import StubExtractor

GOLD = Path(__file__).with_name("extraction_gold.json")
OUT = Path(__file__).with_name("extraction_results.json")
CALIBRATION = DATA_DIR / "extractor_calibration.json"

VALUE_TOLERANCE = 1e-6


def wilson_lower(hits: int, n: int, z: float = 1.96) -> float:
    """Нижня межа Вілсона для частки — консервативна оцінка recall.

    На малому корпусі точковий recall завищує: 20/20 не означає 1.0, воно
    означає «не менше ніж ~0.84 з 95% упевненістю». Саме це число має йти в
    поріг ескалації, бо помилка в бік недовіри коштує перевірки, а в бік
    довіри — пропущеного факту.
    """
    if n == 0:
        return 0.0
    p = hits / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _norm_unit(unit) -> str:
    return (unit or "").strip().lower()


def _fact_key(loinc, value, unit) -> tuple:
    return (loinc, round(float(value), 6), _norm_unit(unit))


def _expected_keys(doc: dict) -> tuple[set, set]:
    facts = {_fact_key(x["loinc"], x["value"], x["unit"]) for x in doc["facts"]}
    meds = {(x["rxnorm"], x["dose_mg_per_day"]) for x in doc["medications"]}
    return facts, meds


def _extracted_keys(result) -> tuple[set, set]:
    facts = set()
    for o in result.observations:
        code = loinc_of(o)
        if code is None or o.valueQuantity is None:
            continue  # некодований факт: правила його не бачать, отже і збігу нема
        facts.add(_fact_key(code, o.valueQuantity.value, o.valueQuantity.unit))
    meds = {(rxnorm_of(m), m.dose_mg_per_day) for m in result.medications}
    meds.discard((None, None))
    return facts, {(rx, dose) for rx, dose in meds if rx is not None}


def _prf(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision and recall else (0.0 if precision is not None and recall is not None else None)
    )
    return {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": round(precision, 3) if precision is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "f1": round(f1, 3) if f1 is not None else None,
    }


def run(extractor, gold: dict) -> dict:
    per_doc, misses = [], []
    agg = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})

    for g in gold["documents"]:
        doc = Document(
            id=g["id"], text=g["text"], kind=g["kind"],
            received_at=datetime.fromisoformat(g["received_at"]),
        )
        result = extractor.extract(doc, "bench-patient")
        got_f, got_m = _extracted_keys(result)
        want_f, want_m = _expected_keys(g)

        for scope, got, want in (("facts", got_f, want_f), ("medications", got_m, want_m)):
            tp, fp, fn = len(got & want), len(got - want), len(want - got)
            for bucket in (agg[scope], agg["all"], agg[f"lang:{g['lang']}"]):
                bucket["tp"] += tp
                bucket["fp"] += fp
                bucket["fn"] += fn
            if fp or fn:
                misses.append({
                    "id": g["id"], "scope": scope, "probes": g["probes"],
                    "missed": sorted(str(x) for x in (want - got)),
                    "spurious": sorted(str(x) for x in (got - want)),
                })

        per_doc.append({
            "id": g["id"], "lang": g["lang"], "kind": g["kind"], "probes": g["probes"],
            "facts": _prf(len(got_f & want_f), len(got_f - want_f), len(want_f - got_f)),
            "medications": _prf(
                len(got_m & want_m), len(got_m - want_m), len(want_m - got_m)
            ),
        })

    summary = {k: _prf(v["tp"], v["fp"], v["fn"]) for k, v in agg.items()}
    for stats in summary.values():
        stats["wilson_recall_low"] = round(
            wilson_lower(stats["tp"], stats["tp"] + stats["fn"]), 3
        )
    return {"summary": summary, "documents": per_doc, "misses": misses}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extractor", default="stub", choices=["stub"],
                    help="LLM-шляхи вимагають живих серверів; тут лише offline")
    ap.add_argument("--write-calibration", action="store_true",
                    help="записати виміряну впевненість у data/extractor_calibration.json")
    args = ap.parse_args()

    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    extractor = StubExtractor()
    report = {
        "extractor": args.extractor,
        "gold_version": gold["version"],
        "measured_at": datetime.now().date().isoformat(),
        **run(extractor, gold),
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    s = report["summary"]
    print(f"{args.extractor}: корпус {len(gold['documents'])} документів "
          f"(gold {gold['version']})")
    for scope in ("all", "facts", "medications", "lang:uk", "lang:en"):
        x = s[scope]
        print(f"  {scope:<14} P={x['precision']} R={x['recall']} F1={x['f1']} "
              f"(Wilson low {x['wilson_recall_low']}) tp={x['tp']} fp={x['fp']} fn={x['fn']}")
    if report["misses"]:
        print(f"\n  розходжень: {len(report['misses'])} — див. {OUT.name}")

    if args.write_calibration:
        payload = {
            "_note": (
                "Виміряна впевненість витягу. Береться нижня межа Вілсона для "
                "recall, а не точкова оцінка: недооцінювати себе безпечно. "
                "Оновлювати через bench/run_extraction_bench.py --write-calibration."
            ),
            "gold_version": gold["version"],
            "measured_at": report["measured_at"],
            "extractors": {
                "StubExtractor": {
                    "recall": s["all"]["recall"],
                    "precision": s["all"]["precision"],
                    "n": s["all"]["tp"] + s["all"]["fn"],
                    "wilson_low": s["all"]["wilson_recall_low"],
                }
            },
        }
        CALIBRATION.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n  калібрування записано: {CALIBRATION}")


if __name__ == "__main__":
    main()
