#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фінальний submit-документ кейсового Digital Future. A4, укр., <=3 стор."""
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, HRFlowable,
                                Table, TableStyle, ListFlowable, ListItem, Flowable)
from reportlab.lib.styles import ParagraphStyle

F = "/usr/share/fonts/truetype/dejavu/"
pdfmetrics.registerFont(TTFont("DV", F + "DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("DVB", F + "DejaVuSans-Bold.ttf"))
pdfmetrics.registerFont(TTFont("DVI", F + "DejaVuSans-Oblique.ttf"))

ACC = HexColor("#1a4d8f")
GREY = HexColor("#555555")
LGREY = HexColor("#eef1f5")

styles = {
 "title": ParagraphStyle("t", fontName="DVB", fontSize=16, leading=19, textColor=ACC, spaceAfter=2),
 "sub":   ParagraphStyle("s", fontName="DV", fontSize=9, leading=12, textColor=GREY, spaceAfter=9),
 "h2":    ParagraphStyle("h", fontName="DVB", fontSize=11.5, leading=14, textColor=ACC,
                         spaceBefore=10, spaceAfter=4),
 "tag":   ParagraphStyle("tg", fontName="DVB", fontSize=8, leading=10, textColor=white),
 "body":  ParagraphStyle("b", fontName="DV", fontSize=9.7, leading=13.4, alignment=TA_JUSTIFY, spaceAfter=6),
 "li":    ParagraphStyle("l", fontName="DV", fontSize=9.7, leading=13.2, alignment=TA_JUSTIFY, spaceAfter=3),
 "lead":  ParagraphStyle("ld", fontName="DVB", fontSize=10.2, leading=14, textColor=HexColor("#0d3b66"), spaceAfter=6),
 "cap":   ParagraphStyle("c", fontName="DVI", fontSize=8, leading=10.4, textColor=GREY),
}
def P(t, s="body"): return Paragraph(t, styles[s])
def UL(items):
    return ListFlowable([ListItem(P(i,"li"), leftIndent=13) for i in items],
                        bulletType="bullet", bulletFontName="DV", bulletFontSize=8.5,
                        leftIndent=9, spaceAfter=3)

class TraceDiagram(Flowable):
    """Чотиришарова архітектура TRACE, інстанційована для Картки+."""
    def __init__(self, width):
        self.width = width
        self.rowh = 12.2*mm; self.gap = 1.8*mm
        self.rows = [
            ("L4", HexColor("#8f2d3d"), "Людина — родина, у клінічній версії лікар",
             "Право рішення на межі клінічних дій. ABC: система готує докази, не діагноз."),
            ("L3", HexColor("#5a4a8f"), "Політика роутингу й ескалації (stateful)",
             "Тип документа → шар; конфлікт L2a/L2b або high-risk → передача людині."),
            ("L2b", HexColor("#8a5a2b"), "Локальна LLM-валідатор (MedGemma, on-device)",
             "Лише розуміння вільного тексту виписок; grounded-відповіді над таймлайном."),
            ("L2a", HexColor("#2a7f62"), "Класичний ML",
             "Числові показники (NER→LOINC), тренд-детекція. Калібрований, відтворюваний."),
            ("L1", HexColor("#0d3b66"), "Детермінований якір довіри (НЕ навчений)",
             "Взаємодії ліків, дозові межі по відкритій БД; довідники RxNorm/LOINC/SNOMED."),
        ]
        self.labelw = 12*mm
        self.height = len(self.rows)*self.rowh + (len(self.rows)-1)*self.gap
    def wrap(self, *a): return (self.width, self.height)
    def draw(self):
        c = self.canv
        y = self.height
        for tag, col, title, body in self.rows:
            y -= self.rowh
            # tag box
            c.setFillColor(col)
            c.roundRect(0, y, self.labelw, self.rowh, 2.5, fill=1, stroke=0)
            c.setFillColor(white); c.setFont("DVB", 10)
            c.drawCentredString(self.labelw/2, y + self.rowh/2 - 3.4, tag)
            # body box
            bx = self.labelw + 2*mm
            bw = self.width - bx
            c.setFillColor(HexColor("#eef1f5")); c.setStrokeColor(col); c.setLineWidth(0.8)
            c.roundRect(bx, y, bw, self.rowh, 2.5, fill=1, stroke=1)
            c.setFillColor(col); c.setFont("DVB", 8.2)
            c.drawString(bx+3*mm, y + self.rowh - 4.6*mm, title)
            c.setFillColor(HexColor("#333333")); c.setFont("DV", 7.2)
            from reportlab.lib.utils import simpleSplit
            for i, ln in enumerate(simpleSplit(body, "DV", 7.2, bw-6*mm)[:2]):
                c.drawString(bx+3*mm, y + self.rowh - 8.0*mm - i*3.1*mm, ln)
            y -= self.gap
        # vertical "trust anchor" arrow on the left
        c.setStrokeColor(HexColor("#888888")); c.setLineWidth(0.6)

def sectlabel(txt):
    tb = Table([[P(txt,"tag")]], colWidths=[None])
    tb.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),ACC),
        ("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),
        ("TOPPADDING",(0,0),(-1,-1),2),("BOTTOMPADDING",(0,0),(-1,-1),2)]))
    return tb

