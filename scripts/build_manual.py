#!/usr/bin/env python
"""Собирает «Руководство по сайту» из docs/РУКОВОДСТВО.md в один самодостаточный HTML-файл и (по желанию) в PDF.

    python scripts/build_manual.py                          # docs/_build/РУКОВОДСТВО.html
    python scripts/build_manual.py --pdf                    # то же + PDF через Chrome или Edge (без интернета)
    python scripts/build_manual.py --pdf "D:/Документы/Руководство.pdf"

Картинки встраиваются в HTML, поэтому файл можно отправить по почте как есть. Только стандартная библиотека.
Разметка — небольшая часть Markdown (её хватает руководству): заголовки #/##/###, абзацы, списки (с вложением),
таблицы, цитаты-подсказки («> **Важно.** …»), картинки ![подпись](путь), **жирный**, *курсив*, `код`, [ссылка](адрес).
"""
from __future__ import annotations

import argparse
import base64
import html
import mimetypes
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "РУКОВОДСТВО.md"
LOGO = ROOT / "app" / "static" / "emblem.webp"

mimetypes.add_type("image/webp", ".webp")

CSS = """
@page { size: A4; margin: 16mm 15mm 18mm 15mm;
        @bottom-center { content: "Руководство по сайту «Диантус» · стр. " counter(page); font: 8pt 'Segoe UI', Arial, sans-serif; color: #8a8a80; } }
@page :first { @bottom-center { content: none; } }
* { box-sizing: border-box; }
html { font: 10.5pt/1.5 'Segoe UI', 'Helvetica Neue', Arial, sans-serif; color: #26302a; }
body { margin: 0; }
h1, h2, h3 { color: #35552f; line-height: 1.25; font-weight: 700; }
h1 { font-size: 21pt; margin: 0 0 10px; padding-bottom: 6px; border-bottom: 3px solid #c9a56a; break-before: page; break-after: avoid; }
h2 { font-size: 14.5pt; margin: 22px 0 8px; break-after: avoid; }
h3 { font-size: 12pt; margin: 16px 0 6px; break-after: avoid; }
p { margin: 6px 0 8px; orphans: 3; widows: 3; }
p.lead { break-after: avoid; }
ul, ol { margin: 6px 0 10px; padding-left: 22px; }
li { margin: 3px 0; break-inside: avoid; }
li > ul, li > ol { margin: 3px 0 4px; }
a { color: #35552f; }
code { font: 9pt Consolas, 'Courier New', monospace; background: #f0eee6; border-radius: 3px; padding: 0 3px; }
strong { color: #1d2a1b; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 14px; font-size: 9.3pt; }
th { background: #e8efe2; color: #26402a; text-align: left; }
th, td { border: 1px solid #cbd5c4; padding: 4px 7px; vertical-align: top; }
tr { break-inside: avoid; }
figure { margin: 10px 0 14px; text-align: center; break-inside: avoid; }
figure img { max-width: 100%; max-height: 225mm; border: 1px solid #d6cfbe; border-radius: 6px; }
figcaption { font-size: 8.8pt; color: #6c6c62; margin-top: 3px; }
.callout { margin: 10px 0 12px; padding: 8px 12px; border-left: 5px solid #6c8a5f; background: #f1f5ee; border-radius: 0 6px 6px 0; break-inside: avoid; }
.callout p { margin: 3px 0; }
.callout.important { border-color: #c0602c; background: #fbf0e8; }
.callout.tip { border-color: #4e8a5a; background: #edf6ef; }
.callout.trouble { border-color: #a63a3a; background: #faeded; }
.cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; }
.cover img { width: 62mm; height: auto; margin-bottom: 16mm; border: 0; border-radius: 0; }
.cover h1 { font-size: 30pt; border: 0; break-before: auto; margin-bottom: 8mm; }
.cover p { font-size: 13pt; color: #55604f; max-width: 140mm; }
.cover .date { margin-top: 14mm; font-size: 10pt; color: #8a8a80; }
.toc { break-after: page; }
.toc h1 { break-before: auto; }
.toc ul { list-style: none; padding-left: 0; }
.toc li.part { margin-top: 10px; font-weight: 700; }
.toc li.chapter { margin-left: 16px; font-weight: 400; margin-top: 1px; }
.toc a { text-decoration: none; }
"""


# ───────────────────────── разбор Markdown ─────────────────────────

def inline(text: str) -> str:
    out = html.escape(text, quote=False)
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    out = re.sub(r"`([^`]+)`", lambda m: keep(f"<code>{m.group(1)}</code>"), out)
    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: keep(f'<a href="{m.group(2)}">{m.group(1)}</a>'), out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<em>\1</em>", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], out)


