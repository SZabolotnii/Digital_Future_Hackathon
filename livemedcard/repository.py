"""Персистентність картки у локальний JSON-файл (local-first).

Дані не залишають пристрій — це умова довіри для медичних даних. Формат — знімок
(`LiveMedCard.snapshot`), тож зберігання й відновлення симетричні.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .layers.l2b_extractor import Extractor
from .pipeline import LiveMedCard


def save_card(card: LiveMedCard, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(card.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_card(path: str, extractor: Optional[Extractor] = None) -> Optional[LiveMedCard]:
    """Відновити картку зі знімка; None, якщо файлу ще немає.

    Пошкоджений файл — ЯВНА помилка, а не «стартуємо порожнім»: інакше наступне
    автозбереження перезаписало б знімок порожнім станом (втрата медичних даних).
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Пошкоджений файл знімка картки: {path} ({exc})") from exc
    return LiveMedCard.from_snapshot(data, extractor)
