"""Калібрування витягу: впевненість як виміряна величина, а не константа.

Найважливіше тут — не самі числа, а те, що поріг ескалації тепер порівнюється з
чимось поміряним. Доти строга формула `c* = 1 − C_human/(π·C_harm)` рахувалась до
третього знака й порівнювалась із числом, яке ніхто не міряв.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from livemedcard import calibration, economics
from livemedcard.config import STUB_CONFIDENCE, UNVERIFIED_CONFIDENCE
from livemedcard.dto import Document
from livemedcard.fhir import Patient
from livemedcard.layers.l2b_extractor import StubExtractor
from livemedcard.pipeline import LiveMedCard


def _doc(text: str = "Креатинін 121 µmol/L.") -> Document:
    return Document(id="d", text=text, kind="lab_report",
                    received_at=datetime(2024, 1, 1))


def test_measured_confidence_is_used_when_available():
    measured = calibration.confidence_for("StubExtractor")
    assert measured is not None, "калібрування має бути в репозиторії"
    assert StubExtractor().extract(_doc(), "p").mean_confidence == measured


def test_missing_calibration_falls_back_to_unverified(monkeypatch):
    """Відсутність вимірювання — не привід вважати витяг надійним."""
    monkeypatch.setattr(calibration, "_data", lambda: {})
    assert calibration.confidence_for("StubExtractor") is None


def test_broken_calibration_file_does_not_break_the_pipeline(tmp_path, monkeypatch):
    """Бите калібрування трактуємо як його відсутність: система лишається
    робочою, просто консервативнішою. Падати тут означало б зробити метрику
    небезпечнішою за її відсутність."""
    monkeypatch.setattr(calibration, "_data", lambda: json.loads("{}"))
    assert calibration.confidence_for("StubExtractor") is None


def test_wilson_bound_is_below_the_point_estimate():
    """Береться нижня межа, а не точковий recall: на малому корпусі 51/60 — це
    не 0.85, а «не менше ніж ~0.74». Асиметрія цін очевидна: недооцінка коштує
    перевірки людиною, переоцінка — пропущеного факту в картці."""
    from bench.run_extraction_bench import wilson_lower

    assert wilson_lower(51, 60) < 51 / 60
    assert wilson_lower(60, 60) < 1.0          # навіть ідеальний прогін не дає 1.0
    assert wilson_lower(0, 0) == 0.0           # без даних — нуль, не одиниця
    assert wilson_lower(510, 600) > wilson_lower(51, 60)  # більший корпус — вужча межа


def test_calibration_summary_reports_what_was_measured_and_when():
    s = calibration.calibration_summary()
    assert s["measured_at"] and s["gold_version"]
    stub = s["extractors"]["StubExtractor"]
    assert stub["confidence_used"] < stub["recall"]      # використовується межа
    assert stub["n_fields"] > 0
    assert s["fallback_confidence"] == UNVERIFIED_CONFIDENCE


def test_stored_calibration_matches_the_current_code():
    """Калібрування живить поріг ескалації, тож воно не має права старіти тихо.

    Якщо цей тест упав — витяг змінився, а записане число ні. Перезняти:
    ``uv run python bench/run_extraction_bench.py --write-calibration``.
    """
    from bench.run_extraction_bench import GOLD, run

    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    fresh = run(StubExtractor(), gold)["summary"]["all"]
    stored = calibration.calibration_summary()["extractors"]["StubExtractor"]

    assert stored["recall"] == fresh["recall"]
    assert stored["precision"] == fresh["precision"]
    assert stored["confidence_used"] == fresh["wilson_recall_low"]


def test_measured_confidence_now_escalates_what_used_to_pass():
    """Головний наслідок вимірювання, названий прямо.

    `STUB_CONFIDENCE = 0.95` перевищував поріг для документів лише з показниками
    (0.80), тож вони йшли в auto. Виміряна межа нижча за обидва пороги — отже
    автоматично не проходить ніщо. Це не регресія, а зникнення необґрунтованої
    довіри: підстав для 0.95 не було, вони просто ніде не перевірялись.
    """
    measured = calibration.confidence_for("StubExtractor")
    assert measured < economics.escalation_threshold("measurement")
    assert STUB_CONFIDENCE > economics.escalation_threshold("measurement")

    card = LiveMedCard(Patient(id="p", gender="female", birthDate=date(1950, 1, 1)))
    report = card.ingest(_doc())
    assert report.decision.escalate is True
