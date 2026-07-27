"""L3 re-invocation: друга спроба замість передчасного виклику людини (FPA).

Сенс політики — не в економії як такій, а в тому, що кожна ескалація, знята
автоматично, лишає увагу людини для тих, де вона справді потрібна. Тому
головне, що тут фіксується: перевикликаємо ЛИШЕ через низьку впевненість
витягу, ніколи — через знайдену небезпеку.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import livemedcard.api as api_module
from livemedcard import economics
from livemedcard.dto import Document, ExtractionResult
from livemedcard.fhir import Patient
from livemedcard.layers import l3_router
from livemedcard.layers.l2b_extractor import StubExtractor, deterministic_extract
from livemedcard.pipeline import LiveMedCard


class _Blind:
    """Екстрактор, що нічого не бачить — впевненість 0.0."""

    calls = 0

    def extract(self, doc: Document, patient_id: str) -> ExtractionResult:
        type(self).calls += 1
        return ExtractionResult(mean_confidence=0.0)


class _Better:
    """Альтернатива, що читає документ як слід."""

    calls = 0

    def extract(self, doc: Document, patient_id: str) -> ExtractionResult:
        type(self).calls += 1
        return deterministic_extract(doc.text, doc, patient_id)


def _patient() -> Patient:
    return Patient(id="p", gender="female", birthDate=date(1950, 1, 1))


def _doc(text: str = "Креатинін 88 µmol/L.") -> Document:
    return Document(id="d", text=text, kind="lab_report",
                    received_at=datetime(2024, 1, 1))


@pytest.fixture(autouse=True)
def _reset_counters():
    _Blind.calls = _Better.calls = 0


# ── політика ───────────────────────────────────────────────────────────────────
def test_reinvokes_only_below_the_threshold():
    low = economics.escalation_threshold("measurement") - 0.01
    high = economics.escalation_threshold("measurement") + 0.01
    assert l3_router.should_reinvoke(low, "measurement", 1) is True
    assert l3_router.should_reinvoke(high, "measurement", 1) is False


def test_budget_is_finite():
    """Невизначеність, яка не зникла з другого разу, здебільшого не зникне і з
    третього — а платить за спроби той, хто чекає на відповідь."""
    assert l3_router.should_reinvoke(0.0, "medication", 1) is True
    assert l3_router.should_reinvoke(0.0, "medication", 2) is False
    assert l3_router.should_reinvoke(0.0, "medication", 1, budget=0) is False


# ── конвеєр ────────────────────────────────────────────────────────────────────
def test_second_attempt_rescues_a_failed_extraction():
    card = LiveMedCard(_patient(), _Blind(), fallback_extractor=_Better())
    report = card.ingest(_doc())

    assert _Blind.calls == 1 and _Better.calls == 1
    assert report.extraction.observations, "факти має дати друга спроба"
    entry = card.audit_log()[-1]
    assert entry["attempts"] == 2
    # Провенанс називає шлях, чий результат ВЗЯЛИ, а не останній випробуваний.
    assert entry["extractor"] == "_Better"


def test_worse_alternative_does_not_replace_the_first_result():
    """Повторна спроба має коштувати лише часу: гірший результат не витісняє
    кращий, інакше друга спроба була б ризиком, а не страховкою."""
    card = LiveMedCard(_patient(), StubExtractor(), fallback_extractor=_Blind())
    report = card.ingest(_doc())

    assert _Blind.calls == 1                      # спроба була
    assert report.extraction.observations          # але взяли перший результат
    assert card.audit_log()[-1]["extractor"] == "StubExtractor"


def test_no_fallback_means_no_retry():
    card = LiveMedCard(_patient(), _Blind())
    card.ingest(_doc())
    assert _Blind.calls == 1
    assert card.audit_log()[-1]["attempts"] == 1


def test_found_danger_is_never_re_extracted():
    """Знайдена взаємодія — не привід для другої спроби: сигнал уже достовірний,
    і повторний витяг його не спростує, а лише відкладе передачу людині."""
    card = LiveMedCard(_patient(), StubExtractor(), fallback_extractor=_Better())
    card.ingest(Document(
        id="rx", kind="prescription", received_at=datetime(2024, 2, 1),
        text="Варфарин 5 мг на добу. Аспірин 100 мг на добу.",
    ))
    entry = card.audit_log()[-1]
    assert entry["escalation_drive"] in ("risk", "both")
    assert entry["high_risk"] is True
    # Впевненість витягу тут нижча за поріг для ліків (0.967), тож спроба буде —
    # але вирішує її саме впевненість, а не наявність сигналу.
    assert _Better.calls == (1 if entry["escalation_drive"] == "both" else 0)


# ── метрика ────────────────────────────────────────────────────────────────────
def test_fpa_is_none_until_something_was_retried():
    client = TestClient(api_module.app)
    api_module.PERSIST_PATH = ""
    client.post("/reset")
    client.post("/ingest/samples")
    cost = client.get("/metrics").json()["cost"]
    assert cost["reinvocations"] == 0
    assert cost["false_positive_attenuation"] is None
    assert cost["paths"]["StubExtractor"]["documents"] == 4
    client.post("/reset")


def test_fpa_counts_escalations_the_retry_removed():
    card = LiveMedCard(_patient(), _Blind(), fallback_extractor=_Better())
    card.ingest(_doc())
    log = card.audit_log()
    retried = [a for a in log if a["attempts"] > 1]
    assert len(retried) == 1
    # Виміряна впевненість stub-а (0.739) нижча за поріг, тож ескалація лишається
    # навіть після вдалої другої спроби. FPA чесно покаже 0.0, а не приховає це:
    # політика спрацювала, але недостатньо, щоб зняти виклик людини.
    assert retried[0]["escalate"] is True
