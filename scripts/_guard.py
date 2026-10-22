# -*- coding: utf-8 -*-
"""Защита от случайного запуска разрушительных скриптов на БОЕВОМ сервере.

Скрипты clear_*.py и reset_*.py удаляют заказы, клиентов, товары или всю
базу. Они лежат в образе Docker рядом с остальным кодом, и один неверный
`docker compose exec app python -m scripts.reset_db` стёр бы сайт. Поэтому в
режиме ENV=prod они отказываются работать, пока вы явно не подтвердите:

    docker compose exec -e I_UNDERSTAND_THIS_DELETES_DATA=yes app python -m scripts.clear_data

Перед этим сделайте бэкап: bash scripts/backup.sh
"""
import os
import sys

if (os.getenv("ENV") or "").strip().lower() == "prod" \
        and os.getenv("I_UNDERSTAND_THIS_DELETES_DATA") != "yes":
    print("⛔ ENV=prod: этот скрипт удаляет данные и на боевом сервере заблокирован.")
    print("   Если это действительно нужно — СНАЧАЛА сделайте бэкап (bash scripts/backup.sh),")
    print("   затем запустите с переменной I_UNDERSTAND_THIS_DELETES_DATA=yes (см. scripts/_guard.py).")
    sys.exit(1)
