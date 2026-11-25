"""Build the per-article .docx files and the final zip (same layout as the reference)."""
from __future__ import annotations

import io
import time
import zipfile

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.shared import Inches, Pt

from extractor import safe_name

TITLE_PT = 16
BODY_PT = 15


def _para(doc, text: str = "", size: int = BODY_PT, style: str | None = None):
    p = doc.add_paragraph(style=style)
    if text:
        run = p.add_run(text)
        run.font.size = Pt(size)
    return p


def build_docx(article: dict) -> bytes:
    """article: {title, location, paragraphs[], highlights[], links[]}"""
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, side, Inches(1))

    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = 1.15

    _para(doc, size=TITLE_PT)
    _para(doc, article.get("title", "").strip() + " ", size=TITLE_PT)
    _para(doc)
    if article.get("location", "").strip():
        _para(doc, article["location"].strip())
        _para(doc)

    for text in article.get("paragraphs", []):
        if text.strip():
            _para(doc, text.strip())
            _para(doc)

    highlights = [h.strip() for h in article.get("highlights", []) if h.strip()]
    if highlights:
        _para(doc, "Key highlight :-")
        _para(doc)
        bullet = "List Bullet" if "List Bullet" in [s.name for s in doc.styles
                                                    if s.type == WD_STYLE_TYPE.PARAGRAPH] else None
        for h in highlights:
            _para(doc, h, style=bullet)
        _para(doc)

    for link in article.get("links", []):
        if link.strip():
            _para(doc, link.strip() + " ")
            _para(doc)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def folder_name(article: dict) -> str:
    number = str(article.get("number", "")).strip()
    title = article.get("title", "").strip()
    return safe_name(f"{number}-{title}" if number else title)


def build_zip(articles: list[dict], images: dict[int, list[dict]]) -> bytes:
    """articles: edited article dicts (with 'index' and 'images' = list of kept image indexes).
    images: original article index -> list of {"ext", "data"}."""
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for art in articles:
            folder = folder_name(art)
            base, n = folder, 2
            while folder.lower() in used:
                folder, n = f"{base} ({n})", n + 1
            used.add(folder.lower())

            zf.writestr(f"{folder}/", b"")
            zf.writestr(f"{folder}/{folder}.docx", build_docx(art))
            source = images.get(art["index"], [])
            keep = art.get("images")
            keep = range(len(source)) if keep is None else keep
            for i, idx in enumerate(keep, start=1):
                if 0 <= idx < len(source):
                    img = source[idx]
                    # photos are already JPEG-compressed; store them as-is
                    zf.writestr(zipfile.ZipInfo(f"{folder}/{folder}_{i}.{img['ext']}",
                                                date_time=time.localtime()[:6]),
                                img["data"], compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()
