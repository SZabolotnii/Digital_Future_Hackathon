"""PP-OCR на наборі Stage 1 — покриття, час і тир моделі.

Метрика та сама, що в `run_bench.py` (входження еталонного значення в текст),
щоб числа стояли поряд з Apple Vision / HunyuanOCR / NuExtract3. Прогін іде
через ПРОДУКТОВИЙ драйвер `livemedcard.ppocr`, а не через окремий код: інакше
бенчмарк міряв би не те, що працює в картці.

    uv run --extra ppocr python bench/run_ppocr_bench.py [--lang en] [tiny small ...]

Мова тут не косметика: у PP-OCRv6 кирилиці немає, тож `--lang uk` (дефолт
драйвера) підставляє інший розпізнавач і платить за це і покриттям, і часом на
англомовних документах. Набір нижче — англомовний, і його чесно міряти з
`--lang en`; `uk` на ньому показує саме ціну кириличної збірки.

Зображення очікуються поряд як `bench/{ключ gold.json}.png` (у git їх немає —
це реальні медичні документи).
"""
from __future__ import annotations

import base64
import json
import re
import sys
import time
from pathlib import Path

from livemedcard import ppocr

HERE = Path(__file__).parent
GOLD = json.loads((HERE / "gold.json").read_text(encoding="utf-8"))


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace(" ", " ")).lower()


def run(tier: str, lang: str) -> dict:
    print(f"\n{'=' * 72}\nPP-OCR · тир {tier} · {ppocr.engine_id(lang, tier)}")
    out: dict = {}
    for doc, spec in GOLD.items():
        img = HERE / f"{doc}.png"
        if not img.exists():
            print(f"  !! немає {img.name}")
            continue
        b64 = base64.b64encode(img.read_bytes()).decode()
        t = time.time()
        text = ppocr.image_to_text(b64, lang=lang, tier=tier)
        dt = time.time() - t
        (HERE / f"out_{doc}_ppocr-{tier}-{lang}.txt").write_text(text, encoding="utf-8")

        n = norm(text)
        vals, hand = spec["values"], spec.get("handwriting", [])
        miss = [v for v in vals if norm(v) not in n]
        hhit = [v for v in hand if norm(v) in n]
        out[doc] = {"hit": len(vals) - len(miss), "total": len(vals),
                    "sec": round(dt, 1), "miss": miss,
                    "hand": f"{len(hhit)}/{len(hand)}" if hand else "—"}
        r = out[doc]
        print(f"  {doc:14s} {r['hit']:2d}/{r['total']:<2d} {r['sec']:6.1f}s"
              f"   рукопис: {r['hand']}")
        if miss:
            print(f"                 ✗ {', '.join(miss)}")
    if out:
        hits = sum(v["hit"] for v in out.values())
        total = sum(v["total"] for v in out.values())
        print(f"  РАЗОМ {hits}/{total}   сер. "
              f"{sum(v['sec'] for v in out.values()) / len(out):.1f} с")
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    lang = "uk"
    if "--lang" in args:
        i = args.index("--lang")
        lang = args[i + 1]
        del args[i:i + 2]
    tiers = args or ["tiny", "small", "medium"]
    results = {f"{t}-{lang}": run(t, lang) for t in tiers}
    (HERE / "ppocr_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