class Doc:
    """Результат разбора: HTML тела и список заголовков для оглавления."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.toc: list[tuple[int, str, str]] = []        # (уровень, id, текст)
        self.images: list[str] = []
        self.title = ""
        self.subtitle = ""
        self._n = 0

    def heading_id(self) -> str:
        self._n += 1
        return f"s{self._n}"


LIST_RE = re.compile(r"^(\s*)([*\-]|\d+[.)])\s+(.*)$")


def parse_list(lines: list[str], i: int, doc: Doc, base: Path) -> tuple[str, int]:
    """Список (в том числе вложенный) начиная со строки i; возвращает (html, следующая строка)."""
    first = LIST_RE.match(lines[i])
    indent = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    start = int(first.group(2).rstrip(".)")) if ordered else 1
    items: list[str] = []
    while i < len(lines):
        m = LIST_RE.match(lines[i])
        if not m or len(m.group(1)) != indent or (m.group(2)[0].isdigit() != ordered):
            break
        body = [m.group(3)]
        i += 1
        nested = ""
        while i < len(lines):
            line = lines[i]
            if not line.strip():                                    # пустая строка: продолжается ли список?
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
                sub, i = parse_list(lines, i, doc, base)
                nested += sub
                continue
            if lm and len(lm.group(1)) <= indent:
                break
            if line.startswith(" " * (indent + 1)) and not lm:      # продолжение текста пункта
                body.append(line.strip())
                i += 1
                continue
            break
        items.append("<li>" + inline(" ".join(body)) + nested + "</li>")
    tag = "ol" if ordered else "ul"
    attr = f' start="{start}"' if ordered and start != 1 else ""
    return f"<{tag}{attr}>" + "".join(items) + f"</{tag}>", i


def parse_table(lines: list[str], i: int) -> tuple[str, int]:
    def cells(row: str) -> list[str]:
        return [c.strip() for c in row.strip().strip("|").split("|")]

    head = cells(lines[i])
    aligns = []
    for c in cells(lines[i + 1]):
        aligns.append("center" if c.startswith(":") and c.endswith(":") else "")
    i += 2
    rows = []
    while i < len(lines) and lines[i].strip().startswith("|"):
        rows.append(cells(lines[i]))
        i += 1

    def td(tag: str, row: list[str]) -> str:
        out = []
        for n, c in enumerate(row):
            al = f' style="text-align:{aligns[n]}"' if n < len(aligns) and aligns[n] else ""
            out.append(f"<{tag}{al}>{inline(c)}</{tag}>")
        return "".join(out)

    body = "".join("<tr>" + td("td", r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{td('th', head)}</tr></thead><tbody>{body}</tbody></table>", i


def convert(text: str, base: Path = ROOT / "docs") -> Doc:
    doc = Doc()
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    first_h1 = True
    para: list[str] = []

    def flush() -> None:
        if para:
            text_ = " ".join(para)
            # абзац-«подводка» («Если забыли пароль:») не должен остаться внизу листа без своего списка
            cls = ' class="lead"' if text_.rstrip().endswith(":") else ""
            doc.parts.append(f"<p{cls}>" + inline(text_) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush()
            i += 1
            continue
        if stripped.startswith("<!--"):
            flush()
            if "pagebreak" in stripped:
                doc.parts.append('<div style="break-after:page"></div>')
            while "-->" not in lines[i]:
                i += 1
            i += 1
            continue
        h = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if h:
            flush()
            level, title = len(h.group(1)), h.group(2).strip()
            if level == 1 and first_h1:
                first_h1 = False
                doc.title = title
                i += 1
                while i < len(lines) and not lines[i].strip():
                    i += 1
                if i < len(lines) and not lines[i].lstrip().startswith("#"):
                    doc.subtitle = lines[i].strip()
                    i += 1
                continue
            sid = doc.heading_id()
            if level <= 2:
                doc.toc.append((level, sid, title))
            doc.parts.append(f'<h{level} id="{sid}">{inline(title)}</h{level}>')
            i += 1
            continue
        img = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)$", stripped)
        if img:
            flush()
            doc.images.append(img.group(2))
            pieces = prepare_image(base / img.group(2))
            alt = html.escape(img.group(1), quote=True)
            for n, uri in enumerate(pieces, 1):
                note = "" if len(pieces) == 1 else (" (начало)" if n == 1 else f" (продолжение, часть {n} из {len(pieces)})")
                doc.parts.append(f'<figure><img src="{uri}" alt="{alt}"><figcaption>{html.escape(img.group(1))}{note}</figcaption></figure>')
            i += 1
            continue
        if stripped.startswith(">"):
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
            doc.parts.append(f'<div class="callout {kind}"><p>' + inline(" ".join(block)) + "</p></div>")
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            flush()
            out, i = parse_table(lines, i)
            doc.parts.append(out)
            continue
        if LIST_RE.match(line):
            flush()
            out, i = parse_list(lines, i, doc, base)
            doc.parts.append(out)
            continue
        para.append(stripped)
        i += 1
    flush()
    return doc


def data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def footer_top(img) -> int | None:
    """Где на снимке начинается подвал сайта (он не нужен в руководстве): ищем строку, с которой левая кромка
    становится почти белой и остаётся такой до низа."""
    w, h = img.size
    px = img.load()
    white = [min(sum(px[x, y][c] for x in range(2, 10)) // 8 for c in range(3)) >= 249 for y in range(h)]
    for y in range(int(h * 0.30), h - 5):
        if white[y] and not white[y - 1] and sum(white[y:]) / len(white[y:]) >= 0.97:
            return y
    return None


def quiet_row(img, target: int, window: int) -> int:
    """Строка около target, где картинка почти однородна (между карточками): по ней безопасно резать."""
    from PIL import ImageStat
    w, h = img.size
    best, best_score = target, None
    for y in range(max(1, target - window), min(h - 1, target + window)):
        score = sum(ImageStat.Stat(img.crop((0, y, w, y + 1))).stddev) + abs(y - target) * 0.02
        if best_score is None or score < best_score:
            best, best_score = y, score
    return best


def prepare_image(path: Path) -> list[str]:
    """Снимок экрана → один или несколько data:-адресов JPEG.

    • WebP переводится в JPEG: браузер кладёт JPEG в PDF как есть, и файл получается в разы меньше;
      прозрачность заменяется белым (иначе фон станет чёрным).
    • У снимков всей страницы отрезается подвал сайта.
    • Слишком высокий снимок режется на части по «тихим» строкам, чтобы каждая часть умещалась на листе
      на всю ширину (иначе буквы на картинке получаются мелкими или остаётся пустое место)."""
    if not path.exists():
        raise FileNotFoundError(f"нет картинки {path}")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        import io

        from PIL import Image
        img = Image.open(path).convert("RGBA")
        flat = Image.new("RGB", img.size, "white")
        flat.paste(img, mask=img.split()[-1])
        img = flat
        if path.stem[:2].isdigit():                       # снимки экрана; логотип и прочее не трогаем
            cut = footer_top(img)
            if cut:
                img = img.crop((0, 0, img.width, cut))
        w, h = img.size
        parts = max(1, -(-h // int(w * 1.22)))              # на сколько частей резать (высота части ≤ 1,22 ширины)
        bounds = [0]
        for k in range(1, parts):
            bounds.append(quiet_row(img, round(h * k / parts), int(h / parts * 0.18)))
        bounds.append(h)
        out = []
        for top, bottom in zip(bounds, bounds[1:]):
            buf = io.BytesIO()
            img.crop((0, top, w, bottom)).save(buf, "JPEG", quality=84, optimize=True)
            out.append(data_uri(buf.getvalue(), "image/jpeg"))
        return out
    except ImportError:                                     # нет Pillow — встраиваем файл как есть
        return [data_uri(path.read_bytes(), mime)]


def embed(path: Path) -> str:
    return prepare_image(path)[0]


def render(doc: Doc) -> str:
    toc_items = []
    for level, sid, title in doc.toc:
        toc_items.append(f'<li class="{"part" if level == 1 else "chapter"}"><a href="#{sid}">{inline(title)}</a></li>')
    logo = f'<img src="{prepare_image(LOGO)[0]}" alt="">' if LOGO.exists() else ""
    cover = (f'<section class="cover">{logo}<h1>{inline(doc.title)}</h1><p>{inline(doc.subtitle)}</p>'
             f'<p class="date">dianthus-opt.ru</p></section>')
    toc = f'<section class="toc"><h1>Содержание</h1><ul>{"".join(toc_items)}</ul></section>'
    return ("<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(doc.title)}</title><style>{CSS}</style></head><body>"
            f"{cover}{toc}{''.join(doc.parts)}</body></html>")


# ───────────────────────── PDF ─────────────────────────

def find_browser() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def make_pdf(html_path: Path, pdf_path: Path) -> None:
    browser = find_browser()
    if not browser:
        raise SystemExit("Не нашёл Chrome или Edge: установите любой из них или откройте HTML и выберите «Печать → Сохранить как PDF».")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        cmd = [browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--user-data-dir={profile}",
               f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()]
        subprocess.run(cmd, check=True, timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not pdf_path.exists() or pdf_path.stat().st_size < 10_000:
        raise SystemExit("PDF не создан (браузер вернул пустой файл).")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default=str(ROOT / "docs" / "_build"), help="куда положить HTML")
    ap.add_argument("--pdf", nargs="?", const="auto", help="ещё и PDF (без значения — рядом с HTML)")
    args = ap.parse_args()

    doc = convert(SOURCE.read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "РУКОВОДСТВО.html"
    html_path.write_text(render(doc), encoding="utf-8")
    print(f"HTML: {html_path} ({html_path.stat().st_size // 1024} КБ), картинок: {len(doc.images)}, разделов: {len(doc.toc)}")
    if args.pdf:
        pdf_path = out_dir / "РУКОВОДСТВО.pdf" if args.pdf == "auto" else Path(args.pdf)
        make_pdf(html_path, pdf_path)
        print(f"PDF:  {pdf_path} ({pdf_path.stat().st_size // 1024} КБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
