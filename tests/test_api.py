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
    assert set(audit) == {"decisions", "reviews", "boundary_events"}
    assert len(audit["decisions"]) == 4
    assert all(
        set(a) >= {"ts", "route_to", "escalate", "escalation_drive"}
        for a in audit["decisions"]
    )


def test_sample_sets_mirror_each_other_across_languages(client):
    """Демо-набір іде за мовою інтерфейсу, але картка має бути ТА САМА: ті самі
    значення → ті самі сигнали. Інакше журі бачить різний продукт залежно від мови."""
    en = TestClient(api_module.app)
    client.post("/ingest/samples?lang=uk")
    en.post("/ingest/samples?lang=en")

    uk_state, en_state = client.get("/state").json(), en.get("/state?lang=en").json()
    def sig(s):
        return sorted((x["layer"], x["kind"], x["severity"]) for x in s["signals"])

    assert sig(uk_state) == sig(en_state)          # 8 однакових сигналів
    assert len(en_state["signals"]) == 8
    # доза метформіну «3000 mg per day» має читатися так само, як «3000 мг на добу»
    assert any(x["kind"] == "dose_limit" for x in en_state["signals"])

    assert {d["lang"] for d in uk_state["docs"].values()} == {"uk"}
    assert {d["lang"] for d in en_state["docs"].values()} == {"en"}


def test_metrics_expose_the_economics_of_escalation(client):
    """/metrics має показувати, з чого взявся поріг і що купує ескалація. Інакше
    «людина в контурі» — гасло, а не рішення, яке можна захистити."""
    client.post("/ingest/samples")
    e = client.get("/metrics").json()["economics"]

    assert e["threshold_source"] == "derived"          # не підібраний руками
    assert e["threshold_medication"] > e["threshold_measurement"]
    # $5 за перевірку проти тисяч очікуваної шкоди → ескалація окупається
    assert e["expected_cost"] < e["expected_cost_without_escalation"]
    assert e["escalation_saves"] > 0


def test_graph_endpoint_exposes_provenance_edges(client):
    client.post("/ingest/samples")
    g = client.get("/graph").json()
    types = {n["type"] for n in g["nodes"]}
    assert {"Patient", "Document", "Observation", "MedicationRequest"} <= types
    # факт → документ: провенанс, який і візуалізуємо
    assert any(e["rel"] == "derived_from" for e in g["edges"])
    assert all(n["label"] for n in g["nodes"])     # без None-міток


def test_state_document_language_comes_from_text_not_from_id(client):
    """Мову документа визначає його ТЕКСТ. Раніше інтерфейс вгадував її за назвою
    (`-en` у doc_id) — і англомовне фото, завантажене як `doc-ocr-<hash>`,
    показувалось як українське."""
    client.post("/ingest/samples")
    client.post("/ingest", json={
        "id": "doc-ocr-abc123",           # в id немає жодної підказки про мову
        "text": "AGP Report. Average Glucose 204 mg/dL. Glucose Management Indicator 8.2%.",
        "kind": "lab_report",
    })
    docs = client.get("/state").json()["docs"]
    assert docs["doc-ocr-abc123"]["lang"] == "en"
    assert any(d["lang"] == "uk" for k, d in docs.items() if k != "doc-ocr-abc123")


def test_uncoded_facts_are_surfaced_not_swallowed(client):
    """Факт без LOINC не має таймлайну — але мусить бути видимим. Витягнутий і
    невидимий факт неможливо відрізнити від невитягнутого."""
    from datetime import datetime, timezone

    from livemedcard.dto import Document
    from livemedcard.layers import l2b_stage2_lora

    client.post("/ingest/samples")
    state = api_module._sessions[client.cookies[api_module._SESSION_COOKIE]]
    doc = Document(id="agp", text="# AGP Report", kind="lab_report",
                   received_at=datetime(2025, 6, 13, tzinfo=timezone.utc))
    obs = l2b_stage2_lora.to_observations(
        [{"keyword": "Time CGM Active", "value": "34", "unit": "%", "timestamp": None}],
        doc, "mom-001", extracted_by="test",
    )
    state.card.factstore.add_observations(obs)

    body = client.get("/state").json()
    uncoded = body["uncoded"]
    assert [u["keyword"] for u in uncoded] == ["Time CGM Active"]
    assert uncoded[0]["value"] == 34.0 and uncoded[0]["unit"] == "%"
    assert uncoded[0]["dated"] is False          # дати в документі не було
    # і він НЕ породжує сигналу: порівнювати нема з чим
    assert all("CGM" not in s["detail"] for s in body["signals"])


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


def test_sessions_are_isolated_between_clients():
    """Per-session: різні відвідувачі (різні cookie) мають незалежні картки."""
    import livemedcard.api as api_module

    a = TestClient(api_module.app)
    b = TestClient(api_module.app)
    a.post("/ingest/samples")            # клієнт A наповнює свою картку
    assert a.get("/state").json()["document_count"] == 4
    assert b.get("/state").json()["document_count"] == 0  # B її не бачить
    b.post("/ingest", json={             # B додає свій документ
        "id": "b1", "text": "Креатинін 121 µmol/L.", "kind": "lab_report",
        "received_at": "2024-03-01T00:00:00",
    })
    assert b.get("/state").json()["document_count"] == 1
    assert a.get("/state").json()["document_count"] == 4  # A не зачеплено
    # A скидає свою — B лишається цілим (немає спільного стану)
    a.post("/reset")
    assert a.get("/state").json()["document_count"] == 0
    assert b.get("/state").json()["document_count"] == 1


def test_concurrent_mixed_requests_stay_consistent(client):
    """Мікс конкурентних читань/мутацій не дає 5xx і лишає стан цілісним."""
    client.get("/state")  # прайм cookie → усі потоки в одній сесії (лок-тест)

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


def test_samples_preview_does_not_ingest(client):
    """Попередній перегляд лише показує вхід — картка має лишитися порожньою."""
    r = client.get("/samples?lang=en")
    assert r.status_code == 200
    docs = r.json()["documents"]
    assert len(docs) == 4
    assert all({"id", "kind", "text", "received_at"} <= set(d) for d in docs)
    assert any("warfarin" in d["text"].lower() for d in docs)
    assert client.get("/state").json()["document_count"] == 0


def test_samples_preview_matches_what_gets_ingested(client):
    """Показане журі й завантажене — той самий набір, інакше перегляд бреше."""
    previewed = {d["id"] for d in client.get("/samples?lang=uk").json()["documents"]}
    client.post("/ingest/samples?lang=uk")
    loaded = set(client.get("/state").json()["docs"])
    assert previewed == loaded


def test_sample_image_is_served(client):
    r = client.get("/samples/image")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert len(r.content) > 10_000
