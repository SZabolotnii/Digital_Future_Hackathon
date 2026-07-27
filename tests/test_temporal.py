"""Тести темпорального шару: інтервали, сутності, епізоди, метрики, запити.

Окремо перевіряється межа детермінізму: `temporal.py` описовий і сигналів не
породжує; сигнали живуть у `l2a_temporal` і рахуються з фактів.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from livemedcard import temporal
from livemedcard.dto import Document
from livemedcard.fhir import (
    CodeableConcept,
    Coding,
    Condition,
    MedicationRequest,
    Observation,
    Patient,
    Provenance,
    Quantity,
    Reference,
    Sys,
)
from livemedcard.layers import l2a_temporal
from livemedcard.pipeline import LiveMedCard
from livemedcard.qa import QA
from livemedcard.reference import load_sample_docs
from livemedcard.temporal import Interval, build, entity_key

UTC = timezone.utc


def _dt(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _obs(oid: str, loinc: str, value: float, when: datetime, doc: str = "d1",
         unit: str = "µmol/L", label: str = "Креатинін") -> Observation:
    return Observation(
        id=oid,
        status="final",
        code=CodeableConcept(coding=[Coding(system=Sys.LOINC, code=loinc)], text=label),
        effectiveDateTime=when,
        valueQuantity=Quantity(value=value, unit=unit),
        provenance=Provenance(source_doc_id=doc, extracted_at=when),
    )


def _med(mid: str, rxnorm: str, label: str, doc: str = "d1", status: str = "active",
         reason: str | None = None) -> MedicationRequest:
    return MedicationRequest(
        id=mid,
        status=status,
        intent="order",
        medicationCodeableConcept=CodeableConcept(
            coding=[Coding(system=Sys.RXNORM, code=rxnorm)], text=label
        ),
        subject=Reference(reference="Patient/p1"),
        reason=Reference(reference=f"Condition/{reason}") if reason else None,
        provenance=Provenance(source_doc_id=doc),
    )


def _cond(cid: str, code: str, label: str, doc: str = "d1",
          onset: datetime | None = None) -> Condition:
    return Condition(
        id=cid,
        subject=Reference(reference="Patient/p1"),
        code=CodeableConcept(coding=[Coding(system=Sys.SNOMED, code=code)], text=label),
        clinicalStatus="active",
        onsetDateTime=onset,
        provenance=Provenance(source_doc_id=doc),
    )


def _sample_card(lang: str = "uk") -> LiveMedCard:
    card = LiveMedCard(Patient(id="mom-001", gender="female"))
    card.ingest_all([Document(**d) for d in load_sample_docs(lang)])
    return card


# ── інтервали ─────────────────────────────────────────────────────────────────
def test_open_interval_contains_any_future_point():
    iv = Interval(start=_dt(2024, 1, 1))
    assert iv.contains(_dt(2030, 1, 1))
    assert not iv.contains(_dt(2023, 12, 31))
    assert iv.duration_days is None  # довжина відкритого інтервалу невідома


def test_intervals_overlap_only_when_they_actually_meet():
    a = Interval(start=_dt(2024, 1, 1), end=_dt(2024, 3, 1))
    b = Interval(start=_dt(2024, 2, 1), end=_dt(2024, 4, 1))
    c = Interval(start=_dt(2024, 5, 1), end=_dt(2024, 6, 1))
    assert a.overlaps(b) and b.overlaps(a)
    assert not a.overlaps(c)
    # відкритий інтервал дотягується до будь-чого пізнішого
    assert Interval(start=_dt(2024, 1, 1)).overlaps(c)


def test_naive_datetime_is_normalised_not_rejected():
    """Джерела дають різне: семпли — naive, екстрактори — aware."""
    iv = Interval(start=datetime(2024, 1, 1))
    assert iv.contains(datetime(2024, 6, 1))


# ── сутності ──────────────────────────────────────────────────────────────────
def test_repeated_measurements_collapse_into_one_entity():
    """Чотири виміри креатиніну — одна сутність, а не чотири вузли."""
    obs = [
        _obs("o1", "2160-0", 88, _dt(2023, 5, 1)),
        _obs("o2", "2160-0", 104, _dt(2023, 11, 3)),
        _obs("o3", "2160-0", 135, _dt(2024, 10, 15)),
    ]
    tg = build(obs, [], [], document_dates={"d1": _dt(2023, 5, 1)})
    assert len(tg.entities) == 1
    e = tg.entities["analyte:2160-0"]
    assert e.occurrences == 3
    assert e.first_valid == _dt(2023, 5, 1)
    assert e.last_valid == _dt(2024, 10, 15)


def test_fact_without_code_still_enters_graph_under_its_label():
    """Витягнутий і невидимий факт неможливо відрізнити від невитягнутого."""
    o = Observation(
        id="o1", status="final",
        code=CodeableConcept(text="Час у діапазоні"),
        effectiveDateTime=_dt(2024, 1, 1),
        valueQuantity=Quantity(value=62, unit="%"),
        provenance=Provenance(source_doc_id="d1"),
    )
    tg = build([o], [], [], document_dates={"d1": _dt(2024, 1, 1)})
    assert entity_key("analyte", None, "Час у діапазоні") in tg.entities


def test_uncoded_labels_do_not_merge_with_coded_entities():
    """Без коду ототожнювати «Креатинін» із 2160-0 не можна."""
    coded = _obs("o1", "2160-0", 88, _dt(2024, 1, 1))
    uncoded = Observation(
        id="o2", status="final", code=CodeableConcept(text="Креатинін"),
        effectiveDateTime=_dt(2024, 1, 2), valueQuantity=Quantity(value=90),
        provenance=Provenance(source_doc_id="d1"),
    )
    tg = build([coded, uncoded], [], [], document_dates={"d1": _dt(2024, 1, 1)})
    assert len(tg.entities) == 2


# ── дві осі часу ──────────────────────────────────────────────────────────────
def test_valid_and_record_time_are_tracked_separately():
    """Заднім числом поданий документ: клінічно факт торішній, у картці — свіжий."""
    clinical, recorded = _dt(2023, 1, 1), _dt(2024, 6, 1)
    o = _obs("o1", "2160-0", 88, clinical)
    o = o.model_copy(update={
        "provenance": Provenance(source_doc_id="d1", extracted_at=recorded)
    })
    tg = build([o], [], [], document_dates={"d1": clinical})
    e = tg.entities["analyte:2160-0"]
    assert e.first_valid == clinical and e.first_recorded == recorded
    assert tg.first_appearance("analyte:2160-0", axis="valid") == clinical
    assert tg.first_appearance("analyte:2160-0", axis="record") == recorded
    # станом на 2023 клінічно факт уже був, а картка про нього ще не знала
    assert tg.as_of(_dt(2023, 6, 1), axis="valid")
    assert not tg.as_of(_dt(2023, 6, 1), axis="record")


def test_document_with_own_date_counts_as_anchored():
    """Дата не в тексті, але документ має власну — таймлайн коректний."""
    card = _sample_card()
    assert card.temporal().metrics().anchored_fraction == 1.0


def test_document_without_date_is_unanchored():
    """Обидві осі показують момент обробки — дату поставила система."""
    card = LiveMedCard(Patient(id="p1", gender="female"))
    card.ingest(Document(id="x", text="Креатинін 121 µmol/L. Калій 5.2 mmol/L.",
                         kind="lab_report"))
    assert card.temporal().metrics().anchored_fraction == 0.0


# ── епізоди ───────────────────────────────────────────────────────────────────
def test_gap_larger_than_threshold_opens_new_episode():
    obs = [
        _obs("o1", "2160-0", 88, _dt(2024, 1, 1)),
        _obs("o2", "2823-3", 4.6, _dt(2024, 1, 5), unit="mmol/L", label="Калій"),
        _obs("o3", "4548-4", 6.4, _dt(2024, 9, 1), unit="%", label="HbA1c"),
    ]
    tg = build(obs, [], [], document_dates={"d1": _dt(2024, 1, 1)}, gap_days=90)
    assert len(tg.episodes) == 2
    assert len(tg.episodes[0].new_entity_keys) == 2
    assert len(tg.episodes[1].new_entity_keys) == 1


def test_episode_threshold_changes_segmentation():
    """Сегментація евристична — на різних порогах картина різна, і це видно."""
    obs = [
        _obs("o1", "2160-0", 88, _dt(2024, 1, 1)),
        _obs("o2", "4548-4", 6.4, _dt(2024, 4, 1), unit="%", label="HbA1c"),
    ]
    dates = {"d1": _dt(2024, 1, 1)}
    assert len(build(obs, [], [], document_dates=dates, gap_days=30).episodes) == 2
    assert len(build(obs, [], [], document_dates=dates, gap_days=180).episodes) == 1


def test_entity_counted_new_only_in_its_first_episode():
    obs = [
        _obs("o1", "2160-0", 88, _dt(2024, 1, 1)),
        _obs("o2", "2160-0", 135, _dt(2024, 9, 1)),
    ]
    tg = build(obs, [], [], document_dates={"d1": _dt(2024, 1, 1)}, gap_days=90)
    assert tg.episodes[0].new_entity_keys == ("analyte:2160-0",)
    assert tg.episodes[1].new_entity_keys == ()


def test_empty_card_has_no_episodes():
    tg = build([], [], [])
    assert tg.episodes == [] and tg.metrics().n_entities == 0


# ── ребра ─────────────────────────────────────────────────────────────────────
def test_treats_edge_is_read_from_reason_not_inferred():
    cond = _cond("c1", "49436004", "фібриляція передсердь", onset=_dt(2023, 5, 1))
    med = _med("m1", "11289", "Варфарин", reason="c1")
    tg = build([], [cond], [med], document_dates={"d1": _dt(2023, 5, 1)})
    treats = [e for e in tg.edges if e.rel == "treats"]
    assert len(treats) == 1
    assert treats[0].basis == "MedicationRequest.reason"
    assert treats[0].target == entity_key("problem", "49436004", "фібриляція передсердь")


def test_no_treats_edge_without_reason():
    """Зв'язок «препарат лікує стан» зчитується, а не вгадується."""
    cond = _cond("c1", "49436004", "фібриляція передсердь", onset=_dt(2023, 5, 1))
    med = _med("m1", "11289", "Варфарин")
    tg = build([], [cond], [med], document_dates={"d1": _dt(2023, 5, 1)})
    assert not [e for e in tg.edges if e.rel == "treats"]


