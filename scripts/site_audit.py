#!/usr/bin/env python
"""Проверка сайта одной командой (только чтение, ничего не меняет и не отправляет).

    python scripts/site_audit.py https://dianthus-opt.ru
    python scripts/site_audit.py http://127.0.0.1:8765 --products 10

Что проверяется на каждой странице: код ответа, сжатие, заголовок и описание (длина, неповторяемость),
один заголовок h1 и порядок заголовков, canonical, теги Open Graph и Twitter, разметка JSON-LD (разбирается ли),
у картинок — alt, размеры и отложенная загрузка, у полей форм — подписи, у ссылок и кнопок — видимый текст.
Затем: sitemap.xml (каждый адрес отвечает 200 и указывает canonical на себя), robots.txt, кэш статических файлов,
картинка для превью в соцсетях.

Только стандартная библиотека. Код выхода 0 — серьёзных замечаний нет, 1 — есть.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

UA = "dianthus-site-audit/1.0 (own-site check, read-only)"  # только латиница: заголовки HTTP не принимают кириллицу

# страницы, которые должны быть в поиске / которые должны быть закрыты от поиска
PUBLIC_PAGES = ["/", "/catalog", "/contacts", "/deadlines", "/privacy", "/terms", "/consent", "/marketing-consent",
                "/help", "/register"]
NOINDEX_PAGES = ["/login", "/forgot"]
SECTION_SLUGS = ["rozy", "kustovye-rozy", "ekvadorskie-rozy", "kenijskie-rozy", "rossijskie-rozy",
                 "hrizantemy", "gvozdiki", "gorshechnye-rasteniya", "zelen"]


class PageParser(HTMLParser):
    """Собирает из разметки всё, что нужно проверкам. Без внешних библиотек."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_attrs: dict[str, str] = {}
        self.title = ""
        self.meta: dict[str, str] = {}
        self.links: list[dict[str, str]] = []
        self.headings: list[tuple[int, str]] = []
        self.images: list[dict[str, str]] = []
        self.anchors: list[dict] = []
        self.inputs: list[dict] = []
        self.label_for: set[str] = set()
        self.buttons: list[dict] = []
        self.scripts: list[dict[str, str]] = []
        self.ld_blocks: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._in_ld = False
        self._ld_buf: list[str] = []
        self._in_script_or_style = 0
        self._heading: tuple[int, list[str]] | None = None
        self._anchor: dict | None = None
        self._button: dict | None = None
        self._label_depth = 0

    # --- разбор тегов -------------------------------------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs_list) -> None:
        a = {k: (v if v is not None else "") for k, v in attrs_list}
        if tag == "html":
            self.html_attrs = a
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = a.get("name") or a.get("property") or a.get("http-equiv")
            if key:
                self.meta[key.lower()] = a.get("content", "")
        elif tag == "link":
            self.links.append(a)
        elif tag in ("script", "style"):
            self._in_script_or_style += 1
            if tag == "script":
                self.scripts.append(a)
                if a.get("type", "").lower() == "application/ld+json":
                    self._in_ld = True
                    self._ld_buf = []
        elif re.fullmatch(r"h[1-6]", tag):
            self._heading = (int(tag[1]), [])
        elif tag == "a":
            self._anchor = {"attrs": a, "text": [], "has_img_alt": False}
        elif tag == "img":
            self.images.append(a)
            if self._anchor is not None and (a.get("alt") or "").strip():
                self._anchor["has_img_alt"] = True
            if self._button is not None and (a.get("alt") or "").strip():
                self._button["has_img_alt"] = True
        elif tag == "label":
            self._label_depth += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        elif tag in ("input", "select", "textarea"):
            self.inputs.append({"attrs": a, "tag": tag, "in_label": self._label_depth > 0})
        elif tag == "button":
            self._button = {"attrs": a, "text": [], "has_img_alt": False}

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in ("script", "style"):
            self._in_script_or_style = max(0, self._in_script_or_style - 1)
            if tag == "script" and self._in_ld:
                self.ld_blocks.append("".join(self._ld_buf))
                self._in_ld = False
        elif re.fullmatch(r"h[1-6]", tag) and self._heading is not None:
            self.headings.append((self._heading[0], re.sub(r"\s+", " ", "".join(self._heading[1])).strip()))
            self._heading = None
        elif tag == "a" and self._anchor is not None:
            self.anchors.append(self._anchor)
            self._anchor = None
        elif tag == "label":
            self._label_depth = max(0, self._label_depth - 1)
        elif tag == "button" and self._button is not None:
            self.buttons.append(self._button)
            self._button = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._in_ld:
            self._ld_buf.append(data)
        if self._in_script_or_style:
            return
        if self._heading is not None:
            self._heading[1].append(data)
        if self._anchor is not None:
            self._anchor["text"].append(data)
        if self._button is not None:
            self._button["text"].append(data)
        self.text_parts.append(data)

    def visible_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.text_parts)).strip()


