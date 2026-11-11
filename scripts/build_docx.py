#!/usr/bin/env python
"""Markdown → DOCX (Word) для документов проекта. Правила разметки те же, что у scripts/build_manual.py:
заголовки #/##/###, абзацы, списки (с вложением), таблицы, врезки «> **Важно.** …», картинки ![подпись {w=50}](путь),
**жирный**, *курсив*, `код`, [ссылка](адрес), блоки ``` и <!-- pagebreak -->.

    python scripts/build_docx.py docs/РУКОВОДСТВО.md                      # → docs/docx/РУКОВОДСТВО.docx
    python scripts/build_docx.py docs/РУКОВОДСТВО.md -o "D:/Документы/Руководство.docx"
    python scripts/build_docx.py --all                                    # все docs/*.md, docs/legal/*.md, пакет юриста,
                                                                          # живые юридические тексты сайта → docs/docx/

Нужен python-docx:  pip install python-docx. Файлы открываются и правятся в Microsoft Word, LibreOffice и Google Docs
(язык документа — русский, оглавление и заголовки — настоящие стили Word, нумерация списков — встроенная).
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import itertools
import re
import subprocess
import sys
import tempfile
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT_DIR = DOCS / "docx"
BUILD = DOCS / "_build"
SITE = "https://dianthus-opt.ru"

FONT, MONO = "Calibri", "Consolas"
GREEN = RGBColor(0x2F, 0x4A, 0x28)
GREY = RGBColor(0x6B, 0x6B, 0x60)
TEXT_W = 17.0            # см: A4 минус поля по 2 см
MAX_IMG_H = 20.0         # см: выше картинку не растягиваем

_spec = importlib.util.spec_from_file_location("build_manual", ROOT / "scripts" / "build_manual.py")
bm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bm)             # find_browser(), LIST_RE
LIST_RE = bm.LIST_RE

ABS_BULLET, ABS_DECIMAL = 90, 91
_num_ids = itertools.count(100)
_bm_ids = itertools.count(1)


# ───────────────────────── низкоуровневые помощники ─────────────────────────

def _set_fonts(rpr, name: str) -> None:
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    for a in ("ascii", "hAnsi", "cs", "eastAsia"):
        rfonts.set(qn(f"w:{a}"), name)
    for a in ("asciiTheme", "hAnsiTheme", "cstheme", "eastAsiaTheme"):
        if rfonts.get(qn(f"w:{a}")) is not None:
            del rfonts.attrib[qn(f"w:{a}")]


def _insert_before(parent, child, successors: tuple[str, ...]) -> None:
    for tag in successors:
        found = parent.find(qn(tag))
        if found is not None:
            found.addprevious(child)
            return
    parent.append(child)


def strip_md(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"\1", text)


TOKEN = re.compile(r"(`[^`]+`)|(\[[^\]]+\]\([^)\s]+\))|(\*\*.+?\*\*)|((?<![*\w])\*(?!\s).+?(?<!\s)\*(?![*\w]))")


def add_run(par, text, *, bold=False, italic=False, code=False, size=None, color=None):
    if not text:
        return None
    run = par.add_run(text)
    if bold:
        run.bold = True
    if italic:
        run.italic = True
    if size:
        run.font.size = Pt(size)
    if color is not None:
        run.font.color.rgb = color
    if code:
        rpr = run._r.get_or_add_rPr()
        _set_fonts(rpr, MONO)
        if not size:
            run.font.size = Pt(9.5)
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), "EFEBDF")
        rpr.append(shd)
    return run


def add_hyperlink(par, text, *, url=None, anchor=None, bold=False, size=None):
    link = OxmlElement("w:hyperlink")
    if url:
        link.set(qn("r:id"), par.part.relate_to(url, RT.HYPERLINK, is_external=True))
    if anchor:
        link.set(qn("w:anchor"), anchor)
    link.set(qn("w:history"), "1")
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    if bold:
        rpr.append(OxmlElement("w:b"))
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "1F5FA8")
    rpr.append(color)
    if size:
        sz = OxmlElement("w:sz")
        sz.set(qn("w:val"), str(int(size * 2)))
        rpr.append(sz)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    rpr.append(underline)
    run.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    link.append(run)
    par._p.append(link)


def add_inline(par, text, *, bold=False, italic=False, size=None, color=None) -> None:
    pos = 0
    for m in TOKEN.finditer(text):
        add_run(par, text[pos:m.start()], bold=bold, italic=italic, size=size, color=color)
        tok = m.group(0)
        if m.group(1):
            add_run(par, tok[1:-1], code=True, bold=bold, italic=italic, size=(size - 0.5) if size else None)
        elif m.group(2):
            label, url = re.match(r"\[([^\]]+)\]\(([^)\s]+)\)", tok).groups()
            if url.startswith("#"):
                add_run(par, label, bold=bold, italic=italic, size=size, color=color)
            else:
                add_hyperlink(par, label, url=url, bold=bold, size=size)
        elif m.group(3):
            add_inline(par, tok[2:-2], bold=True, italic=italic, size=size, color=color)
        else:
            add_inline(par, tok[1:-1], bold=bold, italic=True, size=size, color=color)
        pos = m.end()
    add_run(par, text[pos:], bold=bold, italic=italic, size=size, color=color)


def add_bookmark(par, name: str) -> None:
    bid = str(next(_bm_ids))
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), bid)
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), bid)
    ppr = par._p.pPr
    par._p.insert(list(par._p).index(ppr) + 1 if ppr is not None else 0, start)
    par._p.append(end)


def add_page_field(par, size=8.5) -> None:
    def fld(kind):
        run = par.add_run()
        run.font.size = Pt(size)
        run.font.color.rgb = GREY
        el = OxmlElement("w:fldChar")
        el.set(qn("w:fldCharType"), kind)
        run._r.append(el)

    fld("begin")
    run = par.add_run()
    run.font.size = Pt(size)
    run.font.color.rgb = GREY
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    run._r.append(instr)
    fld("separate")
    add_run(par, "1", size=size, color=GREY)
    fld("end")


# ───────────────────────── документ и нумерация ─────────────────────────

def _abstract(abs_id: int, levels: list[tuple[str, str, int, int]]) -> str:
    out = [f'<w:abstractNum {nsdecls("w")} w:abstractNumId="{abs_id}"><w:multiLevelType w:val="hybridMultilevel"/>']
    for n, (fmt, text, left, hang) in enumerate(levels):
        out.append(
            f'<w:lvl w:ilvl="{n}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/><w:lvlText w:val="{text}"/>'
            f'<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="{left}" w:hanging="{hang}"/></w:pPr>'
            f'<w:rPr><w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}" w:cs="{FONT}"/><w:color w:val="2F4A28"/></w:rPr></w:lvl>')
    out.append("</w:abstractNum>")
    return "".join(out)


def new_document() -> Document:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2)
    sec.top_margin, sec.bottom_margin = Cm(1.8), Cm(1.8)
    styles = doc.styles

    dd = styles.element.find(qn("w:docDefaults"))
    if dd is not None and dd.find(qn("w:rPrDefault")) is not None:
        rpr = dd.find(qn("w:rPrDefault")).find(qn("w:rPr"))
        _set_fonts(rpr, FONT)
        lang = rpr.find(qn("w:lang"))
        if lang is None:
            lang = OxmlElement("w:lang")
            rpr.append(lang)
        lang.set(qn("w:val"), "ru-RU")
        lang.set(qn("w:eastAsia"), "ru-RU")
        lang.set(qn("w:bidi"), "ar-SA")

    normal = styles["Normal"]
    normal.font.name, normal.font.size = FONT, Pt(11)
    _set_fonts(normal.element.get_or_add_rPr(), FONT)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.12

    for name, size, before, after in (("Heading 1", 21, 0, 10), ("Heading 2", 16, 16, 6), ("Heading 3", 13, 12, 4)):
        st = styles[name]
        st.font.size, st.font.bold, st.font.italic = Pt(size), True, False
        st.font.color.rgb = GREEN
        _set_fonts(st.element.get_or_add_rPr(), FONT)
        pf = st.paragraph_format
        pf.space_before, pf.space_after = Pt(before), Pt(after)
        pf.keep_with_next = True
        pf.keep_together = True

    numbering = doc.part.numbering_part.element
    bullets = parse_xml(_abstract(ABS_BULLET, [("bullet", "•", 567, 284), ("bullet", "–", 1134, 284), ("bullet", "▪", 1701, 284)]))
    decimals = parse_xml(_abstract(ABS_DECIMAL, [("decimal", "%1.", 567, 397), ("lowerLetter", "%2)", 1134, 340), ("lowerRoman", "%3.", 1701, 340)]))
    first_num = numbering.find(qn("w:num"))
    for el in (bullets, decimals):
        if first_num is not None:
            first_num.addprevious(el)
        else:
            numbering.append(el)
    return doc


def new_num(doc, abstract_id: int, start: int | None = None) -> int:
    nid = next(_num_ids)
    num = parse_xml(f'<w:num {nsdecls("w")} w:numId="{nid}"><w:abstractNumId w:val="{abstract_id}"/></w:num>')
    if start is not None:
        num.append(parse_xml(f'<w:lvlOverride {nsdecls("w")} w:ilvl="0"><w:startOverride w:val="{start}"/></w:lvlOverride>'))
    doc.part.numbering_part.element.append(num)
    return nid


# ───────────────────────── картинки ─────────────────────────

def svg_to_png(svg: Path) -> Path | None:
    """Схема-SVG → PNG через Chrome/Edge (кэш в docs/_build/img). Нет браузера — None."""
    out = BUILD / "img" / (svg.stem + ".png")
    if out.exists() and out.stat().st_mtime >= svg.stat().st_mtime:
        return out
    browser = bm.find_browser()
    if not browser:
        return None
    from PIL import Image
    text = svg.read_text(encoding="utf-8")
    m = re.search(r'width="(\d+)"\s+height="(\d+)"', text) or re.search(r'viewBox="0 0 (\d+) (\d+)"', text)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1100, 400)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = out.with_suffix(".raw.png")
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                        "--default-background-color=FFFFFFFF", f"--user-data-dir={profile}", f"--window-size={w + 20},{h + 160}",
                        f"--screenshot={raw}", svg.resolve().as_uri()],
                       check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    img = Image.open(raw).convert("RGB")
    img.crop((0, 0, min(img.width, w * 2), min(img.height, h * 2))).save(out, "PNG", optimize=True)
    raw.unlink(missing_ok=True)
    return out


def load_image(path: Path):
    """→ (поток PNG/JPEG, ширина px, высота px) или None."""
    if path.suffix.lower() == ".svg":
        png = svg_to_png(path)
        if png is None:
            return None
        path = png
    try:
        from PIL import Image
        img = Image.open(path)
        w, h = img.size
        if path.suffix.lower() in (".png", ".jpg", ".jpeg"):
            return io.BytesIO(path.read_bytes()), w, h
        buf = io.BytesIO()
        img.convert("RGBA").save(buf, "PNG")
        buf.seek(0)
        return buf, w, h
    except Exception:                                        # noqa: BLE001
        return None


# ───────────────────────── разбор Markdown ─────────────────────────

def parse_list(lines: list[str], i: int) -> tuple[dict, int]:
    first = LIST_RE.match(lines[i])
    indent = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    start = int(first.group(2).rstrip(".)")) if ordered else 1
    items = []
    while i < len(lines):
        m = LIST_RE.match(lines[i])
        if not m or len(m.group(1)) != indent or (m.group(2)[0].isdigit() != ordered):
            break
        body, children = [m.group(3)], []
        i += 1
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                j = i
                while j < len(lines) and not lines[j].strip():
                    j += 1
                nxt = LIST_RE.match(lines[j]) if j < len(lines) else None
                if nxt and len(nxt.group(1)) >= indent and (len(nxt.group(1)) > indent or nxt.group(2)[0].isdigit() == ordered):
                    i = j
                    continue
                break
            lm = LIST_RE.match(line)
            if lm and len(lm.group(1)) > indent:
                sub, i = parse_list(lines, i)
                children.append(sub)
                continue
            if lm and len(lm.group(1)) <= indent:
                break
            if line.startswith(" " * (indent + 1)) and not lm:
                body.append(line.strip())
                i += 1
                continue
            break
        items.append((" ".join(body), children))
    return {"ordered": ordered, "start": start, "items": items}, i


def split_row(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def col_widths(rows: list[list[str]], total: float) -> list[float]:
    """Ширины колонок: пропорционально длине текста, но не уже самого длинного слова (чтобы слова не рвались)."""
    n = max(len(r) for r in rows)
    weights, mins = [], []
    for c in range(n):
        texts = [strip_md(r[c]) if c < len(r) else "" for r in rows]
        longest = max(len(t) for t in texts)
        longest_word = max((len(w) for t in texts for w in t.split()), default=1)
        weights.append(min(max(longest, 7), 70) ** 0.75)
        mins.append(min(longest_word, 26) * 0.21 + 0.55)
    widths = [total * w / sum(weights) for w in weights]
    for _ in range(12):
        widths = [max(w, m) for w, m in zip(widths, mins)]
        over = sum(widths) - total
        if over <= 0.01:
            break
        slack = [max(w - m, 0.0) for w, m in zip(widths, mins)]
        room = sum(slack)
        if room <= 0:
            break
        widths = [w - over * (s / room) for w, s in zip(widths, slack)]
    scale = total / sum(widths)
    return [w * scale for w in widths]


def prescan(lines: list[str]) -> tuple[str, str, list[tuple[int, str, str]]]:
    """Название, подзаголовок и список (уровень, текст, закладка) для оглавления."""
    title = subtitle = ""
    toc: list[tuple[int, str, str]] = []
    in_code = False
    first_h1 = True
    n = 0
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("```"):
            in_code = not in_code
        elif not in_code:
            h = re.match(r"^(#{1,6})\s+(.*)$", s)
            if h:
                level, text = min(len(h.group(1)), 3), h.group(2).strip()
                if level == 1 and first_h1:
                    first_h1, title = False, text
                    j = i + 1
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and not lines[j].lstrip().startswith(("#", "|", "!", ">", "```")) and not LIST_RE.match(lines[j]):
                        subtitle = lines[j].strip()
                else:
                    n += 1
                    toc.append((level, strip_md(text), f"_Toc{n}"))
        i += 1
    return title, subtitle, toc


class Builder:
    def __init__(self, base: Path, footer_title: str | None, cover: bool | None):
        self.doc = new_document()
        self.base = base
        self.footer_title = footer_title
        self.cover_opt = cover
        self.pending_break = False
        self.bullet_num = new_num(self.doc, ABS_BULLET)
        self.has_list_par = "List Paragraph" in [s.name for s in self.doc.styles]

    # ---- абзацы
    def para(self, style=None):
        p = self.doc.add_paragraph(style=style)
        if self.pending_break:
            p.paragraph_format.page_break_before = True
            self.pending_break = False
        return p

    def heading(self, level: int, text: str, bookmark: str, h1_break: bool):
        p = self.para(f"Heading {level}")
        if level == 1 and h1_break:
            p.paragraph_format.page_break_before = True
        p.add_run(strip_md(text))
        add_bookmark(p, bookmark)

    def paragraph(self, text: str):
        p = self.para()
        if text.rstrip().endswith(":"):
            p.paragraph_format.keep_with_next = True
        add_inline(p, text)

    def callout(self, lines: list[str], kind: str):
        styles = {"important": ("FFF2D9", "E0A030"), "tip": ("E9F2E4", "5C8A4E"),
                  "trouble": ("FBE9E7", "C0504D"), "": ("F3F0E6", "B8A878")}
        fill, border = styles[kind]
        t = self.doc.add_table(rows=1, cols=1)
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        t.autofit = False
        cell = t.cell(0, 0)
        cell.width = Cm(TEXT_W)
        tcpr = cell._tc.get_or_add_tcPr()
        tcpr.append(parse_xml(f'<w:tcBorders {nsdecls("w")}><w:top w:val="nil"/><w:left w:val="single" w:sz="24" w:space="0" '
                              f'w:color="{border}"/><w:bottom w:val="nil"/><w:right w:val="nil"/></w:tcBorders>'))
        tcpr.append(parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="{fill}"/>'))
        tcpr.append(parse_xml(f'<w:tcMar {nsdecls("w")}><w:top w:w="80" w:type="dxa"/><w:left w:w="160" w:type="dxa"/>'
                              f'<w:bottom w:w="80" w:type="dxa"/><w:right w:w="140" w:type="dxa"/></w:tcMar>'))
        p = cell.paragraphs[0]
        p.paragraph_format.space_after = Pt(2)
        add_inline(p, " ".join(lines), size=10.5)
        spacer = self.doc.add_paragraph()
        spacer.paragraph_format.space_after = Pt(4)
        spacer.paragraph_format.line_spacing = 0.5

    def table(self, head: list[str], rows: list[list[str]], aligns: list[str]):
        n = len(head)
        t = self.doc.add_table(rows=1 + len(rows), cols=n)
        t.style = "Table Grid"
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        t.autofit = False
        tblpr = t._tbl.tblPr
        tblw = tblpr.find(qn("w:tblW"))
        if tblw is not None:
            tblw.set(qn("w:type"), "dxa")
            tblw.set(qn("w:w"), str(int(TEXT_W * 567)))
        old = tblpr.find(qn("w:tblBorders"))
        if old is not None:
            tblpr.remove(old)
        borders = "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="C9C4B0"/>'
                          for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
        _insert_before(tblpr, parse_xml(f'<w:tblBorders {nsdecls("w")}>{borders}</w:tblBorders>'),
                       ("w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook", "w:tblCaption", "w:tblDescription"))
        _insert_before(tblpr, parse_xml(f'<w:tblCellMar {nsdecls("w")}><w:top w:w="40" w:type="dxa"/><w:left w:w="90" w:type="dxa"/>'
                                        f'<w:bottom w:w="40" w:type="dxa"/><w:right w:w="90" w:type="dxa"/></w:tblCellMar>'),
                       ("w:tblLook", "w:tblCaption", "w:tblDescription"))
        widths = col_widths([head] + rows, TEXT_W)
        for c, w in enumerate(widths):
            t.columns[c].width = Cm(w)
        for r, row in enumerate([head] + rows):
            tr = t.rows[r]
            trpr = tr._tr.get_or_add_trPr()
            trpr.append(OxmlElement("w:cantSplit"))
            if r == 0:
                trpr.append(OxmlElement("w:tblHeader"))
            for c in range(n):
                cell = tr.cells[c]
                cell.width = Cm(widths[c])
                if r == 0:
                    cell._tc.get_or_add_tcPr().append(parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="E3EBDD"/>'))
                p = cell.paragraphs[0]
                p.paragraph_format.space_after = Pt(0)
                p.paragraph_format.space_before = Pt(0)
                p.paragraph_format.line_spacing = 1.05
                if c < len(aligns) and aligns[c] == "center":
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                text = row[c] if c < len(row) else ""
                add_inline(p, text, size=9.5, bold=(r == 0))
        spacer = self.doc.add_paragraph()
        spacer.paragraph_format.space_after = Pt(2)
        spacer.paragraph_format.line_spacing = 0.5

    def code(self, block: list[str]):
        block = block or [""]
        for n, line in enumerate(block):
            p = self.para()
            ppr = p._p.get_or_add_pPr()
            ppr.append(parse_xml(f'<w:pBdr {nsdecls("w")}><w:left w:val="single" w:sz="18" w:space="6" w:color="C9A56A"/></w:pBdr>'))
            ppr.append(parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="F4F1E8"/>'))
            pf = p.paragraph_format
            pf.space_before = Pt(0)
            pf.space_after = Pt(8 if n == len(block) - 1 else 0)
            pf.left_indent = Cm(0.4)
            pf.line_spacing = 1.0
            add_run(p, line if line else " ", code=False, size=9)
            for r in p.runs:
                _set_fonts(r._r.get_or_add_rPr(), MONO)

    def image(self, path: str, caption: str, pct: int | None):
        loaded = load_image(self.base / path)
        cap = strip_md(caption)
        if loaded is None:
            p = self.para()
            add_inline(p, f"[рисунок: {cap or path}]", italic=True, color=GREY)
            return
        stream, w, h = loaded
        width = TEXT_W * (pct or 100) / 100
        if width * h / w > MAX_IMG_H:
            width = MAX_IMG_H * w / h
        p = self.para()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.keep_with_next = True
        p.paragraph_format.space_after = Pt(2)
        p.add_run().add_picture(stream, width=Cm(width))
        if cap:
            c = self.doc.add_paragraph()
            c.alignment = WD_ALIGN_PARAGRAPH.CENTER
            c.paragraph_format.space_after = Pt(10)
            add_run(c, cap, italic=True, size=9, color=GREY)

    def lst(self, lst: dict, depth: int = 0, num_id: int | None = None):
        if lst["ordered"]:
            my_num = num_id if num_id is not None else new_num(self.doc, ABS_DECIMAL, lst["start"])
        else:
            my_num = self.bullet_num
        for text, children in lst["items"]:
            p = self.para("List Paragraph" if self.has_list_par else None)
            numpr = p._p.get_or_add_pPr().get_or_add_numPr()
            numpr.get_or_add_ilvl().val = min(depth, 2)
            numpr.get_or_add_numId().val = my_num
            p.paragraph_format.space_after = Pt(3)
            add_inline(p, text)
            for child in children:
                self.lst(child, depth + 1, my_num if (child["ordered"] and lst["ordered"]) else None)

    # ---- обложка, оглавление, подвал
    def cover(self, title: str, subtitle: str):
        logo = ROOT / "app" / "static" / "emblem.webp"
        p = self.para()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(110)
        loaded = load_image(logo) if logo.exists() else None
        if loaded:
            p.add_run().add_picture(loaded[0], width=Cm(4))
        t = self.doc.add_paragraph()
        t.alignment = WD_ALIGN_PARAGRAPH.CENTER
        t.paragraph_format.space_before = Pt(26)
        add_run(t, title, bold=True, size=28, color=GREEN)
        if subtitle:
            s = self.doc.add_paragraph()
            s.alignment = WD_ALIGN_PARAGRAPH.CENTER
            add_run(s, strip_md(subtitle), size=13, color=GREY)
        u = self.doc.add_paragraph()
        u.alignment = WD_ALIGN_PARAGRAPH.CENTER
        u.paragraph_format.space_before = Pt(14)
        add_run(u, "dianthus-opt.ru", size=12, color=GREEN)

    def toc(self, entries: list[tuple[int, str, str]]):
        h = self.doc.add_paragraph()
        h.paragraph_format.page_break_before = True
        h.paragraph_format.space_after = Pt(10)
        add_run(h, "Содержание", bold=True, size=21, color=GREEN)
        for level, text, bookmark in entries:
            if level > 2:
                continue
            p = self.doc.add_paragraph()
            p.paragraph_format.space_after = Pt(2 if level == 2 else 3)
            p.paragraph_format.space_before = Pt(8 if level == 1 else 0)
            p.paragraph_format.left_indent = Cm(0 if level == 1 else 0.7)
            add_hyperlink(p, text, anchor=bookmark, bold=(level == 1), size=11 if level == 1 else 10.5)
        self.pending_break = True

    def footer(self, text: str, cover: bool):
        sec = self.doc.sections[0]
        sec.different_first_page_header_footer = cover
        fp = sec.footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        add_run(fp, f"{text} · стр. ", size=8.5, color=GREY)
        add_page_field(fp)


def build_docx(md: str, out: Path, base: Path, *, footer_title: str | None = None, cover: bool | None = None) -> Path:
    lines = md.replace("\r\n", "\n").split("\n")
    title, subtitle, toc = prescan(lines)
    use_cover = cover if cover is not None else (len(md) > 20000 and len(toc) >= 8)
    h1_breaks = sum(1 for e in toc if e[0] == 1) >= 2
    b = Builder(base, footer_title, use_cover)
    doc = b.doc
    doc.core_properties.title = title or out.stem
    doc.core_properties.author = "ООО «Диантус»"
    doc.core_properties.language = "ru-RU"
    b.footer(footer_title or (title[:70] if title else "Диантус"), use_cover)

    if use_cover:
        b.cover(title, subtitle)
        b.toc(toc)
    elif title:
        t = b.para()
        t.paragraph_format.space_after = Pt(4)
        add_run(t, title, bold=True, size=22, color=GREEN)
        if subtitle:
            s = b.para()
            s.paragraph_format.space_after = Pt(10)
            add_run(s, strip_md(subtitle), size=11.5, color=GREY)

    i, n = 0, 0
    first_h1 = True
    para: list[str] = []

    def flush():
        if para:
            b.paragraph(" ".join(para))
            para.clear()

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            flush()
            i += 1
            continue
        if s.startswith("```"):
            flush()
            i += 1
            block = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i].rstrip())
                i += 1
            i += 1
            b.code(block)
            continue
        if s.startswith("<!--"):
            flush()
            if "pagebreak" in s:
                b.pending_break = True
            while "-->" not in lines[i]:
                i += 1
            i += 1
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", s)
        if h:
            flush()
            level, text = min(len(h.group(1)), 3), h.group(2).strip()
            i += 1
            if level == 1 and first_h1:
                first_h1 = False
                while i < len(lines) and not lines[i].strip():
                    i += 1
                if subtitle and i < len(lines) and lines[i].strip() == subtitle:
                    i += 1
                continue
            n += 1
            b.heading(level, text, f"_Toc{n}", h1_breaks)
            continue
        img = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)$", s)
        if img:
            flush()
            caption, pct = img.group(1), None
            wm = re.search(r"\s*\{w=(\d+)\}\s*$", caption)
            if wm:
                pct, caption = int(wm.group(1)), caption[:wm.start()]
            b.image(img.group(2), caption, pct)
            i += 1
            continue
        if s.startswith(">"):
            flush()
            block = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                block.append(lines[i].strip()[1:].strip())
                i += 1
            kind = ""
            m = re.match(r"\*\*(.+?)\*\*", block[0]) if block else None
            if m:
                head = m.group(1).lower()
                kind = "important" if head.startswith("важно") else "tip" if head.startswith("совет") else \
                    "trouble" if head.startswith("если что") else ""
            b.callout([x for x in block if x] or [""], kind)
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            flush()
            head = split_row(line)
            aligns = ["center" if c.startswith(":") and c.endswith(":") else "" for c in split_row(lines[i + 1])]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            b.table(head, rows, aligns)
            continue
        if LIST_RE.match(line):
            flush()
            lst, i = parse_list(lines, i)
            b.lst(lst)
            continue
        para.append(s)
        i += 1
    flush()

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)
    return out


# ───────────────────────── живые тексты сайта (HTML → Markdown) ─────────────────────────

class _HtmlToMd(HTMLParser):
    """Достаточно для текстов, которые отдаёт markdown_lite: заголовки, абзацы, списки, жирный/курсив, ссылки, таблицы."""

    def __init__(self, site: str):
        super().__init__(convert_charrefs=True)
        self.site = site
        self.lines: list[str] = []
        self.cur: list[str] = []
        self.lists: list[list] = []
        self.pending = None
        self.head = 0
        self.href = ""
        self.table: list[list[str]] = []
        self.row: list[str] = []

    def _text(self) -> str:
        t = re.sub(r"\s+", " ", "".join(self.cur)).strip()
        self.cur = []
        return t

    def _flush(self):
        t = self._text()
        if not t:
            return
        if self.head:
            self.lines += ["", "#" * self.head + " " + t, ""]
            self.head = 0
        elif self.pending is not None:
            depth, marker = self.pending
            self.lines.append("   " * depth + marker + " " + t)
            self.pending = None
        else:
            self.lines += ["", t, ""]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5"):
            self._flush()
            self.head = min(int(tag[1]), 3)
        elif tag in ("p", "div", "blockquote", "section", "article"):
            self._flush()
        elif tag in ("ul", "ol"):
            self._flush()
            self.lists.append([tag, 0])
        elif tag == "li":
            self._flush()
            lst = self.lists[-1] if self.lists else ["ul", 0]
            lst[1] += 1
            self.pending = (max(len(self.lists) - 1, 0), "-" if lst[0] == "ul" else f"{lst[1]}.")
        elif tag in ("strong", "b"):
            self.cur.append("**")
        elif tag in ("em", "i"):
            self.cur.append("*")
        elif tag == "code":
            self.cur.append("`")
        elif tag == "a":
            href = a.get("href") or ""
            self.href = self.site + href if href.startswith("/") else href
            self.cur.append("[")
        elif tag == "br":
            self.cur.append(" ")
        elif tag == "table":
            self._flush()
            self.table = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cur = []

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4", "h5", "p", "div", "blockquote", "li", "section", "article"):
            self._flush()
        elif tag in ("ul", "ol"):
            self._flush()
            if self.lists:
                self.lists.pop()
            if not self.lists:
                self.lines.append("")
        elif tag in ("strong", "b"):
            self.cur.append("**")
        elif tag in ("em", "i"):
            self.cur.append("*")
        elif tag == "code":
            self.cur.append("`")
        elif tag == "a":
            self.cur.append(f"]({self.href})")
        elif tag in ("td", "th"):
            self.row.append(self._text().replace("|", "/"))
        elif tag == "tr":
            if self.row:
                self.table.append(self.row)
        elif tag == "table" and self.table:
            width = max(len(r) for r in self.table)
            rows = [r + [""] * (width - len(r)) for r in self.table]
            self.lines.append("")
            self.lines.append("| " + " | ".join(rows[0]) + " |")
            self.lines.append("|" + "---|" * width)
            for r in rows[1:]:
                self.lines.append("| " + " | ".join(r) + " |")
            self.lines.append("")
            self.table = []

    def handle_data(self, data):
        self.cur.append(data)


def html_to_md(fragment: str, site: str = SITE) -> str:
    p = _HtmlToMd(site)
    p.feed(fragment)
    p._flush()
    text = "\n".join(p.lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


SITE_TEXTS = [
    ("/privacy", "1. Политика обработки персональных данных"),
    ("/consent", "2. Согласие на обработку персональных данных"),
    ("/marketing-consent", "3. Согласие на получение рекламы"),
    ("/terms", "4. Условия работы и оферта"),
]


def site_legal_to_docx(out_dir: Path, site: str = SITE) -> list[Path]:
    """Живые юридические тексты сайта (как их видят клиенты, с подставленными реквизитами) → DOCX для юриста."""
    made = []
    for path, name in SITE_TEXTS:
        req = urllib.request.Request(site + path, headers={"User-Agent": "dianthus-docs/1.0"})
        page = urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "replace")
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
        art = re.search(r"<article[^>]*legal-text[^>]*>(.*?)</article>", page, re.S)
        if not h1 or not art:
            raise SystemExit(f"{path}: не нашёл заголовок или текст документа на странице")
        title = re.sub(r"<[^>]+>", "", h1.group(1)).strip()
        meta = re.search(r"(Редакци[яи][^<]{0,120})", page)
        md = f"# {title}\n\n{(meta.group(1).strip() if meta else 'Текст, опубликованный на ' + site + path)}\n\n" + html_to_md(art.group(1), site)
        out = out_dir / "legal" / f"{name}.docx"
        build_docx(md, out, DOCS, footer_title=f"ООО «Диантус» · {title}", cover=False)
        made.append(out)
    return made


def lawyer_packet_to_docx(out: Path) -> Path:
    spec = importlib.util.spec_from_file_location("build_lawyer_packet", ROOT / "scripts" / "build_lawyer_packet.py")
    lp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lp)
    return build_docx(lp.build_markdown(), out, DOCS / "legal", footer_title="Пакет для юриста · ООО «Диантус»", cover=False)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", help="файл .md")
    ap.add_argument("-o", "--out", help="куда положить .docx (по умолчанию docs/docx/<имя>.docx)")
    ap.add_argument("--all", action="store_true", help="все документы проекта")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--no-site", action="store_true", help="с --all: не скачивать юридические тексты с живого сайта")
    args = ap.parse_args()
    out_dir = Path(args.out_dir)

    if args.all:
        made = []
        for src in sorted(DOCS.glob("*.md")) + sorted((DOCS / "legal").glob("*.md")):
            if src.name.startswith("_"):
                continue
            sub = "legal" if src.parent.name == "legal" else ""
            out = out_dir / sub / (src.stem + ".docx")
            build_docx(src.read_text(encoding="utf-8"), out, src.parent)
            made.append(out)
        made.append(lawyer_packet_to_docx(out_dir / "legal" / "ПАКЕТ_ДЛЯ_ЮРИСТА.docx"))
        if not args.no_site:
            try:
                made += site_legal_to_docx(out_dir)
            except Exception as exc:                          # noqa: BLE001
                print(f"! юридические тексты сайта не скачаны: {exc}")
        for m in made:
            print(f"{m.relative_to(out_dir.parent) if out_dir.parent in m.parents else m}  {m.stat().st_size // 1024} КБ")
        return 0

    if not args.source:
        ap.error("укажите файл .md или --all")
    src = Path(args.source)
    out = Path(args.out) if args.out else out_dir / (src.stem + ".docx")
    build_docx(src.read_text(encoding="utf-8"), out, src.parent)
    print(f"DOCX: {out} ({out.stat().st_size // 1024} КБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
