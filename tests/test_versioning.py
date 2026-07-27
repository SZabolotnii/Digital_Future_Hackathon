"""Провенанс версій: чим саме витягнуто факт і за якими правилами він став сигналом.

Це вимоги ст. 12 EU AI Act, зведені до перевірюваних тверджень. Головне з них:
**вміст правил безпеки неможливо змінити так, щоб журнал цього не помітив.**
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import livemedcard.api as api_module
from livemedcard import reference
from livemedcard.config import DATA_DIR
from livemedcard.dto import Document
from livemedcard.fhir import Patient, Provenance
from livemedcard.layers.l2b_extractor import StubExtractor
from livemedcard.pipeline import LiveMedCard
from livemedcard.reference import load_sample_docs, ruleset_version
from livemedcard.versioning import agent, fingerprint


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(api_module, "PERSIST_PATH", "")
    c = TestClient(api_module.app)
    c.post("/reset")
    yield c
    c.post("/reset")


def _card() -> LiveMedCard:
    card = LiveMedCard(Patient(id="p", gender="female", birthDate=date(1950, 1, 1)))
    card.ingest_all([Document(**d) for d in load_sample_docs()])
    return card


# ── набір правил ───────────────────────────────────────────────────────────────
def test_ruleset_version_is_declared_and_logged():
    """Задекларована версія + відбиток вмісту мусять бути в `data/CHANGELOG.md`.

    Якщо цей тест упав після правки правила — це не поломка, а робота журналу:
    підніміть версію у файлі й допишіть запис із новим відбитком.
    """
    version = ruleset_version()
    changelog = (DATA_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    declared, digest = version.split("+")
    assert declared in changelog, f"версія {declared} не описана в CHANGELOG"
    assert digest in changelog, (
        f"вміст правил змінився: відбиток {digest} не описаний у CHANGELOG"
    )


def test_ruleset_version_tracks_content_not_just_the_declared_number():
    """Відбиток має реагувати на зміну вмісту — інакше версія нічого не гарантує."""
    reference._raw.cache_clear()
    reference.ruleset_version.cache_clear()
    before = ruleset_version()

    original = reference._raw
    doctored = dict(original("dose_limits.json"))
    doctored["limits"] = [{**doctored["limits"][0], "max_daily": 1.0}]

    reference.ruleset_version.cache_clear()
    reference._raw = lambda n: doctored if n == "dose_limits.json" else original(n)
    try:
        assert ruleset_version() != before
    finally:
        reference._raw = original
        reference.ruleset_version.cache_clear()
        reference._raw.cache_clear()


def test_ruleset_version_covers_only_safety_rules():
    """`vocab.json` впливає на витяг, не на рішення про небезпеку — окрема версія."""
    assert "vocab.json" not in reference._RULESET_FILES


def test_signals_carry_the_ruleset_version():
    card = _card()
    signals = card.current_signals()
    assert signals
    assert all(s.ruleset_version == ruleset_version() for s in signals)


# ── провенанс витягу ───────────────────────────────────────────────────────────
def test_provenance_requires_naming_the_extractor():
    """Дефолт `"MedGemma-4B"` називав не той агент незалежно від того, що працювало.
    Провенанс, який виглядає перевіреним і бреше, гірший за відсутній."""
    with pytest.raises(Exception):
        Provenance(source_doc_id="d1")


def test_stub_names_itself_and_declares_no_prompt():
    doc = Document(id="d", text="Креатинін 121 µmol/L.", kind="lab_report",
                   received_at=datetime(2024, 1, 1))
    res = StubExtractor().extract(doc, "p1")
    prov = res.observations[0].provenance
    assert prov.extracted_by == "StubExtractor"
    assert prov.prompt_id is None  # детермінований шлях — промпту немає


def test_agent_identifier_carries_the_actual_model():
    assert agent("two_stage", "medgemma-1.5-4b-it") == "two_stage@medgemma-1.5-4b-it"
    assert agent("StubExtractor") == "StubExtractor"


# ── дві іпостасі документа (ст. 13) ────────────────────────────────────────────
def test_document_keeps_the_original_apart_from_what_the_model_wrote(client):
    """Регресія: для двоетапних екстракторів у вузол-документ лягало Stage-1
    summary, і UI показував його під заголовком «Джерело». Якби Stage 1
    галюцинував значення, підсвічений «доказ» був би самою галюцинацією."""
    raw = "Креатинін 121 µmol/L. Пацієнтка скаржиться на набряки."
    client.post("/ingest", json={"id": "d1", "text": raw, "kind": "lab_report",
                                 "received_at": "2024-05-01T00:00:00"})
    d = client.get("/document/d1").json()
    assert d["text"] == raw
    # Детермінований stub моделі не кличе — генерованої іпостасі бути не може.
    assert d["generated"] == ""
    assert d["span_base"] == "text"


def test_original_survives_snapshot_and_bundle():
    card = _card()
    doc_id = next(iter(card.documents))
    original = card.graph.g.nodes[f"Document/{doc_id}"]["text"]
    assert original

    from livemedcard.bundle import from_bundle, to_bundle
    for restored in (LiveMedCard.from_snapshot(card.snapshot()),
                     from_bundle(to_bundle(card))):
        assert restored.graph.g.nodes[f"Document/{doc_id}"]["text"] == original


def test_generated_text_is_flagged_by_the_presence_of_a_prompt():
    """Підпис «написане моделлю» — це факт із провенансу (`prompt_id`), а не
    здогадка інтерфейсу за назвою екстрактора."""
    from livemedcard.fhir import Attachment, DocumentReference, Reference

    card = LiveMedCard(Patient(id="p", gender="female", birthDate=date(1950, 1, 1)))
    card.graph.add_document("d9", "оригінал як подано")
    card._absorb_docref(DocumentReference(
        id="docref-d9", status="current",
        subject=Reference(reference="Patient/p"),
        content=Attachment(contentType="text/plain", data="переказ моделі"),
        provenance=Provenance(source_doc_id="d9", extracted_by="two_stage@m",
                              prompt_id="abcd1234"),
    ))
    node = card.graph.g.nodes["Document/d9"]
    assert node["text"] == "оригінал як подано"
    assert node["generated_text"] == "переказ моделі"


# ── RCR і ETC ──────────────────────────────────────────────────────────────────
def test_rule_coverage_falls_when_a_fact_has_no_rule(client):
    """Натрій є у словнику витягу, але референсних меж для нього немає. Система
    мусить сказати це числом, а не показувати спокійний показник, для якого
    норми просто не існує."""
    client.post("/ingest/samples")
    full = client.get("/metrics").json()["rule_coverage"]
    assert full["lab_coverage"] == 1.0

    client.post("/ingest", json={"id": "d-na", "text": "Натрій 141 mmol/L.",
                                 "kind": "lab_report", "received_at": "2024-12-01T00:00:00"})
    after = client.get("/metrics").json()["rule_coverage"]
    assert after["labs_total"] == full["labs_total"] + 1
    assert after["labs_with_reference_range"] == full["labs_with_reference_range"]
    assert after["lab_coverage"] < 1.0


def test_evidence_trail_is_complete_on_the_demo_card(client):
    client.post("/ingest/samples")
    ev = client.get("/metrics").json()["evidence"]
    assert ev["facts"] > 0 and ev["fact_trail"] == 1.0
    assert ev["signals"] > 0 and ev["signal_trail"] == 1.0
    assert ev["ruleset_version"] == ruleset_version()


def test_prompt_fingerprints_are_stable_and_distinct():
    from livemedcard.layers import l2b_nuextract, l2b_stage1, l2b_stage2_lora

    ids = {l2b_stage1.prompt_id(), l2b_stage2_lora.prompt_id(), l2b_nuextract.prompt_id()}
    assert len(ids) == 3                      # різні промпти — різні відбитки
    assert l2b_stage1.prompt_id() == l2b_stage1.prompt_id()   # стабільний
    assert fingerprint("abc") != fingerprint("abd")