def test_co_active_edge_for_overlapping_medications():
    meds = [_med("m1", "11289", "Варфарин"), _med("m2", "1191", "Аспірин")]
    tg = build([], [], meds, document_dates={"d1": _dt(2023, 5, 1)})
    assert len(tg.co_active_pairs()) == 1


def test_stopped_medication_closes_its_interval():
    med = _med("m1", "11289", "Варфарин", status="stopped")
    tg = build([], [], [med], document_dates={"d1": _dt(2023, 5, 1)})
    entity = tg.entities["drug:11289"]
    assert entity.interval is not None and entity.interval.end is not None
    assert not tg.concurrent_medications()


def test_precedes_edges_are_not_produced():
    """Свідоме рішення: порядок у часі не подається як ребро (див. ADR)."""
    card = _sample_card()
    assert {e.rel for e in card.temporal().edges} <= {"treats", "co_active"}


# ── метрики ───────────────────────────────────────────────────────────────────
def test_max_concurrent_counts_overlap_not_list_length():
    """П'ять призначень за роки — не поліфармація; п'ять одночасних — так."""
    early = _med("m1", "11289", "Варфарин", doc="d1", status="stopped")
    late = _med("m2", "1191", "Аспірин", doc="d2")
    tg = build([], [], [early, late],
               document_dates={"d1": _dt(2020, 1, 1), "d2": _dt(2024, 1, 1)})
    assert tg.metrics().max_concurrent_medications == 1


