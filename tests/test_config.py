"""Конфігурація зі змінних середовища: невідомий бекенд зупиняє старт (ADR-15)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _import_api(extractor: str, image_ocr: str = "") -> subprocess.CompletedProcess:
    # Окремий процес: конфіг читається при імпорті, і перезавантаження модуля
    # тут зіпсувало б стан для решти тестів.
    env = {**os.environ, "LMC_EXTRACTOR": extractor, "LMC_IMAGE_OCR": image_ocr,
           "PYTHONPATH": str(ROOT)}
    return subprocess.run(
        [sys.executable, "-c", "import livemedcard.api"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


def test_unknown_extractor_fails_at_startup():
    p = _import_api("gemni")
    assert p.returncode != 0
    assert "LMC_EXTRACTOR='gemni'" in p.stderr
    assert "stub, two_stage, two_stage_lora, nuextract, ocrlab" in p.stderr


def test_known_extractor_starts():
    p = _import_api("stub")
    assert p.returncode == 0, p.stderr


def test_unknown_image_ocr_fails_at_startup():
    p = _import_api("stub", image_ocr="nonе")   # кирилична «е» — типова описка
    assert p.returncode != 0
    assert "LMC_IMAGE_OCR=" in p.stderr
    assert "порожньо, ocrlab, nuextract, local, ppocr, none" in p.stderr


def test_image_ocr_none_starts():
    p = _import_api("stub", image_ocr="none")
    assert p.returncode == 0, p.stderr
