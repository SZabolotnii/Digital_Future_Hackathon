"""Конфігурація зі змінних середовища: невідомий бекенд зупиняє старт (ADR-15)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _import_api(extractor: str) -> subprocess.CompletedProcess:
    # Окремий процес: конфіг читається при імпорті, і перезавантаження модуля
    # тут зіпсувало б стан для решти тестів.
    env = {**os.environ, "LMC_EXTRACTOR": extractor, "PYTHONPATH": str(ROOT)}
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