def test_medications_from_one_document_count_as_concurrent():
    """Закриття інтервалу не має «гасити» відкриття тієї самої миті."""
    meds = [_med(f"m{i}", str(1000 + i), f"Препарат {i}") for i in range(3)]
    tg = build([], [], meds, document_dates={"d1": _dt(2024, 1, 1)})
    assert tg.metrics().max_concurrent_medications == 3


def test_demo_card_metrics_match_the_documents():
    tg = _sample_card().temporal()
    m = tg.metrics()
    assert m.n_analytes == 4 and m.n_drugs == 3 and m.n_problems == 2
    assert m.n_episodes == 4
    assert m.novelty_by_episode == [4, 1, 3, 1]
    assert m.multimorbidity == 2


# ── запити ────────────────────────────────────────────────────────────────────
def test_since_returns_only_later_entities():
    tg = _sample_card().temporal()
    fresh = tg.since(_dt(2024, 6, 1))
    assert [e.key for e in fresh] == ["analyte:2345-7"]


def test_episode_at_locates_the_right_segment():
    tg = _sample_card().temporal()
    # Межі епізоду — точні мітки часу документів, а не доба цілком: перший
    # документ поданий о 10:00, тож опівночі того ж дня епізод ще не почався.
    assert tg.episode_at(tg.episodes[0].start).index == 0
    assert tg.episode_at(_dt(2023, 8, 1)) is None  # у розриві між епізодами


