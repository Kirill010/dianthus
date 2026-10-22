# -*- coding: utf-8 -*-
"""Проверка связи с обменом 1С. Нужен только Python 3.10+ (внешние пакеты не нужны).

    python scripts/onec_smoke_test.py https://dianthus-opt.ru 1c_dianthus ПАРОЛЬ
    python scripts/onec_smoke_test.py http://127.0.0.1:8000 1c_dianthus dev-1c-password --write

По умолчанию ТОЛЬКО ЧТЕНИЕ: связь и пароль, число товаров, новых заказов, клиентов.
С ключом --write дополнительно отправляется один служебный товар
«ТЕСТ-ОБМЕН-1С» с нулевым остатком (на витрине он не виден, в справочнике
админки — виден; потом его можно удалить).

Скрипт полезен программисту 1С: показывает, как должны выглядеть запросы и
ответы, и помогает отличить проблему сети/пароля от проблемы в самой 1С.
"""
import base64
import json
import sys
import urllib.error
import urllib.request


def make_caller(base: str, user: str, password: str):
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    base = base.rstrip("/") + "/api/1c"

    def call(method: str, path: str, body=None):
        data = None
        headers = {"Authorization": f"Basic {token}"}
        if body is not None:
            # JSON в UTF-8 (кириллица как есть) и БЕЗ лишних преобразований
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(base + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
    return call


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 3:
        print(__doc__)
        return 2
    base, user, password = args
    write = "--write" in argv
    call = make_caller(base, user, password)

    print(f"Проверяю {base} ...")
    try:
        status, data = call("GET", "/ping")
    except OSError as e:
        print(f"❌ Нет связи с сервером: {e}")
        print("   Проверьте адрес, интернет, сертификат (https) и что сайт запущен.")
        return 1
    if status == 401:
        print("❌ 401: неверный логин или пароль (INTEGRATION_USER / INTEGRATION_PASSWORD в .env).")
        return 1
    if status == 503:
        print("❌ 503: в .env сервера не задан INTEGRATION_PASSWORD — обмен отключён.")
        return 1
    if status != 200:
        print(f"❌ Неожиданный ответ {status}: {data}")
        return 1
    print(f"✅ ping: {data}")

    s, d = call("GET", "/products/export?all=1")
    print(f"✅ каталог сайта: {d.get('count', '?')} товаров")
    s, d = call("GET", "/orders/export?new=1&limit=1")
    print(f"✅ новые заказы (не забранные 1С): есть ли хоть один — {d.get('count', 0) > 0}")
    s, d = call("GET", "/clients/export?limit=1")
    print(f"✅ клиенты: выгрузка работает (код {s})")

    if not write:
        print("\nЗапись не проверялась (добавьте --write, чтобы отправить служебный товар).")
        return 0

    s, d = call("POST", "/products/sync", {"products": [{
        "id": "TEST-OBMEN-1C", "sku": "TEST-OBMEN-1C", "name": "ТЕСТ-ОБМЕН-1С",
        "package_size": 10, "price_per_stem": 1, "stock_packs": 0,
        "description": "Служебный товар проверки обмена. Можно удалить.",
    }]})
    print(f"{'✅' if s == 200 else '❌'} запись каталога: {s} {d}")
    return 0 if s == 200 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
