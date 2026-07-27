"""Часовий ряд метрик (OSI): чи видно, що система зрушила.

Решта метрик каже, як воно зараз. Система, яка тихо деградує, у кожен окремий
момент виглядає прийнятною — ловиться саме рух.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import livemedcard.api as api_module
from livemedcard import history


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(api_module, "PERSIST_PATH", "")
    monkeypatch.setattr(api_module, "METRICS_HISTORY_PATH", str(tmp_path / "m.jsonl"))
    c = TestClient(api_module.app)
    c.post("/reset")
    yield c
    c.post("/reset")


def test_history_is_off_by_default(monkeypatch):
    monkeypatch.setattr(api_module, "PERSIST_PATH", "")
    monkeypatch.setattr(api_module, "METRICS_HISTORY_PATH", "")
    c = TestClient(api_module.app)
    assert c.post("/metrics/snapshot").status_code == 503
    assert c.get("/metrics/history").status_code == 503


def test_snapshot_records_the_tracked_metrics(client):
    client.post("/ingest/samples")
    row = client.post("/metrics/snapshot").json()
    assert row["ts"]
    assert set(history.TRACKED) <= set(row)
    assert row["escalation_rate"] == 1.0
    assert row["ruleset_version"]


def test_drift_needs_two_points(client):
    client.post("/ingest/samples")
    client.post("/metrics/snapshot")
    d = client.get("/metrics/history").json()["drift"]
    assert d["snapshots"] == 1 and d["changes"] == {}


def test_drift_shows_what_moved(client):
    """Перегляди людиною зсувають метрики нагляду — саме це має бути видно."""
    client.post("/ingest/samples")
    client.post("/metrics/snapshot")

    for a in client.get("/audit").json()["decisions"]:
        client.post("/review", json={"action": "acknowledged",
                                     "document_id": a["document_id"]})
    client.post("/metrics/snapshot")

    d = client.get("/metrics/history").json()["drift"]
    assert d["snapshots"] == 2
    assert d["changes"]["supervision.review_coverage"]["to"] == 1.0
    assert d["changes"]["supervision.review_coverage"]["delta"] > 0
    assert d["ruleset_changed"] is False


def test_small_wobble_is_not_reported_as_drift():
    """Поріг існує, щоб не привчити ігнорувати показник: якщо позначати кожне
    коливання на малій картці, дивитись на нього перестануть."""
    rows = [
        {"ts": "1", "escalation_rate": 0.50},
        {"ts": "2", "escalation_rate": 0.52},
    ]
    assert history.drift(rows)["changes"] == {}
    assert "escalation_rate" in history.drift(rows, tolerance=0.01)["changes"]


def test_none_is_not_compared():
    """`None` означає «не міряли», і різниця з ним не число."""
    rows = [
        {"ts": "1", "supervision.override_rate": None},
        {"ts": "2", "supervision.override_rate": 0.4},
    ]
    assert history.drift(rows)["changes"] == {}


def test_broken_line_does_not_hide_the_rest(tmp_path):
    """На відміну від знімка картки, тут утрата рядка не спотворює дані, а
    падіння на ньому позбавило б доступу до решти історії."""
    path = tmp_path / "m.jsonl"
    path.write_text(
        json.dumps({"ts": "1", "escalation_rate": 0.1}) + "\n"
        + "{обірваний запис\n"
        + json.dumps({"ts": "2", "escalation_rate": 0.9}) + "\n",
        encoding="utf-8",
    )
    rows = history.load(str(path))
    assert len(rows) == 2
    assert history.drift(rows)["changes"]["escalation_rate"]["delta"] == pytest.approx(0.8)


def test_ruleset_change_is_flagged():
    rows = [
        {"ts": "1", "ruleset_version": "1.0.0+aaa"},
        {"ts": "2", "ruleset_version": "1.1.0+bbb"},
    ]
    assert history.drift(rows)["ruleset_changed"] is True