def test_horizon_is_the_freshest_evidence():
    tg = _sample_card().temporal()
    assert tg.horizon == _dt(2024, 10, 15).replace(hour=11)


# ── сигнали L2a-temporal ──────────────────────────────────────────────────────
def test_polypharmacy_fires_above_threshold():
    meds = [_med(f"m{i}", str(2000 + i), f"Препарат {i}") for i in range(5)]
    tg = build([], [], meds, document_dates={"d1": _dt(2024, 1, 1)})
    signals = l2a_temporal.detect_polypharmacy(tg)
    assert len(signals) == 1 and signals[0].kind == "polypharmacy"
    assert signals[0].confidence == 1.0  # детермінований


def test_polypharmacy_silent_below_threshold():
    meds = [_med(f"m{i}", str(2000 + i), f"Препарат {i}") for i in range(4)]
    tg = build([], [], meds, document_dates={"d1": _dt(2024, 1, 1)})
    assert l2a_temporal.detect_polypharmacy(tg) == []


def test_monitoring_gap_fires_for_stale_out_of_range_value():
    card = LiveMedCard(Patient(id="p1", gender="female"))
    card.ingest(Document(id="d1", text="Креатинін 135 µmol/L.", kind="lab_report",
                         received_at=_dt(2023, 1, 1)))
    signals = l2a_temporal.detect_monitoring_gaps(
        card.factstore, card.temporal(), "female", now=_dt(2024, 6, 1)
    )
    assert [s.kind for s in signals] == ["monitoring_gap"]
    assert "не перемірювався" in signals[0].detail


def test_monitoring_gap_silent_for_recent_value():
    card = LiveMedCard(Patient(id="p1", gender="female"))
    card.ingest(Document(id="d1", text="Креатинін 135 µmol/L.", kind="lab_report",
                         received_at=_dt(2024, 5, 1)))
    assert l2a_temporal.detect_monitoring_gaps(
        card.factstore, card.temporal(), "female", now=_dt(2024, 6, 1)
    ) == []


def test_monitoring_gap_silent_for_in_range_value():
    """Старе значення В МЕЖАХ норми — не привід турбувати."""
    card = LiveMedCard(Patient(id="p1", gender="female"))
    card.ingest(Document(id="d1", text="Креатинін 70 µmol/L.", kind="lab_report",
                         received_at=_dt(2020, 1, 1)))
    assert l2a_temporal.detect_monitoring_gaps(
        card.factstore, card.temporal(), "female", now=_dt(2024, 6, 1)
    ) == []


def test_demo_card_raises_no_false_temporal_alarms():
    """Коректний демо-таймлайн не має давати ані розривів, ані сумніву в датах."""
    kinds = {s.kind for s in _sample_card().current_signals()}
    assert "temporal_uncertainty" not in kinds
    assert "monitoring_gap" not in kinds


