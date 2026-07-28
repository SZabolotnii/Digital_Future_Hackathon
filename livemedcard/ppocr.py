"""Stage 1 (зображення → текст) через PP-OCR: детекція рядків + розпізнавання.

Чим цей драйвер відрізняється від інших: він віддає **геометрію** — бокс на
кожен рядок, а не вже згрупований текст. Для лабораторної таблиці це вирішальне.
Apple Vision на українському бланку транскрибує майже бездоганно (19–20 з 20
еталонних значень) і дає **нуль** кодованих показників, бо групує таблицю по
колонках: спершу всі назви, потім усі значення. «Креатинін» і «135» опиняються
за п'ять рядків один від одного, а `_LAB_VALUE_RE` шукає значення впритул до
терміна. Відновити рядок із таких блоків нема з чого — а з боксів можна
(`_rows_from_boxes`), і тоді ті самі показники доходять до картки.

Заміри — `docs/ocr_stage1_benchmark.md`. Межа TRACE не зачіпається: цей шар лише
транскрибує, висновків про безпеку не робить.
"""
from __future__ import annotations

import base64
import binascii
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import PPOCR_LANG, PPOCR_TIER

# PP-OCRv6 покриває 50 мов — китайську, англійську, японську й латиницю, але
# НЕ кирилицю. `PaddleOCR(lang="uk")` мовчки підставляє інший, старіший конвеєр;
# мовчки — це неприйнятно там, де провенанс має називати те, що справді
# працювало. Тому моделі обираються явно, а `engine_id()` повертає їхні імена.
_CYRILLIC_LANGS = frozenset({"uk", "ru", "be", "bg"})
# Кириличний розпізнавач — мобільний PP-OCRv5 (східнослов'янський). Детектор
# лишаємо з v6: на тому самому документі зв'язка v6_det+eslav_rec дає те саме
# покриття за 6.7 с проти 16.8 с у дефолтного PP-OCRv5_server_det.
_CYRILLIC_REC = "eslav_PP-OCRv5_mobile_rec"


class PpOcrError(Exception):
    """PP-OCR недоступний або не зміг прочитати зображення."""


def model_names(lang: str = PPOCR_LANG, tier: str = PPOCR_TIER) -> tuple[str, str]:
    """Імена моделей детекції й розпізнавання — те, що піде в провенанс."""
    det = f"PP-OCRv6_{tier}_det"
    return det, (_CYRILLIC_REC if lang in _CYRILLIC_LANGS
                 else f"PP-OCRv6_{tier}_rec")


def engine_id(lang: str = PPOCR_LANG, tier: str = PPOCR_TIER) -> str:
    det, rec = model_names(lang, tier)
    return f"{det}+{rec}"


@lru_cache(maxsize=2)
def _engine(lang: str, tier: str) -> Any:
    """Ініціалізований рушій (моделі вантажаться раз на процес)."""
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:  # немає важкої опційної залежності
        raise PpOcrError(
            "PP-OCR не встановлено. `uv sync --extra ppocr` "
            "(paddleocr + paddlepaddle, ~30 МБ ваг для тиру small)."
        ) from exc

    det, rec = model_names(lang, tier)
    try:
        return PaddleOCR(
            text_detection_model_name=det,
            text_recognition_model_name=rec,
            # Ці три кроки — про орієнтацію й розгинання сторінки; вони коштують
            # часу, а наші документи вже приходять сторінкою вгору.
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    except Exception as exc:
        raise PpOcrError(f"не вдалося підняти PP-OCR ({engine_id(lang, tier)}): {exc}") from exc


def _rows_from_boxes(polys: Any, texts: Any) -> str:
    """Складання рядків із боксів: що перекривається по вертикалі — один рядок.

    Саме тут з'являється те, чого немає в готовому тексті інших рушіїв: назва
    показника і його значення повертаються на один рядок, і детермінований
    Stage 2 може взяти значення впритул до терміна.
    """
    items = []
    for poly, text in zip(polys, texts):
        ys = [float(p[1]) for p in poly]
        xs = [float(p[0]) for p in poly]
        items.append({"y0": min(ys), "y1": max(ys), "x": min(xs), "t": text})
    items.sort(key=lambda i: i["y0"])

    rows: list[list[dict]] = []
    for item in items:
        middle = (item["y0"] + item["y1"]) / 2
        for row in rows:
            if row[0]["y0"] <= middle <= row[0]["y1"]:
                row.append(item)
                break
        else:
            rows.append([item])
    return "\n".join(" ".join(c["t"] for c in sorted(row, key=lambda c: c["x"]))
                     for row in rows)


def image_to_text(base64_image: str, *, lang: str = PPOCR_LANG,
                  tier: str = PPOCR_TIER) -> str:
    """Транскрипція зображення документа в текст із відновленими рядками."""
    if "," in base64_image:  # data URL prefix
        base64_image = base64_image.split(",", 1)[1]
    try:
        raw = base64.b64decode(base64_image, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PpOcrError("зображення не є коректним base64") from exc

    engine = _engine(lang, tier)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "page.png"
        path.write_bytes(raw)
        try:
            results = engine.predict(str(path))
        except Exception as exc:
            raise PpOcrError(f"PP-OCR не прочитав зображення: {exc}") from exc

    pages = []
    for result in results:
        res = result.json["res"] if hasattr(result, "json") else result
        pages.append(_rows_from_boxes(res["dt_polys"], res["rec_texts"]))
    return "\n".join(p for p in pages if p)
