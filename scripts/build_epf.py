#!/usr/bin/env python
"""Сборка внешней обработки 1С (.epf) из исходного текста БЕЗ Конфигуратора.

Внутри .epf лежит контейнер 1С с текстом модуля объекта. Скрипт берёт готовый рабочий .epf (шаблон), подменяет в нём только текст
модуля и записывает новый файл. Структура обработки (имя, реквизиты, формы) остаётся от шаблона.

    python scripts/build_epf.py docs/1c_kit/ОбменССайтом_v3.epf docs/1c_kit/ObmenSSaitom.bsl новый.epf
    python scripts/build_epf.py --check docs/1c_kit/ОбменССайтом_v3.epf docs/1c_kit/ObmenSSaitom.bsl

--check ничего не пишет: сверяет, что модуль внутри .epf совпадает с исходным текстом.

ВАЖНО
* 1С кеширует внешнюю обработку по ИМЕНИ ФАЙЛА: новая сборка под тем же именем в уже открытом сеансе может выполнить старый код.
  Для проверки называйте сборку по-новому, а в чистом сеансе 1С откройте файл и убедитесь, что меню появилось без ошибок
  (платформа проверяет модуль при открытии и покажет ошибку компиляции).
* Скрипт не проверяет синтаксис 1С; для этого есть scripts/bsl_lint_names.py и scripts/bsl_lint_structure.py.
"""
import struct
import sys
import zlib

END = 0x7FFFFFFF
BOM = b"\xef\xbb\xbf"


def _read_block(data: bytes, addr: int):
    """Блок контейнера: заголовок 31 байт «\\r\\nXXXXXXXX XXXXXXXX XXXXXXXX \\r\\n» (размер документа, размер страницы, следующая)."""
    hdr = data[addr:addr + 31]
    if hdr[:2] != b"\r\n" or hdr[-2:] != b"\r\n":
        raise ValueError(f"не контейнер 1С: плохой заголовок блока по смещению {addr}")
    doc_size = int(hdr[2:10], 16)
    page_size = int(hdr[11:19], 16)
    nxt = int(hdr[20:28], 16)
    out = data[addr + 31:addr + 31 + min(page_size, doc_size)]
    remaining = doc_size - len(out)
    while remaining > 0 and nxt != END:
        hdr = data[nxt:nxt + 31]
        psz = int(hdr[11:19], 16)
        n2 = int(hdr[20:28], 16)
        take = min(psz, remaining)
        out += data[nxt + 31:nxt + 31 + take]
        remaining -= take
        nxt = n2
    return out


def parse_container(data: bytes) -> dict:
    first = struct.unpack_from("<IIII", data, 0)
    toc = _read_block(data, 16)
    entries = []
    for i in range(0, len(toc), 12):
        header_addr, body_addr, _ = struct.unpack_from("<III", toc, i)
        if header_addr == 0 and body_addr == 0:
            continue
        header = _read_block(data, header_addr)
        name = header[20:].decode("utf-16-le").rstrip("\x00")
        entries.append({"name": name, "header": header, "body": _read_block(data, body_addr)})
    return {"first": first, "entries": entries}


def _block(doc: bytes, min_page: int = 512) -> bytes:
    page = max(min_page, len(doc))
    head = b"\r\n" + b"%08x" % len(doc) + b" " + b"%08x" % page + b" " + b"%08x" % END + b" \r\n"
    return head + doc + b"\x00" * (page - len(doc))


def build_container(first, entries, toc_min_page: int) -> bytes:
    out = bytearray(struct.pack("<IIII", *first))
    toc_total = 31 + max(toc_min_page, 12 * len(entries))
    cursor = len(out) + toc_total
    layout = []
    for e in entries:
        header_block = _block(e["header"], 0)
        body_block = _block(e["body"])
        layout.append((cursor, cursor + len(header_block), header_block, body_block))
        cursor += len(header_block) + len(body_block)
    toc = b"".join(struct.pack("<III", ha, ba, END) for ha, ba, _, _ in layout)
    out += _block(toc, toc_min_page)
    for _, _, header_block, body_block in layout:
        out += header_block + body_block
    return bytes(out)


def _inflate(b: bytes) -> bytes:
    return zlib.decompress(b, -15)


def _deflate(b: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(b) + c.flush()


def _module_entry(outer: dict) -> dict:
    found = [e for e in outer["entries"] if e["name"].endswith(".0")]
    if len(found) != 1:
        raise ValueError("в .epf не нашёл единственный модуль объекта (<guid>.0)")
    return found[0]


def read_module_text(epf_path: str) -> bytes:
    """Текст модуля объекта из .epf (без BOM, с переводами строк CRLF)."""
    outer = parse_container(open(epf_path, "rb").read())
    inner = parse_container(_inflate(_module_entry(outer)["body"]))
    text = [e for e in inner["entries"] if e["name"] == "text"]
    if len(text) != 1:
        raise ValueError("в модуле объекта нет текста")
    body = text[0]["body"]
    return body[3:] if body.startswith(BOM) else body


def normalize(source: bytes) -> bytes:
    if source.startswith(BOM):
        source = source[3:]
    return source.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def build(template_path: str, source_path: str, out_path: str) -> int:
    outer = parse_container(open(template_path, "rb").read())
    entry = _module_entry(outer)
    inner = parse_container(_inflate(entry["body"]))
    text = [e for e in inner["entries"] if e["name"] == "text"]
    if len(text) != 1:
        raise ValueError("в шаблоне нет текста модуля")
    text[0]["body"] = BOM + normalize(open(source_path, "rb").read())
    entry["body"] = _deflate(build_container(inner["first"], inner["entries"], 0))
    data = build_container(outer["first"], outer["entries"], 512)
    open(out_path, "wb").write(data)
    return len(data)


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "--check":
        same = read_module_text(argv[1]) == normalize(open(argv[2], "rb").read())
        print("совпадает" if same else "НЕ совпадает: .epf собран из другого текста")
        return 0 if same else 1
    if len(argv) != 3:
        print(__doc__)
        return 2
    size = build(*argv)
    ok = read_module_text(argv[2]) == normalize(open(argv[1], "rb").read())
    print(f"записано {argv[2]}: {size} байт; модуль совпадает с исходным текстом: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
