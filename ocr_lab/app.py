"""LiveMedCard OCR Lab — NuExtract3 на ZeroGPU (Spec.md Phase 0/1).

Окремий Gradio/ZeroGPU Space (НЕ модифікація головного local-first застосунку).
Один рушій NuExtract3 робить дві іпостасі документа: image→markdown і
image/text→KVT4. Сигнали безпеки тут НЕ рахуються — це лише розпізнавання й
структурування; клінічні висновки формує детермінований шар у LiveMedCard.

⚠ ПУБЛІЧНИЙ SANDBOX: лише синтетичні / повністю деідентифіковані документи.

Дві іпостасі API:
  • людський UI (кнопка «Розпізнати», api_name=extract) — markdown + normalized KVT4;
  • програмні ендпойнти для LiveMedCard, що дзеркалять l2b_nuextract:
      /image_to_markdown (image → markdown)  та  /extract_facts (text → raw KVT).
"""
from __future__ import annotations

import json
import time

import gradio as gr
import spaces
import torch
from transformers import AutoModelForImageTextToText, AutoProcessor

from kvt4 import FACTS_TEMPLATE, normalize

MODEL_ID = "numind/NuExtract3"
MODEL_REVISION = "main"
TEMPLATE_VERSION = "kvt4-v1"
TEMPLATE_JSON = json.dumps(FACTS_TEMPLATE, indent=2, ensure_ascii=False)

# Завантаження на CPU при старті; на GPU — усередині @spaces.GPU (правило ZeroGPU).
processor = AutoProcessor.from_pretrained(MODEL_ID, trust_remote_code=True)
model = AutoModelForImageTextToText.from_pretrained(
    MODEL_ID, dtype=torch.bfloat16, trust_remote_code=True
).eval()


# duration тримаємо помірним: ZeroGPU резервує ~1.5× від нього з денної квоти,
# тож нижчий cap дає більше викликів на добу (факти коротші за markdown).
@spaces.GPU(duration=75)
def _run(messages: list, chat_template_kwargs: dict, max_new_tokens: int = 2048) -> str:
    """Одна генерація NuExtract (image або text) → сирий рядок."""
    model.to("cuda")
    inputs = processor.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
        **chat_template_kwargs,
    ).to(model.device)
    with torch.inference_mode():
        generated = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
    generated = generated[:, inputs.input_ids.shape[1]:]
    return processor.batch_decode(
        generated, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )[0].strip()


def _img_messages(image):
    return [{"role": "user", "content": [{"type": "image", "image": image}]}]


def _txt_messages(text: str):
    return [{"role": "user", "content": [{"type": "text", "text": text}]}]


def _parse_facts(raw: str) -> list[dict]:
    """Витягти масив фактів із JSON-відповіді NuExtract за шаблоном {"facts": [...]}"""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return []
    facts = data.get("facts", []) if isinstance(data, dict) else data
    return [f for f in facts if isinstance(f, dict) and f.get("keyword")]


# ── програмні примітиви (дзеркало l2b_nuextract) ─────────────────────────────
def image_to_markdown(image) -> str:
    """Stage 1: зображення → markdown (довший вихід — більший ліміт)."""
    if image is None:
        raise gr.Error("Порожнє зображення.")
    return _run(_img_messages(image), {"enable_thinking": False, "mode": "markdown"}, 3072)


def extract_facts(text: str) -> dict:
    """Stage 2: текст/markdown → сирі KVT-факти {"facts": [...]} (компактний вихід)."""
    if not (text or "").strip():
        return {"facts": []}
    raw = _run(_txt_messages(text), {"enable_thinking": False, "template": TEMPLATE_JSON}, 1024)
    return {"facts": _parse_facts(raw)}


def _facts_from_image(image) -> list[dict]:
    raw = _run(_img_messages(image), {"enable_thinking": False, "template": TEMPLATE_JSON}, 1024)
    return _parse_facts(raw)


