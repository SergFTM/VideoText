"""Export NewsItem / StreamBrief collections to JSON or PDF.

JSON: structured dump, ready for downstream tooling.
PDF:  human-readable report with attribution. Uses reportlab.
"""
import io
import json
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


# Register a Cyrillic-capable font (Windows ships Arial; fall back to DejaVu if absent).
def _register_cyrillic_font() -> str:
    candidates = [
        ("Arial",       r"C:\Windows\Fonts\arial.ttf"),
        ("ArialBold",   r"C:\Windows\Fonts\arialbd.ttf"),
        ("DejaVuSans",  r"/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for name, path in candidates:
        try:
            pdfmetrics.registerFont(TTFont(name, path))
        except Exception:
            continue
    return "Arial" if "Arial" in pdfmetrics.getRegisteredFontNames() else "Helvetica"


def news_items_to_json(items: list[dict]) -> bytes:
    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(items),
        "items": items,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def news_items_to_pdf(items: list[dict], title: str = "VideoText — News digest") -> bytes:
    font = _register_cyrillic_font()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=title,
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle(
        "H1", parent=styles["Heading1"], fontName=font,
        fontSize=18, leading=22, spaceAfter=6,
    )
    h3 = ParagraphStyle(
        "H3", parent=styles["Heading3"], fontName=font,
        fontSize=12, leading=16, spaceBefore=10, spaceAfter=2,
    )
    body = ParagraphStyle(
        "body", parent=styles["BodyText"], fontName=font,
        fontSize=10, leading=14, spaceAfter=4,
    )
    muted = ParagraphStyle(
        "muted", parent=styles["BodyText"], fontName=font,
        fontSize=8, leading=10, textColor=colors.grey, spaceAfter=2,
    )
    quote = ParagraphStyle(
        "quote", parent=styles["BodyText"], fontName=font,
        fontSize=10, leading=14, leftIndent=12,
        textColor=colors.HexColor("#444444"),
        spaceAfter=4,
    )

    story = []
    story.append(Paragraph(title, h1))
    story.append(Paragraph(
        f"Экспортировано: {datetime.now().strftime('%Y-%m-%d %H:%M')}  ·  всего items: {len(items)}",
        muted,
    ))
    story.append(Spacer(1, 8))

    if not items:
        story.append(Paragraph("(пусто)", body))
    for it in items:
        story.append(Paragraph(_xml_escape(it.get("headline", "")), h3))
        meta = []
        if it.get("category"):
            meta.append(it["category"])
        if it.get("confidence") is not None:
            meta.append(f"conf {float(it['confidence']):.2f}")
        if it.get("created_at"):
            meta.append(it["created_at"].replace("T", " ")[:16])
        if it.get("status"):
            meta.append(it["status"])
        story.append(Paragraph("  ·  ".join(meta), muted))

        if it.get("quote"):
            story.append(Paragraph("«" + _xml_escape(it["quote"]) + "»", quote))
        if it.get("tags"):
            story.append(Paragraph(
                "теги: " + ", ".join(f"#{t}" for t in it["tags"]),
                muted,
            ))
        if it.get("attribution"):
            story.append(Paragraph(_xml_escape(it["attribution"]), muted))
        story.append(Spacer(1, 4))

    doc.build(story)
    return buf.getvalue()


def _xml_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
