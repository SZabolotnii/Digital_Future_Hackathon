"""Шар TRACE L4: нагляд людини як спостережувана річ, а не як напис на бейджі.

Головне, що тут фіксується: дію людини можна записати, вона переживає
перезапуск, вона закриває ескалацію, і з неї рахуються метрики нагляду. Доки
цього не було, твердження «людина в контурі» не мало жодного наслідку, який
можна перевірити ззовні.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

import livemedcard.api as api_module
from livemedcard import governance
from livemedcard.bundle import from_bundle, to_bundle
from livemedcard.dto import Document, ReviewEntry
from livemedcard.fhir import Patient
from livemedcard.pipeline import LiveMedCard
from livemedcard.reference import load_sample_docs


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(api_module, "PERSIST_PATH", "")
    c = TestClient(api_module.app)
    c.post("/reset")
    yield c
    c.post("/reset")


def _card() -> LiveMedCard:
    card = LiveMedCard(Patient(id="mom-001", gender="female", birthDate=date(1950, 1, 1)))
    card.ingest_all([Document(**d) for d in load_sample_docs()])
    return card


# ── тотожність сигналу ─────────────────────────────────────────────────────────
def test_signal_keys_survive_language_switch():
    """Ключ — тотожність, а не підпис. Перемикання мови інтерфейсу не може
    робити з переглянутого сигналу новий."""
    card = _card()
    uk = {s.key for s in card.current_signals("uk")}
    en = {s.key for s in card.current_signals("en")}
    assert uk == en
    assert all(k and " " not in k for k in uk)


def test_signal_keys_are_unique_within_a_card():
    card = _card()
    keys = [s.key for s in card.current_signals()]
    assert len(keys) == len(set(keys))


def test_range_breach_key_names_the_side_of_the_breach():
    """Показник, що вийшов за межу з іншого боку, — інший предмет для огляду."""
    card = _card()
    breaches = [s for s in card.current_signals() if s.kind == "range_breach"]
    assert breaches
    assert all(s.key.rsplit(":", 1)[1] in ("above", "below") for s in breaches)


# ── журнал дій людини ──────────────────────────────────────────────────────────
def test_review_is_recorded_and_survives_snapshot():
    card = _card()
    key = next(s.key for s in card.current_signals())
    card.review(ReviewEntry(
        ts=datetime.now(timezone.utc), actor_role="clinician",
        action="overridden", signal_key=key, note="хибне спрацювання",
    ))
    restored = LiveMedCard.from_snapshot(card.snapshot())
    assert restored.review_log() == card.review_log()
    assert restored.reviewed_signal_keys() == {key}


def test_reviews_survive_fhir_bundle_round_trip():
    """Журнал нагляду їде разом з карткою: Bundle без нього показував би її
    вічно непереглянутою."""
    card = _card()
    doc_id = next(iter(card.documents))
    card.review(ReviewEntry(
        ts=datetime.now(timezone.utc), actor_role="caregiver",
        action="acknowledged", document_id=doc_id,
    ))
    restored = from_bundle(to_bundle(card))
    assert len(restored.review_log()) == 1
    assert restored.outstanding_escalations() == card.outstanding_escalations()


def test_review_endpoint_rejects_unknown_signal(client):
    client.post("/ingest/samples")
    r = client.post("/review", json={"action": "acknowledged", "signal_key": "nope:1"})
    assert r.status_code == 404


def test_review_endpoint_requires_exactly_one_target(client):
    client.post("/ingest/samples")
    key = client.get("/state").json()["signals"][0]["key"]
    both = client.post("/review", json={
        "action": "acknowledged", "signal_key": key, "document_id": "doc-2023-05",
    })
    neither = client.post("/review", json={"action": "acknowledged"})
    assert both.status_code == 422 and neither.status_code == 422


# ── ескалація лишається видимою, доки її не закрили ────────────────────────────
def test_state_keeps_escalation_until_it_is_reviewed(client):
    """Регресія: раніше `/state` кликав роутер із впевненістю 1.0, і ескалація
    через погано розпізнаний текст зникала зі стану одразу після завантаження."""
    client.post("/ingest/samples")
    before = client.get("/state").json()["decision"]
    assert before["escalate"] is True

    audit = client.get("/audit").json()["decisions"]
    escalated = [a for a in audit if a["escalate"]]
    assert escalated
    for a in escalated:
        client.post("/review", json={
            "action": "acknowledged", "document_id": a["document_id"],
        })

    after = client.get("/state").json()["decision"]
    # Причина «непереглянуті документи» пішла; лишились major-сигнали.
    assert not any("впевненістю витягу" in r for r in after["reasons"])
    assert after["escalate"] is True


def test_reviewing_everything_closes_the_card_decision(client):
    """Робота людини мусить мати видимий наслідок. Якщо бейдж червоний завжди —
    нагляд вироджується в штампування, тобто в те, від чого L4 і захищає."""
    client.post("/ingest/samples")
    st = client.get("/state").json()
    for s in st["signals"]:
        if s["autonomy"] == "mandatory_review":
            client.post("/review", json={
                "action": "acknowledged", "signal_key": s["key"],
                "actor_role": "clinician",
            })
    for did, meta in st["docs"].items():
        if meta["escalated"]:
            client.post("/review", json={
                "action": "acknowledged", "document_id": did,
            })
    after = client.get("/state").json()
    assert after["decision"]["escalate"] is False
    assert after["decision"]["route_to"] == "auto"
    # Сигнали нікуди не поділись — рішення щодо них прийняте, а не скасоване.
    assert len(after["signals"]) == len(st["signals"])
    sup = client.get("/metrics").json()["supervision"]
    assert sup["outstanding_escalations"] == 0 and sup["unreviewed_mandatory"] == 0


def test_document_review_closes_the_outstanding_escalation():
    card = _card()
    outstanding = card.outstanding_escalations()
    assert outstanding
    card.review(ReviewEntry(
        ts=datetime.now(timezone.utc), actor_role="clinician",
        action="referred", document_id=outstanding[0].document_id,
    ))
    assert len(card.outstanding_escalations()) == len(outstanding) - 1


# ── межа автономії (ABC) ───────────────────────────────────────────────────────
def test_major_signals_are_declared_mandatory_review():
    card = _card()
    for s in card.current_signals():
        expected = "mandatory_review" if s.severity in ("major", "critical") else (
            "advisory" if s.severity == "moderate" else "autonomous"
        )
        assert governance.autonomy_class(s) == expected


def test_export_with_unreviewed_mandatory_records_a_boundary_event(client):
    client.post("/ingest/samples")
    assert client.get("/audit").json()["boundary_events"] == []
    client.get("/export")
    events = client.get("/audit").json()["boundary_events"]
    assert len(events) == 1
    assert events[0]["kind"] == "export_with_unreviewed"
    assert events[0]["signal_keys"]
    assert client.get("/metrics").json()["supervision"]["boundary_excursions"] == 1


def test_export_after_review_records_nothing(client):
    client.post("/ingest/samples")
    for s in client.get("/state").json()["signals"]:
        if s["autonomy"] == "mandatory_review":
            client.post("/review", json={
                "action": "acknowledged", "signal_key": s["key"],
            })
    client.get("/export")
    assert client.get("/audit").json()["boundary_events"] == []


# ── метрики нагляду ────────────────────────────────────────────────────────────
def test_supervision_metrics_are_none_before_anyone_looked(client):
    """Порожній знаменник — це `None`, а не нуль: «ніхто не перекривав» і «ще
    нікого не просили дивитися» — різні твердження."""
    client.post("/ingest/samples")
    sup = client.get("/metrics").json()["supervision"]
    assert sup["reviews"] == 0
    assert sup["override_rate"] is None
    assert sup["review_burden_seconds"] is None
    assert sup["outstanding_escalations"] > 0


def test_supervision_metrics_follow_the_actions(client):
    client.post("/ingest/samples")
    signals = client.get("/state").json()["signals"]
    client.post("/review", json={
        "action": "overridden", "signal_key": signals[0]["key"],
        "actor_role": "clinician", "seconds_to_act": 20,
    })
    client.post("/review", json={
        "action": "acknowledged", "signal_key": signals[1]["key"],
        "seconds_to_act": 10,
    })
    sup = client.get("/metrics").json()["supervision"]
    assert sup["reviews"] == 2
    assert sup["override_rate"] == 0.5
    assert sup["review_burden_seconds"] == 15.0
    assert sup["unreviewed_mandatory"] < len(
        [s for s in signals if s["autonomy"] == "mandatory_review"]
    ) + 1


def test_signal_to_noise_separates_danger_from_illegible_text(client):
    """SNR має відрізняти чергу «знайдено взаємодію» від черги «нерозбірливий
    текст»: це різна цінність часу людини."""
    client.post("/ingest/samples")
    drives = {a["escalation_drive"] for a in client.get("/audit").json()["decisions"]}
    assert drives & {"risk", "both", "confidence"}
    snr = client.get("/metrics").json()["supervision"]["signal_to_noise"]
    assert snr is not None and 0.0 <= snr <= 1.0
