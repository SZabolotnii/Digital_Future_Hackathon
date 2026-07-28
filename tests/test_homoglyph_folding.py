"""Русифікована OCR-транскрипція не має губити показник — і не має підміняти його.

East-Slavic розпізнавач PP-OCR систематично пише українські терміни російськими
літерами («Креатинин», «Калий», «Гликований гемоглобин»). Словник шукає точний
збіг, тож без згортання омоглифів такий показник просто зникає з картки.

Найважливіший тест тут — не той, що показник знайшовся, а той, що він знайшовся
ПРАВИЛЬНИЙ: очевидне «полагодимо згортанням тексту» перетворює HbA1c 7.6 % на
гемоглобін 7.6 в межах норми, тобто міняє пропущений факт на хибно нормальний.
"""
from __future__ import annotations

import json
from datetime import datetime

from livemedcard.config import DATA_DIR
from livemedcard.dto import Document
from livemedcard.layers.l2b_extractor import _fold, deterministic_extract


def _doc(text: str) -> Document:
    return Document(id="ocr", text=text, kind="lab_report",
                    received_at=datetime(2026, 5, 14))


def _codes(text: str) -> dict[str, float]:
    ext = deterministic_extract(text, _doc(text), "p")
    return {o.code.coding[0].code: o.valueQuantity.value
            for o in ext.observations if o.code.coding and o.code.coding[0].code}


# ── те, заради чого згортання існує ────────────────────────────────────────────
def test_russified_analyte_names_still_get_their_loinc():
    codes = _codes("Креатинин 135 µmol/L. Калий 5.1 mmol/L.")
    assert codes == {"2160-0": 135.0, "2823-3": 5.1}


def test_russified_hba1c_does_not_degrade_into_hemoglobin():
    """Підміна коду гірша за втрату факту.

    «Гликований гемоглобин» містить «гемоглобин», і при згортанні самого тексту
    довгий термін перестає збігатися, а короткий — починає. 7.6 % HbA1c (вище
    межі 4.0–6.0) стало б гемоглобіном 7.6 (у межах 120–160 g/L його не видно
    зовсім), і сигнал `range_breach` тихо зник би.
    """
    codes = _codes("Гликований гемоглобин 7.6 %")
    assert codes == {"4548-4": 7.6}
    assert "718-7" not in codes


# ── те, що згортання не сміє зламати ───────────────────────────────────────────
def test_correctly_spelled_ukrainian_is_untouched():
    text = "Аспірин 100 мг на добу. Варфарин 5 мг на добу. Метформін 1000 мг."
    ext = deterministic_extract(text, _doc(text), "p")
    assert {m.medicationCodeableConcept.text for m in ext.medications} == {
        "Аспірин", "Варфарин", "Метформін"}


def test_english_terms_pass_through_folding_unchanged():
    assert _fold("glycated hemoglobin") == "glycated hemoglobin"
    assert _codes("Creatinine 135 umol/L") == {"2160-0": 135.0}


def test_span_still_points_at_the_original_text():
    """Згортання посимвольне, тож offset-и лишаються дійсними проти оригіналу.

    Якби воно змінювало довжину рядка, підсвітка джерела в UI показувала б не те
    місце — а це саме той доказ, на який спирається провенанс.
    """
    text = "Аналіз від 2026-05-14. Креатинин 135 µmol/L."
    ext = deterministic_extract(text, _doc(text), "p")
    span = ext.observations[0].provenance.source_span
    assert text[span[0]:span[1]] == "Креатинин 135 µmol/L"


# ── інваріант словника ─────────────────────────────────────────────────────────
def test_no_two_vocab_terms_collide_after_folding():
    """Згортання не сміє злити два різні терміни в один.

    Поки колізій немає, згортання — це чистий приріст recall. Щойн з'явиться
    пара, що згортається в один рядок, вибір між ними стане випадковим (виграє
    довший, а при рівній довжині — порядок словника), і це має валити збірку, а
    не тихо вирішуватись.
    """
    vocab = json.loads((DATA_DIR / "vocab.json").read_text(encoding="utf-8"))
    for group in ("labs", "medications", "conditions"):
        folded: dict[str, str] = {}
        for term in vocab[group]:
            other = folded.setdefault(_fold(term), term)
            assert other == term, f"{group}: «{term}» і «{other}» згортаються в одне"