def test_timeline_uncertainty_fires_on_unanchored_card():
    card = LiveMedCard(Patient(id="p1", gender="female"))
    card.ingest(Document(id="x", text="Креатинін 121 µmol/L. Калій 5.2 mmol/L. "
                                      "Глюкоза 7.0 mmol/L.", kind="lab_report"))
    signals = l2a_temporal.detect_timeline_uncertainty(card.temporal())
    assert [s.kind for s in signals] == ["temporal_uncertainty"]
    assert signals[0].severity == "info"  # епістемічний, не клінічний


@pytest.mark.parametrize("lang,needle", [("uk", "препаратів"), ("en", "medications")])
def test_temporal_signals_are_bilingual(lang, needle):
    meds = [_med(f"m{i}", str(2000 + i), f"Drug {i}") for i in range(5)]
    tg = build([], [], meds, document_dates={"d1": _dt(2024, 1, 1)})
    assert needle in l2a_temporal.detect_polypharmacy(tg, lang)[0].detail


# ── межа детермінізму ─────────────────────────────────────────────────────────
def test_temporal_module_does_not_import_signal():
    """Інваріант: `temporal.py` описовий і сигналів не породжує.

    Перевіряємо не текст (у докстрінгу слово «Signal» цілком доречне), а
    фактичні імпорти через AST: граф лишається навігацією й провенансом, а шлях
    безпеки — окремим модулем.
    """
    import ast
    import pathlib

    assert not hasattr(temporal, "Signal")
    tree = ast.parse(pathlib.Path(temporal.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
    assert "Signal" not in imported
    assert not any(m.endswith("dto") for m in modules)
    assert not any("l1_safety" in m or "l2a" in m for m in modules)


def test_temporal_signals_carry_full_confidence():
    """Усе, що приходить з L2a, детерміноване — confidence == 1.0."""
    card = _sample_card()
    temporal_kinds = {"polypharmacy", "monitoring_gap", "temporal_uncertainty"}
    for s in card.current_signals():
        if s.kind in temporal_kinds:
            assert s.confidence == 1.0 and s.layer == "L2a"


# ── інтеграція ────────────────────────────────────────────────────────────────
def test_temporal_survives_snapshot_round_trip():
    """Без дат документів відновлена картка «злипається» в один епізод."""
    card = _sample_card()
    restored = LiveMedCard.from_snapshot(card.snapshot())
    assert restored.temporal().metrics().n_episodes == card.temporal().metrics().n_episodes
    assert restored.temporal().horizon == card.temporal().horizon


def test_temporal_survives_bundle_round_trip():
    from livemedcard.bundle import from_bundle, to_bundle

    card = _sample_card()
    restored = from_bundle(to_bundle(card))
    before = card.temporal().metrics().model_dump()
    assert restored.temporal().metrics().model_dump() == before


def test_snapshot_from_older_version_without_dates_still_loads():
    """Старі знімки поля `document_dates` не мають — це не має бути помилкою."""
    snap = _sample_card().snapshot()
    snap.pop("document_dates")
    restored = LiveMedCard.from_snapshot(snap)
    assert restored.temporal().metrics().n_entities > 0


def test_payload_is_json_serialisable():
    import json

    payload = _sample_card().temporal().to_payload()
    json.dumps(payload)  # не має кидати
    assert {"entities", "edges", "episodes", "metrics", "horizon"} <= payload.keys()


# ── Q&A ───────────────────────────────────────────────────────────────────────
def _qa(card: LiveMedCard) -> QA:
    return QA(card.factstore, card.graph, card.current_signals(),
              sex=card.patient.gender, temporal=card.temporal())


def test_qa_answers_when_did_it_first_appear():
    answer = _qa(_sample_card()).ask("Коли вперше з'явився креатинін?")
    assert "2023-05-01" in answer.text
    assert answer.sources  # відповідь із провенансом


def test_qa_temporal_intent_wins_over_lab_intent():
    """«Коли вперше підвищився креатинін» просить дату, а не значення."""
    answer = _qa(_sample_card()).ask("Коли вперше підвищився креатинін?")
    assert "вперше з'являється" in answer.text
    assert "останнє значення" not in answer.text.lower()


def test_qa_answers_what_is_new():
    answer = _qa(_sample_card()).ask("Що нового останнім часом?")
    assert "Глюкоза" in answer.text
    # Джерело веде до документа НОВОГО факту, а не до всіх, що дожили до епізоду.
    assert answer.sources == ["doc-2024-10"]


def test_qa_history_without_entity_lists_episodes():
    answer = _qa(_sample_card()).ask("Розкажи історію картки")
    assert "епізоди" in answer.text and "2023-05-01" in answer.text


def test_qa_warns_when_chronology_is_unanchored():
    """Часова відповідь на вигаданих датах не має виглядати впевнено."""
    card = LiveMedCard(Patient(id="p1", gender="female"))
    card.ingest(Document(id="x", text="Креатинін 121 µmol/L. Калій 5.2 mmol/L.",
                         kind="lab_report"))
    answer = _qa(card).ask("Коли вперше з'явився креатинін?")
    assert "хронологія приблизна" in answer.text


def test_lab_question_with_time_range_is_not_hijacked_by_change_intent():
    """«Що з нирками за останній рік» — про нирки, а не «що нового»."""
    answer = _qa(_sample_card()).ask("Що з нирками за останній рік?")
    assert "Креатинін" in answer.text and "останнє значення" in answer.text.lower()


def test_all_ui_chips_route_sensibly():
    """Рядки-підказки з інтерфейсу не мають потрапляти у fallback."""
    qa = _qa(_sample_card())
    chips = [
        "Що з нирками за останній рік?",
        "Коли вперше з’явився креатинін?",
        "Які ліки приймає і чи є взаємодії?",
        "Що нового останнім часом?",
    ]
    for chip in chips:
        text = qa.ask(chip).text
        assert "Не вдалося зіставити" not in text, chip


def test_qa_without_temporal_graph_behaves_as_before():
    """Темпоральна проєкція необов'язкова — без неї Q&A працює як раніше."""
    card = _sample_card()
    qa = QA(card.factstore, card.graph, card.current_signals(), sex="female")
    assert "останнє значення" in qa.ask("Коли вперше з'явився креатинін?").text.lower()


def test_qa_english_temporal_answer_uses_english_label():
    card = _sample_card("uk")  # документи українські, питання англійське
    answer = _qa(card).ask("When did creatinine first appear?")
    assert "Creatinine" in answer.text and "first appears" in answer.text


def test_qa_days_use_ukrainian_plural_forms():
    """«533 днів» одразу видає машинний текст — має бути «533 дні»."""
    from livemedcard.qa import _plural

    forms = ("день", "дні", "днів")
    assert _plural(1, "uk", forms, "days") == "день"
    assert _plural(3, "uk", forms, "days") == "дні"
    assert _plural(5, "uk", forms, "days") == "днів"
    assert _plural(11, "uk", forms, "days") == "днів"  # 11-14 — виняток
    assert _plural(533, "uk", forms, "days") == "дні"
    assert _plural(533, "en", forms, "days") == "days"


# ── API ───────────────────────────────────────────────────────────────────────
def test_temporal_endpoint_returns_projection():
    from fastapi.testclient import TestClient

    from livemedcard.api import app

    client = TestClient(app)
    client.post("/ingest/samples?lang=uk")
    payload = client.get("/temporal").json()
    assert payload["metrics"]["n_episodes"] == 4
    assert payload["entities"] and payload["horizon"]


def test_temporal_endpoint_on_empty_card():
    from fastapi.testclient import TestClient

    from livemedcard.api import app

    client = TestClient(app)
    client.post("/reset")
    payload = client.get("/temporal").json()
    assert payload["entities"] == [] and payload["metrics"]["n_entities"] == 0


def test_time_ordering_is_stable_across_builds():
    """Двічі побудований граф має давати ідентичний порядок сутностей."""
    card = _sample_card()
    first = [e.key for e in card.temporal().as_of(datetime.now(UTC) + timedelta(days=1))]
    second = [e.key for e in card.temporal().as_of(datetime.now(UTC) + timedelta(days=1))]
    assert first == second
