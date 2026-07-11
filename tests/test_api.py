"""HTTP-тести API через TestClient: контракт ендпойнтів + потокобезпечність стану."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import livemedcard.api as api_module


@pytest.fixture()
def client(monkeypatch):
    # Тести не мають чіпати файл персистентності користувача.
    monkeypatch.setattr(api_module, "PERSIST_PATH", "")
    c = TestClient(api_module.app)
    c.post("/reset")
    yield c
    c.post("/reset")


def test_state_initially_empty(client):
    r = client.get("/state")
    assert r.status_code == 200
    body = r.json()
    assert body["document_count"] == 0
    assert body["series"] == []
    assert body["signals"] == []


def test_ingest_then_timeline_and_sex_range(client):
    r = client.post("/ingest", json={
        "id": "t1", "text": "Креатинін 121 µmol/L.", "kind": "lab_report",
        "received_at": "2024-03-01T00:00:00",
    })
    assert r.status_code == 200
    tl = client.get("/timeline/2160-0").json()
    assert len(tl) == 1 and tl[0]["value"] == 121.0
    series = client.get("/state").json()["series"]
    creat = next(s for s in series if s["loinc"] == "2160-0")
    assert (creat["low"], creat["high"]) == (44, 90)  # межі за статтю (жінка)


def test_oversized_document_rejected_413(client):
    big = "х" * (api_module.MAX_DOC_CHARS + 1)
    r = client.post("/ingest", json={"id": "big", "text": big, "kind": "note"})
    assert r.status_code == 413


def test_samples_flow_signals_metrics_audit(client):
    reports = client.post("/ingest/samples").json()
    assert len(reports) == 4
    assert client.get("/signals").json()  # тренд/межі/взаємодія на демо-даних
    m = client.get("/metrics").json()
    assert m["documents"] == 4 and m["decisions"] == 4
    assert m["escalated"] + m["auto"] == m["decisions"]
    assert m["escalated"] >= 1
    audit = client.get("/audit").json()
    assert len(audit) == 4
    assert all(set(a) >= {"ts", "route_to", "escalate"} for a in audit)


def test_ask_grounded_with_sources(client):
    client.post("/ingest/samples")
    ans = client.post("/ask", json={"question": "Що з нирками?"}).json()
    assert "135" in ans["text"]
    assert ans["sources"]


def test_export_import_round_trip(client):
    client.post("/ingest/samples")
    before = client.get("/state").json()
    bundle = client.get("/export").json()
    assert bundle["resourceType"] == "Bundle"
    client.post("/reset")
    assert client.get("/state").json()["document_count"] == 0
    assert client.post("/import", json=bundle).status_code == 200
    after = client.get("/state").json()
    assert after["document_count"] == before["document_count"]
    assert len(after["signals"]) == len(before["signals"])


def test_import_bundle_without_patient_422(client):
    r = client.post("/import", json={"resourceType": "Bundle", "entry": []})
    assert r.status_code == 422


def test_concurrent_mixed_requests_stay_consistent(client):
    """Мікс конкурентних читань/мутацій не дає 5xx і лишає стан цілісним."""

    def call(i: int) -> int:
        if i % 4 == 0:
            return client.post("/reset").status_code
        if i % 4 == 1:
            return client.post("/ingest/samples").status_code
        if i % 4 == 2:
            return client.get("/state").status_code
        return client.get("/metrics").status_code

    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(call, range(24)))
    assert all(c == 200 for c in codes)
    # Кожен запит атомарний під локом → фінальний стан або порожній, або 3 демо-доки.
    assert client.get("/state").json()["document_count"] in (0, 4)


def test_state_lang_en_localizes_signals_and_reasons(client):
    client.post("/ingest/samples")
    body = client.get("/state?lang=en").json()
    joined = " ".join(s["detail"] for s in body["signals"])
    assert "Warfarin + Aspirin" in joined
    assert "above range" in joined
    # Жодної кирилиці в EN-сигналах і причинах рішення.
    all_text = joined + " ".join(body["decision"]["reasons"])
    assert not any("Ѐ" <= ch <= "ӿ" for ch in all_text)
    # Дефолт (uk) незмінний — зворотна сумісність.
    uk = client.get("/state").json()
    assert any("вище норми" in s["detail"] for s in uk["signals"])


def test_state_points_carry_span_and_document_endpoint(client):
    client.post("/ingest", json={
        "id": "t-span", "text": "Креатинін 121 µmol/L.", "kind": "lab_report",
        "received_at": "2024-03-01T00:00:00",
    })
    series = client.get("/state").json()["series"]
    pt = next(s for s in series if s["loinc"] == "2160-0")["points"][0]
    assert pt["source"] == "t-span"
    assert pt["span"] is not None
    doc = client.get("/document/t-span")
    assert doc.status_code == 200
    text = doc.json()["text"]
    start, end = pt["span"]
    assert "121" in text[start:end]


def test_document_endpoint_404_for_unknown(client):
    assert client.get("/document/no-such-doc").status_code == 404
