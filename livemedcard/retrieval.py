"""Релевантнісний ретрив по free-text іпостасі (Stage 1 summary / сирий текст).

Живить grounded-Q&A детальнішими відповідями: розбиває текст документа на пасажі
(з offset-ами → span-рівнева прив'язка), ранжує їх за питанням і повертає найкращі
разом із документом-джерелом і span-ом.

Два режими (межа детермінізму збережена — це лише ретрив, не висновки):
* **лексичний** (дефолт, offline) — перекриття токенів; нуль залежностей;
* **семантичний** — косинус на embedding-ах, коли переданий `embed`-callable
  (локальний сервер embeddings). М'який відкат на лексику, якщо embedder недоступний.

Модуль чистий: HTTP-embedder ін'єктується ззовні (`qa` збирає його з конфігу), тож
тести й CI лишаються offline.
"""
from __future__ import annotations

import math
import re
from typing import Callable, Optional

from pydantic import BaseModel

# Callable, що повертає embedding-вектор на кожен вхідний рядок (перший — питання).
Embedder = Callable[[list[str]], list[list[float]]]


class Passage(BaseModel):
    """Фрагмент free-text іпостасі з прив'язкою до документа й позиції в ньому."""

    text: str
    doc_id: str
    span: tuple[int, int]  # (start, end) offset у free-text документа


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"\w+", s.lower()) if len(w) >= 3}


def chunk(doc_id: str, text: str) -> list[Passage]:
    """Розбити текст документа на пасажі-рядки з коректними offset-ами.

    Пропускає порожні рядки, markdown-заголовки й `not stated` (порожні поля
    SGR-summary). Offset рахується від початку `text`, тож span валідний як зріз.
    """
    passages: list[Passage] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        start = pos
        pos += len(line)
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.lower() == "not stated":
            continue
        # span покриває непорожній рядок без кінцевого переносу.
        s = start + (len(line) - len(line.lstrip()))
        e = s + len(stripped)
        passages.append(Passage(text=stripped, doc_id=doc_id, span=(s, e)))
    return passages


def _lexical_rank(query: str, passages: list[Passage], top_k: int) -> list[Passage]:
    q = _tokens(query)
    if not q:
        return []
    scored = [(len(q & _tokens(p.text)), p) for p in passages]
    scored = [(sc, p) for sc, p in scored if sc > 0]
    scored.sort(key=lambda it: it[0], reverse=True)
    return [p for _, p in scored[:top_k]]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _semantic_rank(
    query: str, passages: list[Passage], embed: Embedder, top_k: int
) -> list[Passage]:
    vectors = embed([query] + [p.text for p in passages])
    qv, pvs = vectors[0], vectors[1:]
    scored = sorted(
        zip((_cosine(qv, pv) for pv in pvs), passages),
        key=lambda it: it[0],
        reverse=True,
    )
    return [p for _, p in scored[:top_k]]


def rank(
    query: str,
    passages: list[Passage],
    *,
    top_k: int = 5,
    embed: Optional[Embedder] = None,
) -> list[Passage]:
    """Найрелевантніші пасажі до питання. `embed` заданий → семантично, з відкатом
    на лексику при будь-якій помилці embedder-а (недоступний сервер тощо)."""
    if not passages:
        return []
    if embed is not None:
        try:
            return _semantic_rank(query, passages, embed, top_k)
        except Exception:
            pass  # м'який відкат: ретрив не має валити відповідь
    return _lexical_rank(query, passages, top_k)
