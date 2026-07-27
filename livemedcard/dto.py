"""DTO конвеєра TRACE — спільні типи між шарами.

`Signal` — єдиний тип сигналу для L1 і L2a; це роз'єднує L3-роутер від реалізацій
шарів безпеки. Детерміновані сигнали мають ``confidence == 1.0``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .fhir import Condition, DocumentReference, MedicationRequest, Observation

Severity = Literal["info", "moderate", "major", "critical"]
DocKind = Literal["discharge_summary", "lab_report", "prescription", "note"]

_SEVERITY_ORDER: dict[str, int] = {"info": 0, "moderate": 1, "major": 2, "critical": 3}


def severity_rank(s: Severity) -> int:
    """Порядковий ранг тяжкості (більше = гірше)."""
    return _SEVERITY_ORDER[s]


class Document(BaseModel):
    """Вхідний документ (у MVP — текст; у проді — фото → OCR/vision)."""

    id: str
    text: str
    kind: DocKind = "note"
    received_at: Optional[datetime] = None


class ExtractionResult(BaseModel):
    """Результат L2b: FHIR-ресурси + середня впевненість витягу.

    ``document_reference`` (free-text іпостась) заповнює лише двоетапний
    екстрактор; детермінований stub лишає None.
    """

    observations: list[Observation] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)
    medications: list[MedicationRequest] = Field(default_factory=list)
    mean_confidence: float = 1.0
    document_reference: Optional[DocumentReference] = None

    def is_empty(self) -> bool:
        return not (self.observations or self.conditions or self.medications)


class Signal(BaseModel):
    """Детермінований сигнал безпеки/тренду (L1 або L2a).

    ``key`` — мовно-незалежна тотожність сигналу: те, на що посилається запис
    перегляду людиною (`ReviewEntry`). Сигнали ніде не зберігаються, а
    перераховуються на кожен запит і в обох мовах, тож брати ідентичність з
    ``detail`` не можна — вона змінилася б від перемикання мови інтерфейсу.

    Ключ будується з коду показника/препарату й напряму порушення. Наслідок
    свідомий: нове вимірювання, що змінює напрям, дає новий ключ і новий предмет
    для огляду — квитування старого не переносить на нові дані.
    """

    layer: Literal["L1", "L2a"]
    kind: str  # напр. "interaction", "dose_limit", "trend", "range_breach"
    key: str
    severity: Severity
    detail: str
    subjects: list[str] = Field(default_factory=list)  # посилання на FHIR-ресурси
    confidence: float = 1.0
    # Версія набору правил, за яким сигнал з'явився. Проставляє конвеєр в одному
    # місці (`LiveMedCard.current_signals`), а не кожен детектор: правила L1 і L2a
    # читаються з тих самих файлів, тож версія в них спільна. Порожньо → сигнал
    # отримано в обхід конвеєра (прямий виклик шару в тесті).
    ruleset_version: str = ""


class Decision(BaseModel):
    """Рішення L3: авто чи ескалація до людини (L4)."""

    route_to: Literal["auto", "L4_human"]
    escalate: bool
    reasons: list[str] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)


class TraceReport(BaseModel):
    """Повний результат прогону одного документа крізь конвеєр."""

    document_id: str
    extraction: ExtractionResult
    signals: list[Signal]
    decision: Decision


class AuditEntry(BaseModel):
    """Запис аудиту одного рішення L3 (типізована схема журналу).

    Гарантує стабільну структуру журналу між версіями: знімки зі сторонніми чи
    неповними записами відхиляються валідацією при відновленні.
    """

    ts: datetime
    document_id: str
    kind: str
    extraction_confidence: float
    n_signals: int
    route_to: Literal["auto", "L4_human"]
    escalate: bool
    # Чим ризикував документ — від цього залежав поріг ескалації (economics).
    # Дефолт зберігає сумісність зі старими знімками.
    hazard: str = "medication"
    # Чи цей документ ВНІС новий детермінований сигнал major/critical. Це ДРУГЕ,
    # незалежне джерело вартості: небезпека вже знайдена, і питання лише в тому,
    # чи її хтось прогляне. Саме «вніс новий», а не «в картці є»: інакше після
    # першого major-сигналу кожен наступний документ платив би за ту саму
    # небезпеку ще раз, і контрфактична економія росла б від самої кількості
    # документів.
    high_risk: bool = False
    # Що саме спрацювало в політиці L3. Відрізняється від `high_risk`: той про
    # НОВУ небезпеку (вхід у вартість), цей — про причину маршруту (вхід у SNR і
    # EP). Документ може ескалювати через давно відомий сигнал: тоді причина
    # "risk", а нової небезпеки нема.
    escalation_drive: Literal["none", "risk", "confidence", "both"] = "none"
    # Вартість шляху виконання (вхід для TCC). `extractor` називає, ЯКИЙ шлях
    # пройшов документ, бо без цього латентність не з чим порівнювати: 24 с у
    # VLM і 24 с у регексі означають різні речі.
    extractor: str = ""
    latency_ms: float = 0.0
    # Скільки разів L2 викликали для цього документа. >1 означає, що політика
    # витратила бюджет повторної спроби замість того, щоб одразу кликати людину
    # (вхід для FPA). `extractor` при цьому називає той шлях, чий результат
    # зрештою взяли, а не останній випробуваний.
    attempts: int = 1


class ReviewEntry(BaseModel):
    """Дія людини (L4) над сигналом або над рішенням щодо документа.

    Журнал окремий від `AuditEntry`: той фіксує, що зробив конвеєр, цей — що
    зробила людина. Без такого запису метрики нагляду (OvR, RBI, SNR) не
    існують навіть теоретично, а економічна модель кредитує ескалацію вартістю,
    якої ніхто не спостерігає.
    """

    ts: datetime
    # Роль декларується клієнтом, особа не перевіряється — це MVP без
    # автентифікації, і воно має бути видно в даних, а не лише в UI.
    actor_role: Literal["caregiver", "clinician"]
    action: Literal["acknowledged", "overridden", "referred"]
    # Рівно одне з двох: ключ сигналу або id документа (рішення рівня документа).
    signal_key: Optional[str] = None
    document_id: Optional[str] = None
    note: str = ""
    # Час від показу до дії — вхід для RBI. Міряється клієнтом, тож може бути
    # відсутнім (curl, інший клієнт); відсутність не підміняємо нулем, бо нуль
    # означав би миттєвий огляд і занижував би оцінку навантаження.
    seconds_to_act: Optional[float] = None


class BoundaryEvent(BaseModel):
    """Перетин задекларованої межі автономії (вхід для метрики ABC).

    Межа described у `governance.py`: сигнали класу ``mandatory_review``
    система лише пропонує, вирішує людина. Подія фіксує момент, коли картку
    використали як вирішену, попри непереглянуті такі сигнали — експорт віддає
    її назовні (лікарю, в EHR), і далі вона живе без нашого інтерфейсу.
    """

    ts: datetime
    kind: Literal["export_with_unreviewed"]
    detail: str
    signal_keys: list[str] = Field(default_factory=list)