story = [
 P("LiveMedCard — жива медична картка, що відповідає на питання", "title"),
 P("Кейсове завдання · Digital Future Hackathon · ніша Aging &amp; Longevity · формат участі: соло<br/>"
   "Автор: Сергій Заболотній, д.т.н., професор — AI/ML, NLP, клінічний AI", "sub"),
 HRFlowable(width="100%", thickness=0.9, color=ACC, spaceAfter=8),

 P("Процес, який ми змінюємо: як людина та її родина ведуть і використовують медичну історію. "
   "Найгостріше це болить у догляді за літніми рідними — з них ми й починаємо.", "lead"),

 # 1
 P("1. Проблема — що зараз болить", "h2"),
 P("Медична історія будь-кого зі складним анамнезом — а найгостріше літньої людини 65+ — це фізична "
   "папка виписок, стос роздруківок аналізів, фото рецептів "
   "у телефоні доньки та те, що родичі запам'ятали зі слів лікаря. Частина даних формально є в eHealth "
   "(еЗдоров'я, Helsi), але вони покривають лише останні роки й лише державний контур: приватні "
   "лабораторії, стаціонарні виписки різних років, кардіограми, знімки залишаються розрізненим паперовим "
   "архівом. Дані існують — але у форматі, який не вміє відповідати на питання. Три наслідки:"),
 UL([
  "<b>Лікар бачить лише частину картини.</b> На прийомі він спирається на те, що пацієнт зміг принести і згадати. "
  "Динаміка показників за роки (креатинін, HbA1c, тиск) фізично лежить у папці, але нікому не видна.",
  "<b>Поліпрагмазія без контролю.</b> Людина приймає 5–10 препаратів від різних лікарів, які не бачать "
  "призначень одне одного. Дублювання діючих речовин і небезпечні взаємодії спливають постфактум — "
  "після госпіталізації.",
  "<b>Родина — «людський API» до здоров'я мами.</b> Дорослі діти переказують історію по пам'яті й не "
  "можуть відповісти на просте «а який у мами був калій пів року тому?».",
 ]),
 P("Первинний сегмент — догляд за літніми, де щільність болю найвища. Та сама механіка працює для "
   "кожного зі складною чи хронічною історією: пацієнти з хронічними діагнозами, люди після тривалого "
   "лікування, батьки, що ведуть медкарти дітей. Літні — точка входу, а не межа ринку."),

 # 2
 P("2. Стара логіка — як це вирішується зараз", "h2"),
 P("Сьогоднішнє «рішення» — це архів плюс ручна праця навколо нього. Папка зберігає документи, але не "
   "зміст. Цифрові кабінети зберігають кожен свій фрагмент, без злиття в цілісну історію. Синтез — "
   "побудову повної картини, помічання трендів, перевірку сумісності ліків — щоразу робить людина: "
   "лікар за 12 хвилин прийому або донька ввечері після роботи. Наявні «цифрові» надбудови логіки не "
   "змінюють: нагадувалки про ліки лише пришвидшують нагадування, сканери документів — лише архівацію. "
   "Архів залишається пасивним, синтез залишається на людині."),

 # 3
 P("3. AI-гіпотеза — як AI змінює саму логіку, а не пришвидшує", "h2"),
 P("<b>LiveMedCard</b> змінює природу об'єкта: медична картка з пасивного сховища стає агентом, який розуміє "
   "власний зміст, стежить за динамікою і відповідає на питання. Донька фотографує будь-який документ — "
   "у будь-якому вигляді — і система сама:"),
 UL([
  "<b>Читає і структурує.</b> Компактна медична модель (клас MedGemma) екстрагує з неструктурованого "
  "тексту тріплети «показник — значення — час» за медичною онтологією зі стандартними кодами (LOINC, "
  "RxNorm, SNOMED CT).",
  "<b>Будує єдиний таймлайн здоров'я.</b> Кожен документ вбудовується в одну хронологію: усі виміри "
  "креатиніну за 6 років на одній кривій, незалежно від лабораторій і бланків.",
  "<b>Помічає те, чого не бачить жоден окремий лікар:</b> тренди між епізодами, дублювання діючих "
  "речовин, потенційні взаємодії — як сигнали, а не за запитом.",
  "<b>Відповідає людською мовою</b> з посиланням на конкретні документи-джерела, а не пошуком по папці.",
 ]),
 P("<b>Чому саме AI тут ключовий, а не зручне доповнення.</b> Без AI задача не розв'язується в принципі: "
   "неможливо правилами чи шаблонами розпарсити тисячі варіантів оформлення виписок, рукописних доз і "
   "скорочень з десятків закладів за десятиліття. Саме здатність мовної моделі розуміти неструктурований "
   "медичний текст і зводити його до єдиної онтології — це та операція, якої стара логіка не мала. AI не "
   "пришвидшує ведення архіву — він скасовує архів як парадигму, замінюючи його діалогом з власною "
   "медичною історією."),
]

