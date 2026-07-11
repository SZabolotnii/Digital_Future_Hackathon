"""Stage 1 shootout: Apple Vision vs HunyuanOCR 1B vs NuExtract3 4B.

Метрика — покриття еталону транскрипцією: чи присутнє кожне ключове значення у
витягнутому тексті. Це верхня межа якості конвеєра: чого немає в тексті, того
жоден Stage 2 вже не витягне.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).parent
GOLD = json.loads((HERE / "gold.json").read_text(encoding="utf-8"))
PROMPT = "Transcribe this document to markdown. Preserve all tables and numbers exactly."

ENGINES = {
    "vision": None,  # окремий шлях (pyobjc)
    "hunyuan": "ggml-org/HunyuanOCR-GGUF:Q8_0",
    "nuextract": "numind/NuExtract3-GGUF:Q4_K_M",
}


def run_llama(hf: str, img: Path) -> tuple[str, float]:
    t = time.time()
    p = subprocess.run(
        ["llama-mtmd-cli", "-hf", hf, "--image", str(img), "-p", PROMPT,
         "-n", "2048", "--temp", "0", "--repeat-penalty", "1.05", "-c", "8192"],
        capture_output=True, text=True, timeout=900,
    )
    return p.stdout, time.time() - t


def run_vision(img: Path) -> tuple[str, float]:
    t = time.time()
    p = subprocess.run(
        ["uv", "run", "--with", "pyobjc-framework-Vision",
         "--with", "pyobjc-framework-Quartz", "python",
         str(HERE / "vision_ocr.py"), str(img)],
        capture_output=True, text=True, timeout=300, cwd=HERE,
    )
    return p.stdout, time.time() - t


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace(" ", " ")).lower()


results: dict = {}
for doc, spec in GOLD.items():
    img = HERE / f"{doc}.png"
    if not img.exists():
        print(f"!! немає {img}")
        continue
    print(f"\n{'=' * 72}\n{doc} — {spec['title']}\n  ({spec['kind']})")
    checks = spec["values"]
    hand = spec.get("handwriting", [])
    results[doc] = {}
    for eng in ENGINES:
        try:
            txt, dt = (run_vision(img) if eng == "vision"
                       else run_llama(ENGINES[eng], img))
        except subprocess.TimeoutExpired:
            print(f"  {eng:10s} ТАЙМАУТ")
            continue
        (HERE / f"out_{doc}_{eng}.txt").write_text(txt, encoding="utf-8")
        n = norm(txt)
        hit = [v for v in checks if norm(v) in n]
        hhit = [v for v in hand if norm(v) in n]
        results[doc][eng] = {
            "hit": len(hit), "total": len(checks), "sec": round(dt),
            "miss": [v for v in checks if norm(v) not in n],
            "hand": f"{len(hhit)}/{len(hand)}" if hand else "—",
        }
        r = results[doc][eng]
        print(f"  {eng:10s} {r['hit']:2d}/{r['total']:<2d}  {r['sec']:3d}s"
              f"   рукопис: {r['hand']}")
        if r["miss"]:
            print(f"             ✗ {', '.join(r['miss'][:6])}")

(HERE / "results.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n\n=== ПІДСУМОК ===")
for doc, engs in results.items():
    row = "  ".join(f"{e}: {v['hit']}/{v['total']} ({v['sec']}s)"
                    for e, v in engs.items())
    print(f"{doc:14s} {row}")
