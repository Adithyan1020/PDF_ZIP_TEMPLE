"""Parse a 'Temples of Kerala' style PDF into per-temple articles + images.

Layout assumptions (taken from the reference PDF):
  * Each article starts on a page that has a large bold title (>= 22pt),
    a bold location line (16-22pt) and the article number in the page header.
  * Body text is ~11.5pt, laid out in two columns or full width, around photos.
  * A bold "Key highlight" line is followed by a bulleted list and a maps link.
  * Full-bleed images (page backgrounds / decorative strips) are not photos.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf

TITLE_MIN = 22.0
LOCATION_MIN = 16.0
HEADER_BAND = 36.0   # pt from the top of the page reserved for the header
FOOTER_BAND = 48.0   # pt from the bottom reserved for page number / footer
PARA_GAP = 22.0      # line-top distance above which a new paragraph starts
REGION_GAP = 45.0    # distance above which lines belong to separate text regions
SENTENCE_END = re.compile(r"[.!?:;\"'”’)]\s*$")


@dataclass
class Line:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    col: str          # 'L' (left / full width) or 'R' (right column)
    bold: bool
    size: float


@dataclass
class Article:
    number: str = ""
    title: str = ""
    location: str = ""
    paragraphs: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)  # {"ext": str, "data": bytes}
    pages: list[int] = field(default_factory=list)

    @property
    def folder(self) -> str:
        name = f"{self.number}-{self.title}" if self.number else self.title
        return safe_name(name)


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "untitled"


def clean(text: str) -> str:
    return re.sub(r"[ \t ]+", " ", text).strip()


def join_text(parts: list[str]) -> str:
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not out:
            out = p
        elif out.endswith("-") and not out.endswith(" -"):
            out += p                      # hyphen at line end: "copper-" + "covered"
        else:
            out += " " + p
    return clean(out)


# ----------------------------------------------------------------------------
# Page level helpers
# ----------------------------------------------------------------------------

def page_spans(page: pymupdf.Page) -> list[dict]:
    spans = []
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for s in line["spans"]:
                if not s["text"].strip():
                    continue
                spans.append({
                    "text": s["text"],
                    "bbox": s["bbox"],
                    "size": s["size"],
                    "bold": bool(s["flags"] & 16) or "bold" in s["font"].lower(),
                })
    return spans


def in_header(bbox, h) -> bool:
    return bbox[3] < HEADER_BAND


def in_footer(bbox, h) -> bool:
    return bbox[1] > h - FOOTER_BAND


def build_lines(spans: list[dict], width: float) -> list[Line]:
    """Group spans that share a baseline into lines, split at the column gutter."""
    mid = width / 2
    spans = sorted(spans, key=lambda s: ((s["bbox"][1] + s["bbox"][3]) / 2, s["bbox"][0]))
    rows: list[list[dict]] = []
    for s in spans:
        cy = (s["bbox"][1] + s["bbox"][3]) / 2
        if rows:
            last = rows[-1]
            lcy = sum((t["bbox"][1] + t["bbox"][3]) / 2 for t in last) / len(last)
            if abs(cy - lcy) < 3.5:
                last.append(s)
                continue
        rows.append([s])

    for row in rows:
        row.sort(key=lambda s: s["bbox"][0])
    info = [{
        "y": (row[0]["bbox"][1] + row[0]["bbox"][3]) / 2,
        "x0": row[0]["bbox"][0],
        "full": any(s["bbox"][0] < mid - 2 and s["bbox"][2] > mid + 2 for s in row),
    } for row in rows]
    # A justified full-width line may have no single word crossing the gutter.
    # Treat it as full width when it sits directly above/below a full-width line
    # with the same left edge (i.e. it is part of the same paragraph / list).
    changed = True
    while changed:
        changed = False
        for i, r in enumerate(info):
            if r["full"] or r["x0"] >= mid:
                continue
            for j in (i - 1, i + 1):
                if 0 <= j < len(info) and info[j]["full"]                         and abs(info[j]["y"] - r["y"]) <= 17 and abs(info[j]["x0"] - r["x0"]) < 4:
                    r["full"] = changed = True
                    break

    lines: list[Line] = []
    for row, r in zip(rows, info):
        groups = {"F": row} if r["full"] else {
            "L": [s for s in row if s["bbox"][2] <= mid + 2],
            "R": [s for s in row if s["bbox"][2] > mid + 2],
        }
        for col, group in groups.items():
            if not group:
                continue
            text = ""
            for s in group:
                t = s["text"]
                if text and not text.endswith(" ") and not t.startswith(" "):
                    text += " "
                text += t
            x0 = min(s["bbox"][0] for s in group)
            # Lines starting in the left half share one reading stream (left column,
            # full-width paragraphs, bullet lists); lines starting right of the
            # gutter form the right column.
            col = "R" if x0 >= mid - 10 else "L"
            lines.append(Line(
                text=clean(text),
                x0=x0,
                y0=min(s["bbox"][1] for s in group),
                x1=max(s["bbox"][2] for s in group),
                y1=max(s["bbox"][3] for s in group),
                col=col,
                bold=all(s["bold"] for s in group),
                size=max(s["size"] for s in group),
            ))
    return lines


def build_regions(lines: list[Line]) -> list[list[Line]]:
    """Split lines into column regions and return them in reading order."""
    regions: list[list[Line]] = []
    for col in ("L", "R"):
        col_lines = sorted((l for l in lines if l.col == col), key=lambda l: l.y0)
        current: list[Line] = []
        for l in col_lines:
            if current and l.y0 - current[-1].y0 > REGION_GAP:
                regions.append(current)
                current = []
            current.append(l)
        if current:
            regions.append(current)
    order = {"L": 0, "R": 1}
    regions.sort(key=lambda r: (round(r[0].y0 / 4), order[r[0].col]))
    return regions


def bullet_marks(page: pymupdf.Page) -> list[tuple[float, float]]:
    """Centres of small filled shapes (list bullets drawn as vector graphics)."""
    marks = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return marks
    for d in drawings:
        r = d["rect"]
        if 1 < r.width < 8 and 1 < r.height < 8 and d.get("fill") is not None:
            marks.append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
    return marks


def has_bullet(line: Line, marks) -> bool:
    cy = (line.y0 + line.y1) / 2
    return any(mx < line.x0 and line.x0 - mx < 25 and abs(my - cy) < 5 for mx, my in marks)


def page_images(doc: pymupdf.Document, page: pymupdf.Page) -> list[tuple[float, float, int]]:
    w, h = page.rect.width, page.rect.height
    found = []
    for info in page.get_image_info(xrefs=True):
        xref = info.get("xref") or 0
        x0, y0, x1, y1 = info["bbox"]
        bw, bh = x1 - x0, y1 - y0
        if xref <= 0 or bw < 40 or bh < 40:
            continue
        if bw >= 0.9 * w or bh >= 0.9 * h:       # background / decorative strip
            continue
        found.append((y0, x0, xref))
    found.sort(key=lambda t: (round(t[0] / 10), t[1]))
    return found


def export_image(doc: pymupdf.Document, xref: int) -> dict | None:
    try:
        info = doc.extract_image(xref)
    except Exception:
        info = None
    if info and info.get("image") and not info.get("smask") and info.get("ext") in ("jpeg", "jpg", "png"):
        ext = "jpg" if info["ext"] in ("jpeg", "jpg") else "png"
        return {"ext": ext, "data": info["image"]}
    try:
        pix = pymupdf.Pixmap(doc, xref)
        if info and info.get("smask"):
            pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, info["smask"]))
        if pix.colorspace and pix.colorspace.n > 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        return {"ext": "png", "data": pix.tobytes("png")}
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Document level
# ----------------------------------------------------------------------------

def extract(pdf_bytes: bytes) -> dict:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    articles: list[Article] = []
    intro: list[str] = []

    cur: Article | None = None
    para_parts: list[str] = []
    mode = "body"           # body | highlight
    last_line: Line | None = None
    last_region_width = 0.0
    seen_xrefs: set[int] = set()

    def flush_para():
        nonlocal para_parts
        if cur is not None and para_parts:
            text = join_text(para_parts)
            if text:
                cur.paragraphs.append(text)
        para_parts = []

    for pno in range(doc.page_count):
        page = doc[pno]
        w, h = page.rect.width, page.rect.height
        spans = page_spans(page)

        title_spans = [s for s in spans if s["size"] >= TITLE_MIN and s["bold"]
                       and not in_header(s["bbox"], h)]
        start = None
        if title_spans:
            tl = build_lines(title_spans, w)
            tl.sort(key=lambda l: (l.y0, l.x0))
            top = max(l.y1 for l in tl)
            loc = [s for s in spans if LOCATION_MIN <= s["size"] < TITLE_MIN and s["bold"]
                   and not in_header(s["bbox"], h) and 0 <= s["bbox"][1] - top < 40]
            if loc:          # an article title is always followed by its location line
                start = (tl, loc)
        if start:
            tl, loc = start
            # --- a new article starts on this page
            flush_para()
            cur = Article()
            articles.append(cur)
            mode, last_line, seen_xrefs = "body", None, set()
            cur.title = join_text([l.text for l in tl])
            loc.sort(key=lambda s: (s["bbox"][1], s["bbox"][0]))
            cur.location = join_text([s["text"] for s in loc])
            num = [s["text"].strip() for s in spans
                   if (in_header(s["bbox"], h) or s["bbox"][1] < 40) and re.fullmatch(r"\d{1,5}", s["text"].strip())]
            if num:
                cur.number = num[0]
            elif len(articles) > 1 and articles[-2].number.isdigit():
                cur.number = str(int(articles[-2].number) + 1)
            spans = [s for s in spans if s["size"] < LOCATION_MIN]

        body = [s for s in spans if s["size"] < LOCATION_MIN
                and not in_header(s["bbox"], h) and not in_footer(s["bbox"], h)]

        if cur is None:
            # cover / introduction pages before the first article
            lines = build_lines(body + [s for s in spans if s["size"] >= LOCATION_MIN], w)
            intro.extend(l.text for l in lines)
            continue

        cur.pages.append(pno + 1)
        for _, _, xref in page_images(doc, page):
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            img = export_image(doc, xref)
            if img:
                cur.images.append(img)

        marks = bullet_marks(page)
        for region in build_regions(build_lines(body, w)):
            region_width = max(l.x1 for l in region) - min(l.x0 for l in region)
            first = True
            for line in region:
                text = line.text
                if re.match(r"^key\s*highlights?\b", text, re.I):
                    flush_para()
                    mode = "highlight"
                    last_line = line
                    continue
                if re.match(r"^(https?://|www\.)\S+$", text):
                    flush_para()
                    cur.links.append(text)
                    last_line = line
                    continue

                if mode == "highlight":
                    bullet = has_bullet(line, marks) or bool(re.match(r"^[•●▪\-\*]\s", text))
                    text = re.sub(r"^[•●▪\*]\s*", "", text)
                    same_block = (last_line is not None and not first
                                  and abs(line.x0 - last_line.x0) < 6 and line.y0 - last_line.y0 < PARA_GAP)
                    if bullet or not cur.highlights:
                        cur.highlights.append(text)
                    elif same_block:
                        cur.highlights[-1] = join_text([cur.highlights[-1], text])
                    else:
                        mode = "body"     # text after the list: back to the article body
                    if mode == "highlight":
                        last_line, first = line, False
                        continue

                # ---- body text: decide whether this line starts a new paragraph
                if last_line is None:
                    new_para = True
                elif not first:
                    new_para = line.y0 - last_line.y0 > PARA_GAP
                else:
                    short = (last_line.x1 - last_line.x0) < 0.8 * last_region_width
                    new_para = bool(SENTENCE_END.search(last_line.text)) and short
                if new_para:
                    flush_para()
                para_parts.append(text)
                last_line, first = line, False
            last_region_width = region_width
        # paragraphs may continue onto the next page; nothing to flush here

    flush_para()
    doc.close()
    return {"intro": intro, "articles": articles}
