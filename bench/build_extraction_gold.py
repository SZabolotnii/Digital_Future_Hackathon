"""Складання gold-корпусу для вимірювання витягу L2b.

Кожен документ існує заради **названого явища**, а не для кількості. Це принципово:
корпус із двадцяти перефразувань «креатинін 121» дав би recall 1.0 і не сказав би
нічого. Тут кожен рядок — гіпотеза про те, де витяг ламається, і `note` каже, яка.

Очікувані факти — те, що витягла б **людина**, а не те, що витягає поточний код.
Інакше метрика вимірювала б сама себе: recall був би 1.0 за побудовою, а справжні
пропуски лишалися б невидимими. Кілька документів свідомо очікують **порожній**
список (діапазони норм, номери палат, заперечення) — саме на них міряється
precision, якої в OCR-бенчмарку не було взагалі.

Запуск: ``uv run python bench/build_extraction_gold.py`` → `bench/extraction_gold.json`.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).with_name("extraction_gold.json")

CREAT, K, NA, GLU, HB, HBA1C, SBP = (
    "2160-0", "2823-3", "2951-2", "2345-7", "718-7", "4548-4", "8480-6"
)
WARFARIN, ASPIRIN, IBUPROFEN, METFORMIN, LISINOPRIL, AMIODARONE = (
    "11289", "1191", "5640", "6809", "29046", "703"
)


def d(id_, lang, kind, date, text, *, facts=(), meds=(), probes="") -> dict:
    return {
        "id": id_, "lang": lang, "kind": kind, "received_at": date,
        "text": text, "facts": list(facts), "medications": list(meds),
        "probes": probes,
    }


def f(loinc, value, unit, date=None) -> dict:
    return {"loinc": loinc, "value": value, "unit": unit, "date": date}


def m(rxnorm, dose=None) -> dict:
    return {"rxnorm": rxnorm, "dose_mg_per_day": dose}


UK = [
    d("g-uk-01", "uk", "lab_report", "2024-01-10",
      "Біохімія крові. Креатинін 121 µmol/L. Калій 5.2 mmol/L.",
      facts=[f(CREAT, 121, "µmol/L"), f(K, 5.2, "mmol/L")],
      probes="базовий випадок: значення впритул до терміна"),
    d("g-uk-02", "uk", "lab_report", "2024-01-11",
      "Креатинін становив 118 µmol/L на момент огляду.",
      facts=[f(CREAT, 118, "µmol/L")],
      probes="значення НЕ впритул: між терміном і числом стоїть дієслово"),
    d("g-uk-03", "uk", "lab_report", "2024-01-12",
      "Показники:\n  Креатинін .......... 96 µmol/L\n  Калій .............. 4.4 mmol/L",
      facts=[f(CREAT, 96, "µmol/L"), f(K, 4.4, "mmol/L")],
      probes="табличне вирівнювання крапками — типове для бланків"),
    d("g-uk-04", "uk", "lab_report", "2024-02-01",
      "Креатинін 132 µmol/L (2024-01-28), калій 5.4 mmol/L (2024-01-28).",
      facts=[f(CREAT, 132, "µmol/L", "2024-01-28"), f(K, 5.4, "mmol/L", "2024-01-28")],
      probes="дата на кожен факт, відмінна від дати подання"),
    d("g-uk-05", "uk", "note", "2024-02-05",
      "Аналізи здано, креатинін 128 µmol/L. Дату в бланку не вказано.",
      facts=[f(CREAT, 128, "µmol/L")],
      probes="дати немає взагалі — valid time має лишитись датою документа"),
    d("g-uk-06", "uk", "lab_report", "2024-02-20",
      "За період 2024-01-15 - 2024-02-15 середня глюкоза 7.9 mmol/L.",
      facts=[f(GLU, 7.9, "mmol/L", "2024-02-15")],
      probes="період замість дати: якорем має стати кінець періоду"),
    d("g-uk-07", "uk", "lab_report", "2024-03-01",
      "Глюкоза натще 122 mg/dL.",
      facts=[f(GLU, 122, "mg/dL")],
      probes="одиниця не збігається з довідником — має дати unit_mismatch, не тихе порівняння"),
    d("g-uk-08", "uk", "lab_report", "2024-03-02",
      "Калій 5.1, креатинін 140.",
      facts=[f(K, 5.1, None), f(CREAT, 140, None)],
      probes="одиниць немає зовсім"),
    d("g-uk-09", "uk", "lab_report", "2024-03-10",
      "Гемоглобін 118 g/L, глікований гемоглобін 7.9 %.",
      facts=[f(HB, 118, "g/L"), f(HBA1C, 7.9, "%")],
      probes="довший термін не має злипатися з коротшим (гемоглобін ⊂ глікований гемоглобін)"),
    d("g-uk-10", "uk", "prescription", "2024-03-15",
      "Призначено: метформін 850 мг тричі на добу.",
      meds=[m(METFORMIN, 2550)],
      probes="доза × кратність словом («тричі»)"),
    d("g-uk-11", "uk", "prescription", "2024-03-16",
      "Метформін по 1 таблетці 1000 мг двічі на добу.",
      meds=[m(METFORMIN, 2000)],
      probes="доза за формою випуску («по 1 таблетці N мг»)"),
    d("g-uk-12", "uk", "prescription", "2024-03-17",
      "Продовжити лізиноприл у попередній дозі.",
      meds=[m(LISINOPRIL, None)],
      probes="препарат без дози — має потрапити в картку, але без дозової перевірки"),
    d("g-uk-13", "uk", "prescription", "2024-03-20",
      "Варфарин 5 мг на добу. Додано аспірин 100 мг на добу.",
      meds=[m(WARFARIN, 5), m(ASPIRIN, 100)],
      probes="пара, що утворює взаємодію — обидва препарати мусять бути витягнуті"),
    d("g-uk-14", "uk", "prescription", "2024-03-21",
      "Метформін 1500 мг двічі на добу.",
      meds=[m(METFORMIN, 3000)],
      probes="перевищення добової межі (2550 mg) — сигнал залежить від правильної арифметики"),
    d("g-uk-15", "uk", "lab_report", "2024-04-01",
      "Феритин 18 нг/мл, вітамін D 22 нг/мл.",
      probes="показників немає у словнику — очікуємо порожньо, але це втрата recall, а не успіх"),
    d("g-uk-16", "uk", "note", "2024-04-05",
      "Пацієнтка 69 років, палата 312, телефон доньки 0671234567. Креатинін 126 µmol/L.",
      facts=[f(CREAT, 126, "µmol/L")],
      probes="числа-не-показники поруч: вік, палата, телефон"),
    d("g-uk-17", "uk", "note", "2024-04-10",
      "Калій не визначався. Креатинін 119 µmol/L.",
      facts=[f(CREAT, 119, "µmol/L")],
      probes="заперечення: «не визначався» не має породжувати факт"),
    d("g-uk-18", "uk", "lab_report", "2024-04-15",
      "Креатинін 133 µmol/L (норма 44-106 µmol/L).",
      facts=[f(CREAT, 133, "µmol/L")],
      probes="межі норми в тексті не є вимірюванням"),
    d("g-uk-19", "uk", "discharge_summary", "2024-04-20",
      "Креатинін 137 µmol/L. При виписці креатинін 137 µmol/L без динаміки.",
      facts=[f(CREAT, 137, "µmol/L")],
      probes="повторна згадка того самого факту має колапсувати в один"),
    d("g-uk-20", "uk", "discharge_summary", "2024-05-01",
      "Виписний епікриз. Діагноз: фібриляція передсердь, гіпертензія. "
      "Лабораторно: креатинін 141 µmol/L, калій 5.3 mmol/L, глюкоза 8.1 mmol/L. "
      "Систолічний тиск 158 mmHg. Призначено: варфарин 5 мг на добу, "
      "лізиноприл 10 мг на добу.",
      facts=[f(CREAT, 141, "µmol/L"), f(K, 5.3, "mmol/L"),
             f(GLU, 8.1, "mmol/L"), f(SBP, 158, "mmHg")],
      meds=[m(WARFARIN, 5), m(LISINOPRIL, 10)],
      probes="реалістичний епікриз: багато фактів різних типів в одному документі"),
]

EN = [
    d("g-en-01", "en", "lab_report", "2024-01-10",
      "Blood chemistry. Creatinine 121 µmol/L. Potassium 5.2 mmol/L.",
      facts=[f(CREAT, 121, "µmol/L"), f(K, 5.2, "mmol/L")],
      probes="baseline: value adjacent to the term"),
    d("g-en-02", "en", "lab_report", "2024-01-11",
      "Creatinine was 118 µmol/L at the time of review.",
      facts=[f(CREAT, 118, "µmol/L")],
      probes="value not adjacent: a verb sits between term and number"),
    d("g-en-03", "en", "lab_report", "2024-01-12",
      "Results:\n  Creatinine .......... 96 µmol/L\n  Potassium ........... 4.4 mmol/L",
      facts=[f(CREAT, 96, "µmol/L"), f(K, 4.4, "mmol/L")],
      probes="dot-leader table layout, typical of lab forms"),
    d("g-en-04", "en", "lab_report", "2024-02-01",
      "Creatinine 132 µmol/L (2024-01-28), potassium 5.4 mmol/L (2024-01-28).",
      facts=[f(CREAT, 132, "µmol/L", "2024-01-28"), f(K, 5.4, "mmol/L", "2024-01-28")],
      probes="per-fact date differing from the submission date"),
    d("g-en-05", "en", "note", "2024-02-05",
      "Labs done, creatinine 128 µmol/L. The form states no date.",
      facts=[f(CREAT, 128, "µmol/L")],
      probes="no date at all — valid time must stay the document date"),
    d("g-en-06", "en", "lab_report", "2024-02-20",
      "Over 2024-01-15 - 2024-02-15 average glucose 7.9 mmol/L.",
      facts=[f(GLU, 7.9, "mmol/L", "2024-02-15")],
      probes="period instead of a date: the end should anchor it"),
    d("g-en-07", "en", "lab_report", "2024-03-01",
      "Fasting glucose 122 mg/dL.",
      facts=[f(GLU, 122, "mg/dL")],
      probes="unit differs from the reference — must raise unit_mismatch, not compare silently"),
    d("g-en-08", "en", "lab_report", "2024-03-02",
      "Potassium 5.1, creatinine 140.",
      facts=[f(K, 5.1, None), f(CREAT, 140, None)],
      probes="no units at all"),
    d("g-en-09", "en", "lab_report", "2024-03-10",
      "Hemoglobin 118 g/L, glycated hemoglobin 7.9 %.",
      facts=[f(HB, 118, "g/L"), f(HBA1C, 7.9, "%")],
      probes="longer term must not collapse into the shorter one"),
    d("g-en-10", "en", "prescription", "2024-03-15",
      "Prescribed: metformin 850 mg three times a day.",
      meds=[m(METFORMIN, 2550)],
      probes="dose × frequency in words"),
    d("g-en-11", "en", "prescription", "2024-03-16",
      "Metformin 1 tablet of 1000 mg twice a day.",
      meds=[m(METFORMIN, 2000)],
      probes="dose expressed through the dosage form"),
    d("g-en-12", "en", "prescription", "2024-03-17",
      "Continue lisinopril at the previous dose.",
      meds=[m(LISINOPRIL, None)],
      probes="medication without a dose — must enter the card, but without a dose check"),
    d("g-en-13", "en", "prescription", "2024-03-20",
      "Warfarin 5 mg daily. Aspirin 100 mg daily added.",
      meds=[m(WARFARIN, 5), m(ASPIRIN, 100)],
      probes="an interacting pair — both must be extracted or L1 has nothing to compare"),
    d("g-en-14", "en", "prescription", "2024-03-21",
      "Metformin 1500 mg twice a day.",
      meds=[m(METFORMIN, 3000)],
      probes="daily limit exceeded — the signal depends on correct arithmetic"),
    d("g-en-15", "en", "lab_report", "2024-04-01",
      "Ferritin 18 ng/mL, vitamin D 22 ng/mL.",
      probes="analytes absent from the vocabulary — empty is expected, but it is lost recall, not success"),
    d("g-en-16", "en", "note", "2024-04-05",
      "Patient aged 69, room 312, daughter's phone 0671234567. Creatinine 126 µmol/L.",
      facts=[f(CREAT, 126, "µmol/L")],
      probes="non-measurement numbers nearby: age, room, phone"),
    d("g-en-17", "en", "note", "2024-04-10",
      "Potassium was not measured. Creatinine 119 µmol/L.",
      facts=[f(CREAT, 119, "µmol/L")],
      probes="negation: 'not measured' must not produce a fact"),
    d("g-en-18", "en", "lab_report", "2024-04-15",
      "Creatinine 133 µmol/L (reference 44-106 µmol/L).",
      facts=[f(CREAT, 133, "µmol/L")],
      probes="a reference range in the text is not a measurement"),
    d("g-en-19", "en", "discharge_summary", "2024-04-20",
      "Creatinine 137 µmol/L. On discharge creatinine 137 µmol/L, unchanged.",
      facts=[f(CREAT, 137, "µmol/L")],
      probes="a repeated mention of the same fact must collapse into one"),
    d("g-en-20", "en", "discharge_summary", "2024-05-01",
      "Discharge summary. Diagnosis: atrial fibrillation, hypertension. "
      "Labs: creatinine 141 µmol/L, potassium 5.3 mmol/L, glucose 8.1 mmol/L. "
      "Systolic bp 158 mmHg. Prescribed: warfarin 5 mg daily, lisinopril 10 mg daily.",
      facts=[f(CREAT, 141, "µmol/L"), f(K, 5.3, "mmol/L"),
             f(GLU, 8.1, "mmol/L"), f(SBP, 158, "mmHg")],
      meds=[m(WARFARIN, 5), m(LISINOPRIL, 10)],
      probes="realistic discharge summary: many facts of different kinds in one document"),
]


def main() -> None:
    corpus = {
        "_note": (
            "Gold-корпус витягу L2b. Очікувані факти — те, що витягла б ЛЮДИНА, "
            "а не поточний код: інакше recall дорівнював би 1.0 за побудовою. "
            "Складено автором, без незалежного анотатора — це обмеження, а не "
            "властивість. Тексти синтетичні, персональних даних немає."
        ),
        "version": "1.0.0",
        "documents": UK + EN,
    }
    OUT.write_text(
        json.dumps(corpus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    n_facts = sum(len(x["facts"]) for x in corpus["documents"])
    n_meds = sum(len(x["medications"]) for x in corpus["documents"])
    print(f"{OUT}: {len(corpus['documents'])} документів, "
          f"{n_facts} показників, {n_meds} призначень")


if __name__ == "__main__":
    main()