# продуктова зрілість — блок
story += [
 P("4. Продуктові рішення, що роблять це довіреним", "h2"),
 P("<b>Приватність by design.</b> Уся обробка — локальною моделлю на пристрої користувача; медичні дані "
   "не залишають телефон і не потрапляють у хмарні LLM-сервіси. Для медичних даних літньої людини це не "
   "маркетингова опція, а умова довіри — і водночас технічний бар'єр, який відрізняє продукт від будь-якої "
   "обгортки навколо хмарного GPT."),
 P("<b>Безпеку не довіряємо генеративній моделі.</b> Свідоме архітектурне рішення: критичні для здоров'я "
   "перевірки — взаємодії ліків і дозові межі — виконує детермінований модуль по відкритій довідковій базі, "
   "а не мовна модель. LLM відповідає лише за розуміння тексту; висновок про безпеку — за верифікованим "
   "правилом. Це не перестраховка: емпірично показано, що і пацієнти, і лікарі переоцінюють впевнені, добре "
   "сформульовані відповіді LLM навіть за їх недостатньої точності — сама лінгвістична переконливість стає "
   "фактором ризику (клінічний базис TRACE, arXiv:2604.26671). Тому в медичному продукті безпеку не можна "
   "ставити в залежність від імовірнісної генерації.", ),
 P("<b>Чесна межа MVP.</b> Система не ставить діагнозів і не призначає лікування. Вона структурує факти, "
   "показує динаміку і формує правильні питання, з якими варто прийти до лікаря. Момент, коли продукт "
   "почне видавати клінічні призначення, — це окремий регуляторний рубіж (медичний виріб, лікар у контурі "
   "рішення), і ми проходимо його свідомо, а не випадково."),

 P("5. Методологічний базис і архітектура надійності: TRACE", "h2"),
 P("Продуктові рішення вище спираються на власний інженерний фреймворк автора "
   "<b>TRACE</b> (Zabolotnii, arXiv:2605.03838; traces.solutions) — метрологічно обґрунтованого підходу до "
   "довіреного агентного AI в критичних доменах. Головна теза: <b>довіру до системи вимірюють, а не "
   "декларують</b>. LiveMedCard інстанціює чотиришарову архітектуру TRACE — читати знизу вгору, від якоря "
   "довіри до людини:", "body"),
 TraceDiagram(170*mm),
 Spacer(1, 4),
 P("Чому ця структура дає надійність, а не «розумний чат над медкартою». Довіра тут — властивість "
   "усієї архітектури. Безпечні для здоров'я перевірки лежать у детермінованому L1, який "
   "формально верифікований і не галюцинує; генеративна модель (L2b) ізольована до єдиної задачі, де вона "
   "справді потрібна — розуміння вільного тексту. Вибір типу моделі для кожної підзадачі обґрунтований "
   "метрикою Computational Parsimony Ratio (CPR): LLM застосовується там, де додає цінність, а не за "
   "замовчуванням. Ескалація спрацьовує на поєднання високого ризику й високої впевненості — саме "
   "впевнена помилка найнебезпечніша. Ця структура прямо операціоналізує вимоги EU AI Act (ст. 14 — "
   "людський нагляд; ст. 15 — точність і робастність) для майбутньої клінічної версії.", "body"),

 P("6. Архітектура даних і сумісність (FHIR + граф знань)", "h2"),
 P("LiveMedCard структурує дані не у власний формат, а у стандартні ресурси <b>HL7 FHIR</b> "
   "(Observation, MedicationRequest, Condition) з кодами LOINC/RxNorm/SNOMED. Наслідок продуктовий: "
   "лікар отримує не скрін-фото, а структуровані дані, які приймає будь-яка лікарняна система й еЗдоров'я — "
   "картка перестає бути ще одним ізольованим застосунком. Наслідок стратегічний: LiveMedCard не конкурує "
   "з державним eHealth, а стає споживчим шаром поверх стандарту — щойно з'явиться інтеграційний API, "
   "сумісність уже закладена в дизайн. Це водночас менший ризик (беремо готову модель даних замість "
   "власної онтології) і рух у бік регуляції сумісності медданих (US Cures Act, EU EHDS).", "body"),
 P("Ті самі FHIR-ресурси живуть у двох взаємопов'язаних сховищах. <b>Факт-стор</b> (keyword–value–"
   "timestamp) — джерело істини для кількісного: саме на ньому <b>детерміновано</b> рахуються сигнали "
   "(тренд показника, взаємодія ліків), тому вони відтворювані й калібровані. <b>Граф знань</b> (FHIR-ресурси "
   "як вузли з типізованими ребрами) тримає семантичні зв'язки й провенанс — навіщо призначено препарат, "
   "з якого документа взято факт — і живить відповіді природною мовою з посиланням на джерело. Ключова "
   "межа: сигнали безпеки формуються на факт-сторі детерміновано, а не обходом графа генеративною моделлю.", "body"),

 P("7. Чому користувач обере нове рішення", "h2"),
 P("<b>Вся медична історія мами вперше відповідає на питання — замість того, щоб лежати в шухляді й "
   "чекати, поки хтось її перечитає.</b>", "lead"),

 HRFlowable(width="100%", thickness=0.6, color=HexColor("#c8d0da"), spaceBefore=4, spaceAfter=4),
 P("Реалізованість (соло, 48 год). Проєкт спирається на компоненти з дослідницької роботи автора з "
   "клінічного AI (проєкт Medical Brain): онтологія екстракції «показник — значення — час» з прив'язкою до "
   "RxNorm/LOINC/SNOMED CT; пайплайни для компактних локальних моделей; FHIR-вирівняні Pydantic-моделі "
   "ресурсів як спільна схема для факт-стору й графа. Технічне ядро — local-first структурування "
   "клінічного тексту локальною "
   "fine-tuned моделлю MedGemma — уже валідоване у власному препринті автора: Zabolotnii &amp; Holinko, "
   "«Local-First Clinical Text Structuring with Fine-Tuned MedGemma» (DOI: 10.5281/zenodo.20605433, "
   "CC-BY-4.0, 2026). План демо: фото документа → структурований таймлайн → сигнали про тренди і "
   "взаємодії → відповіді на питання родини природною мовою.", "cap"),

 Spacer(1, 6),
 P("Джерела", "h2"),
 P("[1] Zabolotnii, S., Holinko, V., &amp; Antonenko, O. (2026). <i>From Black-Box Confidence to Measurable "
   "Trust in Clinical AI: A Framework for Evidence, Supervision, and Staged Autonomy.</i> arXiv:2604.26671 "
   "(submitted to IEEE).", "cap"),
 P("[2] Zabolotnii, S. (2026). <i>TRACE: A Metrologically-Grounded Engineering Framework for Trustworthy "
   "Agentic AI Systems in Operationally Critical Domains.</i> arXiv:2605.03838 (cs.CL). "
   "Companion: traces.solutions.", "cap"),
 P("[3] Zabolotnii, S., &amp; Holinko, V. (2026). <i>Local-First Clinical Text Structuring with Fine-Tuned "
   "MedGemma for Readmission Risk Assessment.</i> Zenodo. DOI: 10.5281/zenodo.20605433 (CC-BY-4.0).", "cap"),
]

doc = SimpleDocTemplate(
 "/sessions/hopeful-serene-brahmagupta/mnt/outputs/LiveMedCard_DigitalFuture_Submit.pdf",
 pagesize=A4, leftMargin=20*mm, rightMargin=20*mm, topMargin=15*mm, bottomMargin=14*mm,
 title="LiveMedCard — кейсове завдання Digital Future Hackathon",
 author="Serhii Zabolotnii", subject="LiveMedCard — Digital Future Hackathon case")
doc.build(story)
print("OK")
