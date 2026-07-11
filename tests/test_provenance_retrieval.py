"""Тести провенанс-інваріанту (1), span-рівневої прив'язки (2) і ретриву (3)."""
from datetime import datetime, timezone

import pytest

from livemedcard.dto import Document, ExtractionResult
from livemedcard.fhir import (
    CodeableConcept,
    Coding,
    Observation,
    Patient,
    Quantity,
    Reference,
    Sys,
)
from livemedcard.layers.l2b_extractor import StubExtractor
from livemedcard.pipeline import LiveMedCard
from livemedcard.retrieval import Passage, chunk, rank
from livemedcard.stores import observation_to_kvt

_RECEIVED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _doc(text: str) -> Document:
    return Document(id="d1", text=text, kind="lab_report", received_at=_RECEIVED)


# ── (1) Провенанс — обов'язковий інваріант на межі екстракції ─────────────────
class _NoProvenanceExtractor:
    """Екстрактор, що (помилково) віддає факт без провенансу."""

    def extract(self, doc, patient_id):
        obs = Observation(
            id="obs-x",
            status="final",
            code=CodeableConcept(
                coding=[Coding(system=Sys.LOINC, code="2160-0", display="Creat")],
                text="Креатинін",
            ),
            subject=Reference(reference=f"Patient/{patient_id}"),
            effectiveDateTime=_RECEIVED,
            valueQuantity=Quantity(value=100.0, unit="µmol/L"),
            # provenance навмисно відсутній
        )
        return ExtractionResult(observations=[obs], mean_confidence=0.9)


def test_ingest_rejects_facts_without_provenance():
    card = LiveMedCard(Patient(id="mom-001"), extractor=_NoProvenanceExtractor())
    with pytest.raises(ValueError, match="Провенанс обов'язковий"):
        card.ingest(_doc("креатинін 100"))


def test_stub_extraction_passes_provenance_invariant():
    card = LiveMedCard(Patient(id="mom-001"))
    card.ingest(_doc("креатинін 95"))  # не має падати
    assert card.observations[0].provenance.source_doc_id == "d1"


# ── (2) source_span — прив'язка триплета до позиції у free-text ───────────────
def test_source_span_slices_the_stored_text():
    ext = StubExtractor().extract(_doc("креатинін 95"), "mom-001")
    obs = ext.observations[0]
    span = obs.provenance.source_span
    assert span is not None
    # span має вирізати саме фрагмент «термін … значення» зі збереженого тексту.
    stored = ext.document_reference.content.data
    fragment = stored[span[0]:span[1]]
    assert "креатинін" in fragment.lower()
    assert "95" in fragment


def test_kvt_fact_carries_span():
    obs = StubExtractor().extract(_doc("креатинін 95"), "mom-001").observations[0]
    kvt = observation_to_kvt(obs)
    assert kvt.source_span == obs.provenance.source_span
    assert kvt.source_doc_id == "d1"


# ── (3) Ретрив: chunk offsets, лексичне/семантичне ранжування, відкат ─────────
def test_chunk_offsets_are_valid_slices_and_skip_noise():
    text = "## Заголовок\nкреатинін 135 (2024-10-15)\nnot stated\n\nкалій 5.1\n"
    passages = chunk("d1", text)
    assert [p.text for p in passages] == ["креатинін 135 (2024-10-15)", "калій 5.1"]
    for p in passages:  # span — валідний зріз того ж тексту
        assert text[p.span[0]:p.span[1]] == p.text


def test_lexical_rank_prefers_more_overlap():
    passages = [
        Passage(text="креатинін 135 вище норми", doc_id="d1", span=(0, 24)),
        Passage(text="калій 5.1 у нормі", doc_id="d1", span=(25, 42)),
    ]
    hits = rank("який останній креатинін", passages)
    assert hits[0].text.startswith("креатинін")


def test_semantic_rank_uses_injected_embedder():
    passages = [
        Passage(text="креатинін 135", doc_id="d1", span=(0, 13)),
        Passage(text="калій 5.1", doc_id="d1", span=(14, 23)),
    ]
    table = {"калій зараз": [1.0, 0.0], "калій 5.1": [1.0, 0.0], "креатинін 135": [0.0, 1.0]}

    def fake_embed(texts):
        return [table.get(t, [0.0, 0.0]) for t in texts]

    hits = rank("калій зараз", passages, embed=fake_embed)
    assert hits[0].text == "калій 5.1"  # семантично найближче, попри інший порядок


def test_rank_falls_back_to_lexical_on_embedder_error():
    passages = [Passage(text="креатинін 135", doc_id="d1", span=(0, 13))]

    def boom(texts):
        raise RuntimeError("сервер embeddings недоступний")

    hits = rank("креатинін", passages, embed=boom)  # не має підняти виняток
    assert hits and hits[0].text == "креатинін 135"


def test_qa_freetext_uses_ranked_retrieval():
    from livemedcard.qa import QA

    card = LiveMedCard(Patient(id="mom-001"))
    card.ingest(_doc("Пацієнт скаржиться на головний біль після навантаження"))
    qa = QA(card.factstore, card.graph)
    ans = qa.ask("розкажи про головний біль")  # не лаб/ліки → freetext-гілка
    assert "головний" in ans.text.lower()
    assert ans.sources == ["d1"]
