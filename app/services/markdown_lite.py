# app/services/markdown_lite.py
"""Минимальная и БЕЗОПАСНАЯ разметка для юридических текстов и памяток.

Юрист или владелец правит текст политики в админке обычным текстом; страница
показывает его оформленным. Библиотеку Markdown не подключаем: ей нужна
отдельная защита от вставки HTML. Здесь весь текст сначала экранируется,
и только потом из безопасных кусочков строится разметка — вставить <script>
или javascript:-ссылку невозможно.

Поддерживается:
    ## Заголовок раздела / ### Подзаголовок
    - пункт списка          1. нумерованный пункт
    **жирный**   *курсив*   `код`   [ссылка](https://…)   ---  (линия)
    ```                     блок кода (текст внутри показывается как есть)
    | колонка | колонка |   таблица (вторая строка — |---|---|)
Пустая строка разделяет абзацы.
"""
import html
import re

from markupsafe import Markup

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_CODE = re.compile(r"`([^`]+)`")
_SAFE_URL = re.compile(r"^(https?://|mailto:|tel:|/(?!/)|#)", re.IGNORECASE)
_OL = re.compile(r"^\d+[.)]\s+(.*)$")
_TABLE_SEP = re.compile(r"^:?-{2,}:?$")


def _link(match: "re.Match[str]") -> str:
    label, url = match.group(1), html.unescape(match.group(2))
    if not _SAFE_URL.match(url):
        return label                               # javascript:, data: и т.п. — только текст
    external = url.lower().startswith(("http://", "https://"))
    attrs = ' target="_blank" rel="noopener noreferrer"' if external else ""
    return f'<a href="{html.escape(url, quote=True)}"{attrs}>{label}</a>'


def _inline(text: str) -> str:
    out = html.escape(text.replace("\x00", ""), quote=True)
    # Код и ссылки откладываем «в сторонку»: иначе * или _ внутри адреса/кода
    # превратились бы в <em>/<strong> прямо в атрибуте href и сломали бы разметку.
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    out = _CODE.sub(lambda m: keep(f"<code>{m.group(1)}</code>"), out)
    out = _LINK.sub(lambda m: keep(_link(m)), out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], out)


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _render_table(rows: list[str]) -> str:
    cells = [_split_row(r) for r in rows]
    head: list[str] | None = None
    if len(cells) >= 2 and all(_TABLE_SEP.match(c) for c in cells[1] if c):
        head, cells = cells[0], cells[2:]
    out = ['<div class="table-responsive"><table class="table table-sm table-bordered">']
    if head:
        out.append("<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead>")
    out.append("<tbody>")
    for row in cells:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def render(text: str) -> Markup:
    """Текст → безопасный HTML (Markup — шаблону не нужен фильтр |safe)."""
    blocks: list[str] = []
    paragraph: list[str] = []
    list_kind: str | None = None
    items: list[str] = []
    table: list[str] = []
    code: list[str] | None = None

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append("<p>" + " ".join(_inline(p) for p in paragraph) + "</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_kind
        if list_kind and items:
            tag = "ol" if list_kind == "ol" else "ul"
            blocks.append(f"<{tag}>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + f"</{tag}>")
        items.clear()
        list_kind = None

    def flush_table() -> None:
        if table:
            blocks.append(_render_table(table))
            table.clear()

    def flush_all() -> None:
        flush_paragraph()
        flush_list()
        flush_table()

    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = raw.strip()

        if code is not None:                                    # внутри блока кода
            if stripped.startswith("```"):
                blocks.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code = None
            else:
                code.append(raw.rstrip())
            continue
        if stripped.startswith("```"):
            flush_all()
            code = []
            continue

        if not stripped:
            flush_all()
            continue
        if stripped == "---":
            flush_all()
            blocks.append("<hr>")
            continue
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 2:
            flush_paragraph()
            flush_list()
            table.append(stripped)
            continue
        flush_table()

        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            flush_all()
            # h1 занят заголовком страницы, поэтому «#» и «##» → h2, «###» → h3
            level = max(2, len(heading.group(1)))
            blocks.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^[-•]\s+(.*)$", stripped)
        ordered = _OL.match(stripped)
        if bullet or ordered:
            flush_paragraph()
            kind = "ul" if bullet else "ol"
            if list_kind and list_kind != kind:
                flush_list()
            list_kind = kind
            items.append((bullet or ordered).group(1))
            continue
        flush_list()
        paragraph.append(stripped)

    if code is not None:                                        # незакрытый блок кода
        blocks.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
    flush_all()
    return Markup("\n".join(blocks))