# ── людський UI-ендпойнт ─────────────────────────────────────────────────────
def extract(image, mode: str, source_document_id: str):
    """UI: image → markdown + normalized KVT4 + diagnostics."""
    if image is None:
        raise gr.Error("Завантажте зображення документа (синтетичне / деідентифіковане).")
    src = (source_document_id or "ocr-demo-001").strip()
    warnings: list[str] = []
    markdown = ""
    raw_facts: list[dict] = []
    t0 = time.time()

    if mode in ("Markdown", "Full"):
        markdown = image_to_markdown(image)
    if mode == "Structured":
        raw_facts = _facts_from_image(image)
    elif mode == "Full":
        raw_facts = _parse_facts(
            _run(_txt_messages(markdown), {"enable_thinking": False, "template": TEMPLATE_JSON}, 1024)
        )

    facts = normalize(raw_facts, src)
    if mode in ("Structured", "Full") and not facts:
        warnings.append("Модель не повернула валідних фактів за шаблоном KVT4.")
    unreadable = sum(1 for f in facts if f.get("unreadable"))
    if unreadable:
        warnings.append(f"{unreadable} факт(ів) позначено unreadable (value=null).")

    diagnostics = {
        "model_id": MODEL_ID, "model_revision": MODEL_REVISION,
        "template_version": TEMPLATE_VERSION, "runtime_mode": "zerogpu",
        "mode": mode, "latency_ms": int((time.time() - t0) * 1000),
        "facts_count": len(facts), "warnings": warnings,
    }
    return markdown, {"facts": facts}, diagnostics


_DISCLAIMER = """
### 🔬 LiveMedCard OCR Lab · NuExtract3 · ZeroGPU
> **⚠ Це публічний demonstration sandbox. Не завантажуйте реальні персональні
> медичні дані — лише синтетичні або повністю деідентифіковані документи.**
Сервіс лише **розпізнає й структурує** документ. Він **не ставить діагнозів**,
не визначає взаємодій ліків і не формує сигналів безпеки — це робить
детермінований шар у самому LiveMedCard.
"""
_BADGE = (
    f"`model: {MODEL_ID}` · `revision: {MODEL_REVISION}` · "
    f"`template: {TEMPLATE_VERSION}` · `runtime: zerogpu`"
)

with gr.Blocks(title="LiveMedCard OCR Lab", theme=gr.themes.Soft()) as demo:
    gr.Markdown(_DISCLAIMER)
    with gr.Row():
        with gr.Column(scale=1):
            image_in = gr.Image(type="pil", label="Документ (синтетичний)", height=340)
            mode_in = gr.Radio(
                ["Full", "Markdown", "Structured"], value="Full", label="Режим",
                info="Full = markdown + KVT4 (двоетапно) · Markdown = транскрипція · Structured = лише факти",
            )
            src_in = gr.Textbox(
                value="ocr-demo-001", label="source_document_id",
                info="Провенанс: додається до кожного факту",
            )
            submit = gr.Button("Розпізнати", variant="primary")
            gr.Examples(
                examples=[["samples/lab_report_ua.jpg", "Full", "ocr-demo-001"]],
                inputs=[image_in, mode_in, src_in], label="Синтетичний приклад",
            )
        with gr.Column(scale=1):
            with gr.Tabs():
                with gr.Tab("Markdown"):
                    md_out = gr.Markdown(label="Транскрипція")
                with gr.Tab("KVT4 JSON"):
                    kvt_out = gr.JSON(label="Normalized facts")
                with gr.Tab("Diagnostics"):
                    diag_out = gr.JSON(label="Diagnostics")
                with gr.Tab("Шаблон KVT4"):
                    gr.Code(value=TEMPLATE_JSON, language="json", label="template (kvt4-v1)")
    gr.Markdown(_BADGE)

    submit.click(
        extract, inputs=[image_in, mode_in, src_in],
        outputs=[md_out, kvt_out, diag_out], api_name="extract",
    )

    # Приховані програмні ендпойнти для клієнта LiveMedCard (gradio_client).
    with gr.Row(visible=False):
        api_img = gr.Image(type="pil")
        api_md = gr.Textbox()
        api_text = gr.Textbox()
        api_facts = gr.JSON()
        btn_md = gr.Button()
        btn_facts = gr.Button()
    btn_md.click(image_to_markdown, inputs=api_img, outputs=api_md, api_name="image_to_markdown")
    btn_facts.click(extract_facts, inputs=api_text, outputs=api_facts, api_name="extract_facts")

if __name__ == "__main__":
    demo.queue(max_size=12).launch()
