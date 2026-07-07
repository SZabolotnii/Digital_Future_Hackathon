#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Картка+ — архітектурний one-pager за TRACE. A4, кирилиця."""
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, HRFlowable, Flowable)
from reportlab.lib.styles import ParagraphStyle

F = "/usr/share/fonts/truetype/dejavu/"
pdfmetrics.registerFont(TTFont("DV", F + "DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("DVB", F + "DejaVuSans-Bold.ttf"))
pdfmetrics.registerFont(TTFont("DVI", F + "DejaVuSans-Oblique.ttf"))

ACC = HexColor("#1a4d8f")     # синій
L1C = HexColor("#0d3b66")     # deterministic anchor
L2aC = HexColor("#2a7f62")    # classical ML
L2bC = HexColor("#8a5a2b")    # LLM
L3C = HexColor("#5a4a8f")     # policy
L4C = HexColor("#8f2d3d")     # human
GREY = HexColor("#555555")
LGREY = HexColor("#eef1f5")
RED = HexColor("#b3261e")
GREEN = HexColor("#1e7d3a")

st = {
 "title": ParagraphStyle("t", fontName="DVB", fontSize=15, leading=18, textColor=ACC),
 "sub":   ParagraphStyle("s", fontName="DV", fontSize=8.5, leading=11, textColor=GREY),
 "h2":    ParagraphStyle("h", fontName="DVB", fontSize=11, leading=13, textColor=ACC,
                         spaceBefore=9, spaceAfter=4),
 "body":  ParagraphStyle("b", fontName="DV", fontSize=8.6, leading=11.4, alignment=TA_JUSTIFY),
 "cap":   ParagraphStyle("c", fontName="DVI", fontSize=7.6, leading=9.6, textColor=GREY),
 "cell":  ParagraphStyle("cl", fontName="DV", fontSize=7.6, leading=9.4),
 "cellb": ParagraphStyle("cb", fontName="DVB", fontSize=7.6, leading=9.4, textColor=white),
 "cellw": ParagraphStyle("cw", fontName="DV", fontSize=7.5, leading=9.2, textColor=white),
}

def P(t, s="body"): return Paragraph(t, st[s])

# ---- Layered diagram flowable ----
class LayerDiagram(Flowable):
    def __init__(self, w, layers):
        self.w = w; self.layers = layers
        self.rowh = 15*mm; self.gap = 2.2*mm
        self.height = len(layers)*self.rowh + (len(layers)-1)*self.gap
        self.width = w
    def wrap(self, *a): return (self.width, self.height)
    def draw(self):
        c = self.canv
        y = self.height
        for (tag, color, title, body) in self.layers:
            y -= self.rowh
            c.setFillColor(color); c.setStrokeColor(color)
            c.roundRect(0, y, self.w, self.rowh, 3, fill=1, stroke=0)
            # left tag box
            c.setFillColor(white)
            c.setFont("DVB", 11)
            c.drawString(6*mm, y + self.rowh/2 - 3, tag)
            c.setFont("DVB", 8.6)
            c.drawString(20*mm, y + self.rowh - 6*mm, title)
            c.setFont("DV", 7.4)
            # body wrapped manually
            from reportlab.lib.utils import simpleSplit
            lines = simpleSplit(body, "DV", 7.4, self.w - 24*mm)
            ty = y + self.rowh - 9.2*mm
            for ln in lines[:2]:
                c.drawString(20*mm, ty, ln); ty -= 3.4*mm
            y -= self.gap

layers = [
 ("L4", L4C, "Bounded Human Supervision — родина + (у клін. версії) лікар",
  "ABC-межа: система повертає структуру й питання, НЕ діагноз і НЕ призначення. Reviewer'а-клініциста додає pro/clinical рівень."),
 ("L3", L3C, "Orchestration & Escalation (stateful)",
  "Роутинг за типом документа: бланк→L2a, вільна виписка→L2b. Тригер на L2a/L2b inconsistency. Межа informational vs prescriptive."),
 ("L2b", L2bC, "LLM Validator — MedGemma 4B, локально, on-device",
  "Тільки те, що вимагає semantic understanding: екстракція з вільного тексту виписок, рукописних доз; grounded Q&A над timeline."),
 ("L2a", L2aC, "Classical ML",
  "NER числових значень з бланків (→LOINC), time-series тренд-детекція показників, класифікація типу документа. Калібрований, low-latency."),
 ("L1", L1C, "Deterministic Core — trust anchor (НЕ навчений)",
  "Взаємодії ліків по відкритій БД, дозові ліміти per GFR/вага, hard contraindications, довідники RxNorm/LOINC/SNOMED. Formally verifiable."),
]

# ---- CPR table ----
def cprrow(task, layer, cpr, verdict, vcolor):
    return [P(task,"cell"), P(layer,"cell"),
            Paragraph(cpr, ParagraphStyle("x",fontName="DVB",fontSize=7.6,leading=9.4,alignment=TA_CENTER)),
            Paragraph(verdict, ParagraphStyle("v",fontName="DVB",fontSize=7.4,leading=9.2,textColor=vcolor))]

cpr_data = [
 [P("<b>Підзадача</b>","cell"), P("<b>Куди належить</b>","cell"),
  Paragraph("<b>CPR</b>",ParagraphStyle("hh",fontName="DVB",fontSize=7.6,alignment=TA_CENTER)),
  P("<b>Вердикт</b>","cell")],
 cprrow("Екстракція з вільного тексту виписок", "L2b (LLM)", "≈ 1", "OK — залишити", GREEN),
 cprrow("NER значень з друкованих бланків", "L2a (NER)", "≈ 1", "OK — винести з LLM", GREEN),
 cprrow("Тренд-детекція («креатинін росте»)", "L2a / L1", "≪ 1", "FIX — не LLM", RED),
 cprrow("Перевірка взаємодій ліків", "L1 (БД)", "≪ 1", "FIX — safety-ризик", RED),
 cprrow("Q&amp;A природною мовою", "L2b grounded", "≈ 1", "OK — RAG над timeline", GREEN),
]

story = [
 P("Картка+ — архітектура за TRACE", "title"),
 P("Архітектурний one-pager до кейсового Digital Future Hackathon · Aging &amp; Longevity · "
   "методологія TRACE (Заболотній, 2026). Trust — властивість системи, не моделі.", "sub"),
 HRFlowable(width="100%", thickness=0.8, color=ACC, spaceBefore=3, spaceAfter=7),

 P("Чотиришарова архітектура (замість «один MedGemma робить усе»)", "h2"),
 LayerDiagram(170*mm, layers),
 Spacer(1, 3),
 P("Читати знизу вгору: L1 — детермінований якір довіри; над ним stateless-інвентар навчених компонентів "
   "(L2a класика / L2b LLM); L3 — окрема stateful-політика роутингу; L4 — людина з override-правами. "
   "Ескалація — на joint {high risk ∧ high confidence}, не лише на низьку впевненість.", "cap"),

 P("Model parsimony: CPR-аналіз пайплайну", "h2"),
]

t = Table(cpr_data, colWidths=[62*mm, 34*mm, 16*mm, 58*mm])
t.setStyle(TableStyle([
 ("BACKGROUND",(0,0),(-1,0),ACC),
 ("TEXTCOLOR",(0,0),(-1,0),white),
 ("FONTNAME",(0,0),(-1,0),"DVB"),
 ("GRID",(0,0),(-1,-1),0.4,HexColor("#c8d0da")),
 ("ROWBACKGROUNDS",(0,1),(-1,-1),[white, LGREY]),
 ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
 ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
 ("LEFTPADDING",(0,0),(-1,-1),4),("RIGHTPADDING",(0,0),(-1,-1),4),
]))
# recolor header text
story.append(t)
story.append(Spacer(1,3))
story.append(P("CPR ≪ 1 = архітектурний overhead / LLM-by-default. Два FIX не косметичні: тренд і взаємодії "
   "через LLM дають confident error у high-risk контексті — найдорожчий failure mode.", "cap"))

story.append(P("Три антипатерни, які TRACE ловить — і виправлення", "h2"))
ap = [
 [P("<b>Антипатерн</b>","cell"), P("<b>Де в пітчі</b>","cell"), P("<b>Виправлення</b>","cell")],
 [P("<font color='#b3261e'><b>Learned L1</b></font> — правила в LLM","cell"),
  P("«Перевіряє взаємодії ліків» силами MedGemma","cell"),
  P("Взаємодії й дозові ліміти — детермінований L1 по відкритій БД; LLM туди не пускати","cell")],
 [P("<font color='#b3261e'><b>LLM на structured</b></font>","cell"),
  P("«Помічає тренди» — монотонність числового ряду","cell"),
  P("Time-series / порогове правило в L2a-L1; тут нема чого «розуміти мовою»","cell")],
 [P("<font color='#b3261e'><b>LLM-by-default</b></font> на екстракції","cell"),
  P("Один MedGemma і на бланки, і на вільний текст","cell"),
  P("L3-роутинг: друковані бланки→L2a NER, вільні виписки→L2b","cell")],
]
apt = Table(ap, colWidths=[42*mm, 58*mm, 70*mm])
apt.setStyle(TableStyle([
 ("BACKGROUND",(0,0),(-1,0),ACC),("TEXTCOLOR",(0,0),(-1,0),white),
 ("FONTNAME",(0,0),(-1,0),"DVB"),
 ("GRID",(0,0),(-1,-1),0.4,HexColor("#c8d0da")),
 ("ROWBACKGROUNDS",(0,1),(-1,-1),[white, LGREY]),
 ("VALIGN",(0,0),(-1,-1),"TOP"),
 ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
 ("LEFTPADDING",(0,0),(-1,-1),4),("RIGHTPADDING",(0,0),(-1,-1),4),
]))
story.append(apt)

story.append(P("Регуляторна межа — вирішити свідомо зараз", "h2"))
story.append(P("<b>Informational бік (MVP, demo):</b> система структурує факти й показує динаміку. "
   "Governance = GDPR; локальна on-device модель прямо працює на compliance (дані не покидають пристрій). "
   "<b>Prescriptive бік:</b> у момент, коли з'являється алерт «ці препарати взаємодіють», продукт перетинає "
   "в SaMD → EU MDR / AI Act Annex III (high-risk) з вимогою L4-клініциста. "
   "<b>Рішення:</b> хакатон-demo тримати строго на informational боці; interaction-check показувати як "
   "L1-компонент «для клінічної версії з лікарем у L4». Це і є коректно проведена ABC-межа.", "body"))

story.append(Spacer(1,4))
story.append(HRFlowable(width="100%", thickness=0.6, color=HexColor("#c8d0da"), spaceAfter=4))
story.append(P("Для пітчу: формулювання «чотиришарова архітектура з детермінованим trust anchor і "
   "метрологічним обґрунтуванням вибору моделі (CPR), а не LLM-by-default» — рівень зрілості, який "
   "venture-журі відрізняє від студентського demo. Диференціація від GPT-wrapper — структурою, не словами.", "cap"))

doc = SimpleDocTemplate(
 "/sessions/hopeful-serene-brahmagupta/mnt/outputs/Kartka_Plus_TRACE_Architecture.pdf",
 pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=14*mm, bottomMargin=12*mm,
 title="Картка+ — архітектура за TRACE", author="Serhii Zabolotnii")
doc.build(story)
print("OK")