class Fetcher:
    def __init__(self, base: str, delay: float) -> None:
        self.base = base.rstrip("/")
        self.delay = delay
        self.cache: dict[str, tuple[int, dict[str, str], bytes]] = {}

    def get(self, path_or_url: str, accept: str = "text/html,*/*;q=0.8", gzip_ok: bool = True):
        url = path_or_url if path_or_url.startswith("http") else self.base + path_or_url
        key = f"{url}|{accept}|{gzip_ok}"
        if key in self.cache:
            return self.cache[key]
        headers = {"User-Agent": UA, "Accept": accept}
        if gzip_ok:
            headers["Accept-Encoding"] = "gzip, deflate"
        result: tuple[int, dict[str, str], bytes] = (0, {}, b"")
        for attempt in range(3):
            try:
                time.sleep(self.delay)
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                    result = (r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
                break
            except urllib.error.HTTPError as e:
                result = (e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())
                break
            except Exception as e:  # сеть: повторим
                result = (0, {"error": str(e)}, b"")
                time.sleep(2 * (attempt + 1))
        status, hdr, body = result
        enc = hdr.get("content-encoding", "").lower()
        if enc == "gzip":
            try:
                body = gzip.decompress(body)
            except Exception:
                pass
        elif enc == "deflate":
            try:
                body = zlib.decompress(body)
            except Exception:
                pass
        self.cache[key] = (status, hdr, body)
        return self.cache[key]


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.serious = 0
        self.minor = 0

    def page(self, title: str) -> None:
        self.lines.append("")
        self.lines.append("== " + title)

    def info(self, text: str) -> None:
        self.lines.append("   " + text)

    def bad(self, text: str) -> None:
        self.serious += 1
        self.lines.append("   [!] " + text)

    def warn(self, text: str) -> None:
        self.minor += 1
        self.lines.append("   [~] " + text)


def check_page(path: str, fetch: Fetcher, rep: Report, seen: dict, expect_noindex: bool = False) -> PageParser | None:
    status, hdr, body = fetch.get(path)
    rep.page(f"{path}  ->  {status}, {len(body) // 1024 or 1} КБ")
    if status != 200:
        rep.bad(f"ответ {status}, ожидался 200" + (f" ({hdr['error']})" if hdr.get("error") else ""))
        return None
    if "html" not in hdr.get("content-type", ""):
        rep.bad("это не HTML: " + hdr.get("content-type", ""))
        return None
    if hdr.get("content-encoding", "").lower() not in ("gzip", "br", "deflate"):
        rep.warn("страница отдаётся без сжатия")
    p = PageParser()
    p.feed(body.decode("utf-8", "replace"))
    text = p.visible_text()

    # --- заголовок и описание ------------------------------------------------------------------------------------
    title = re.sub(r"\s+", " ", p.title).strip()
    desc = p.meta.get("description", "").strip()
    robots = p.meta.get("robots", "").lower()
    if not title:
        rep.bad("нет <title>")
    else:
        if not 20 <= len(title) <= 75:
            rep.warn(f"длина title {len(title)} симв. (хорошо 20–75): «{title}»")
        if title in seen.setdefault("titles", {}) and seen["titles"][title] != path:
            rep.warn(f"такой же title на {seen['titles'][title]}")
        seen["titles"].setdefault(title, path)
    if not expect_noindex:
        if not desc:
            rep.bad("нет meta description")
        else:
            if not 70 <= len(desc) <= 175:
                rep.warn(f"длина description {len(desc)} симв. (хорошо 70–175)")
            if desc in seen.setdefault("descs", {}) and seen["descs"][desc] != path:
                rep.warn(f"такое же description на {seen['descs'][desc]}")
            seen["descs"].setdefault(desc, path)

    # --- индексация ----------------------------------------------------------------------------------------------
    if expect_noindex:
        if "noindex" not in robots:
            rep.bad("страница должна быть закрыта от индексации (noindex), а не закрыта")
    elif "noindex" in robots:
        rep.warn("страница закрыта от индексации (noindex)")
    canon = next((l.get("href", "") for l in p.links if "canonical" in l.get("rel", "").lower().split()), "")
    if not canon and not expect_noindex:
        rep.bad("нет canonical")
    elif canon and not canon.startswith("http"):
        rep.bad(f"canonical не абсолютный: {canon}")
    if p.html_attrs.get("lang", "") != "ru":
        rep.bad(f"html lang = «{p.html_attrs.get('lang', '')}», нужно ru")
    if "width=device-width" not in p.meta.get("viewport", ""):
        rep.bad("нет viewport для телефона")
    if not p.meta.get("theme-color"):
        rep.warn("нет theme-color")

    # --- заголовки -----------------------------------------------------------------------------------------------
    h1 = [t for lvl, t in p.headings if lvl == 1]
    if len(h1) != 1:
        rep.bad(f"заголовков h1 на странице: {len(h1)} (нужен один)")
    elif len(h1[0]) < 5:
        rep.warn("h1 слишком короткий")
    prev = 0
    for lvl, t in p.headings:
        if prev and lvl > prev + 1:
            rep.warn(f"порядок заголовков: после h{prev} сразу h{lvl} («{t[:40]}»)")
            break
        prev = lvl
    if not expect_noindex and len(text) < 300:
        rep.warn(f"мало текста на странице: {len(text)} симв.")

    # --- соцсети -------------------------------------------------------------------------------------------------
    if not expect_noindex:
        for key in ("og:title", "og:description", "og:image", "og:url", "og:type", "twitter:card"):
            if not p.meta.get(key):
                rep.warn(f"нет {key}")
        img = p.meta.get("og:image", "")
        if img and not img.startswith("http"):
            rep.bad(f"og:image не абсолютный адрес: {img}")

    # --- разметка JSON-LD ----------------------------------------------------------------------------------------
    types: list[str] = []
    for block in p.ld_blocks:
        try:
            data = json.loads(block)
        except Exception as e:
            rep.bad(f"JSON-LD не разбирается: {e}")
            continue
        for item in (data if isinstance(data, list) else [data]):
            t = item.get("@type") if isinstance(item, dict) else None
            types += t if isinstance(t, list) else [t or "?"]
    if types:
        rep.info("разметка: " + ", ".join(types))

    # --- картинки ------------------------------------------------------------------------------------------------
    noalt = [i.get("src", "")[:50] for i in p.images if "alt" not in i]
    nosize = [i.get("src", "")[:50] for i in p.images if not (i.get("width") and i.get("height"))]
    if noalt:
        rep.bad(f"картинок без alt: {len(noalt)} ({noalt[0]}…)")
    if nosize:
        rep.warn(f"картинок без width/height (прыжки вёрстки): {len(nosize)} ({nosize[0]}…)")
    below = [i for n, i in enumerate(p.images) if n >= 2 and i.get("loading") != "lazy" and "data:" not in i.get("src", "")]
    if below:
        rep.warn(f"картинок ниже первого экрана без loading=lazy: {len(below)}")

    # --- формы и ссылки --------------------------------------------------------------------------------------------
    unl = []
    for inp in p.inputs:
        a = inp["attrs"]
        if a.get("type", "text").lower() in ("hidden", "submit", "button", "image", "reset"):
            continue
        if inp["in_label"] or a.get("id", "") in p.label_for or a.get("aria-label") or a.get("aria-labelledby") or a.get("title"):
            continue
        unl.append(a.get("name") or a.get("id") or a.get("type", "?"))
    if unl:
        rep.warn(f"полей формы без подписи (label/aria-label): {', '.join(unl[:6])}")
    # ссылка-дубль с aria-hidden="true" (картинка рядом с названием) скринридер пропускает — это нормально
    emp = [x["attrs"].get("href", "") for x in p.anchors
           if not "".join(x["text"]).strip() and not x["has_img_alt"] and not x["attrs"].get("aria-label")
           and not x["attrs"].get("aria-labelledby") and not x["attrs"].get("title")
           and x["attrs"].get("aria-hidden") != "true"]
    if emp:
        rep.warn(f"ссылок без видимого текста/aria-label: {len(emp)} ({emp[0][:40]})")
    bemp = [x for x in p.buttons if not "".join(x["text"]).strip() and not x["has_img_alt"]
            and not x["attrs"].get("aria-label") and not x["attrs"].get("title")]
    if bemp:
        rep.warn(f"кнопок без текста/aria-label: {len(bemp)}")
    if not any(a["attrs"].get("href", "").startswith("#") and "main" in a["attrs"].get("href", "") for a in p.anchors):
        rep.info("нет ссылки «Перейти к содержимому» (skip link)")
    rep.info(f"h1: {h1[0][:70] if h1 else '—'}; текста {len(text)} симв.; картинок {len(p.images)}; ссылок {len(p.anchors)}")
    return p


def check_assets(page: PageParser, fetch: Fetcher, rep: Report, label: str) -> None:
    """Свои css/js/шрифты/картинки: отвечают ли, сжаты ли, как кэшируются."""
    rep.page(f"Статические файлы страницы {label}")
    urls: set[str] = set()
    for l in page.links:
        rel = l.get("rel", "").lower()
        if ("stylesheet" in rel or "preload" in rel or "icon" in rel or "manifest" in rel) and l.get("href"):
            urls.add(l["href"])
    for s in page.scripts:
        if s.get("src"):
            urls.add(s["src"])
    for u in sorted(urls):
        if u.startswith("http") and not u.startswith(fetch.base):
            rep.info(f"внешний ресурс: {u}")
            continue
        status, hdr, body = fetch.get(u, accept="*/*")
        cc = hdr.get("cache-control", "")
        m = re.search(r"max-age=(\d+)", cc)
        age = int(m.group(1)) if m else 0
        msg = f"{u[:60]}: {status}, {len(body) // 1024 or 1} КБ, cache-control: {cc or '—'}"
        if status != 200:
            rep.bad(msg)
            continue
        ctype = hdr.get("content-type", "")
        compressible = any(x in ctype for x in ("css", "javascript", "json", "svg"))
        if compressible and hdr.get("content-encoding", "") not in ("gzip", "br") and len(body) > 2048:
            rep.warn(msg + " — не сжат")
        elif "v=" in u and age < 2592000 and "fonts" not in u:
            rep.warn(msg + " — адрес с версией, а кэш короче 30 суток")
        else:
            rep.info(msg)


def check_sitemap(fetch: Fetcher, rep: Report, products: int, seen: dict) -> list[str]:
    status, hdr, body = fetch.get("/sitemap.xml", accept="*/*")
    rep.page(f"/sitemap.xml  ->  {status}")
    if status != 200:
        rep.bad("sitemap.xml не отдаётся")
        return []
    locs = re.findall(r"<loc>(.*?)</loc>", body.decode("utf-8", "replace"))
    kinds: dict[str, int] = {}
    for u in locs:
        k = "/" + urllib.parse.urlparse(u).path.strip("/").split("/")[0]
        kinds[k] = kinds.get(k, 0) + 1
    rep.info(f"адресов: {len(locs)}; по разделам: {kinds}")
    if "<lastmod>" not in body.decode("utf-8", "replace"):
        rep.warn("в sitemap нет lastmod (даты изменения) — поисковик реже перепроверяет страницы")
    prods = [u for u in locs if "/product/" in u]
    step = max(1, len(prods) // max(1, products)) if products else 0
    sample = prods[::step][:products] if step else []
    return [urllib.parse.urlparse(u).path for u in sample]


def check_robots(fetch: Fetcher, rep: Report) -> None:
    status, hdr, body = fetch.get("/robots.txt", accept="*/*")
    rep.page(f"/robots.txt  ->  {status}")
    t = body.decode("utf-8", "replace")
    if status != 200:
        rep.bad("robots.txt не отдаётся")
    elif "sitemap:" not in t.lower():
        rep.bad("в robots.txt нет строки Sitemap:")
    else:
        rep.info("есть Sitemap, " + str(len(t.splitlines())) + " строк")


def check_headers(fetch: Fetcher, rep: Report) -> None:
    status, hdr, body = fetch.get("/")
    rep.page("Заголовки безопасности главной страницы")
    wanted = {
        "strict-transport-security": "HSTS", "content-security-policy": "CSP", "x-content-type-options": "nosniff",
        "x-frame-options": "защита от встраивания", "referrer-policy": "Referrer-Policy",
        "permissions-policy": "Permissions-Policy",
    }
    for h, name in wanted.items():
        if hdr.get(h):
            rep.info(f"{name}: есть")
        elif fetch.base.startswith("https") or h != "strict-transport-security":
            rep.warn(f"нет заголовка {h} ({name})")
    if "set-cookie" in hdr:
        rep.info("на главной ставится cookie: " + hdr["set-cookie"].split("=")[0] + " (техническая сессия)")


def check_404(fetch: Fetcher, rep: Report) -> None:
    status, hdr, body = fetch.get("/this-page-does-not-exist-check")
    rep.page(f"Несуществующая страница  ->  {status}")
    if status != 404:
        rep.bad(f"должен быть 404, а пришёл {status} (поисковик начнёт индексировать мусор)")
    elif "html" not in hdr.get("content-type", ""):
        rep.warn("404 отдаётся не страницей, а " + hdr.get("content-type", ""))
    else:
        rep.info("честный 404 со страницей")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", help="адрес сайта, например https://dianthus-opt.ru")
    ap.add_argument("--products", type=int, default=12, help="сколько карточек товаров из sitemap проверить (по умолчанию 12)")
    ap.add_argument("--delay", type=float, default=0.15, help="пауза между запросами, сек.")
    args = ap.parse_args()

    fetch = Fetcher(args.base, args.delay)
    rep = Report()
    seen: dict = {}
    home = None
    product_page = None
    for path in PUBLIC_PAGES:
        p = check_page(path, fetch, rep, seen)
        if path == "/":
            home = p
    for slug in SECTION_SLUGS:
        check_page(f"/optom/{slug}", fetch, rep, seen)
    for path in NOINDEX_PAGES:
        check_page(path, fetch, rep, seen, expect_noindex=True)
    for path in check_sitemap(fetch, rep, args.products, seen):
        p = check_page(path, fetch, rep, seen)
        product_page = product_page or p
    check_robots(fetch, rep)
    check_404(fetch, rep)
    check_headers(fetch, rep)
    if home:
        check_assets(home, fetch, rep, "«/»")
    og = home.meta.get("og:image", "") if home else ""
    if og:
        status, hdr, body = fetch.get(og, accept="image/*")
        rep.page(f"Картинка для превью в соцсетях  ->  {status}")
        if status != 200 or "image" not in hdr.get("content-type", ""):
            rep.bad("og:image не открывается как картинка: " + og)
        else:
            rep.info(f"{og}: {len(body) // 1024} КБ, {hdr.get('content-type')}")
    rep.lines.append("")
    rep.lines.append(f"ИТОГО: серьёзных замечаний [!] — {rep.serious}, мелких [~] — {rep.minor}")
    print("\n".join(rep.lines))
    return 1 if rep.serious else 0


if __name__ == "__main__":
    raise SystemExit(main())
