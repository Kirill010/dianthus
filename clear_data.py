# Интерактивное меню очистки данных Диантуса.

# Каждый пункт работает ОТДЕЛЬНО и БЕЗОПАСНО:
#   - заказы — с возвратом остатков
#   - клиенты — с возвратом остатков их заказов
#   - справочник — удаляет товары и партии
#   - поставки — удаляет рейсы
#   - полки — снимает товары с каталога (без удаления)
#   - всё — полная очистка (с возвратом остатков)

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clear_orders_return_stock import clear_orders_return_stock
from clear_clients_return_stock import clear_clients_return_stock
from clear_products import clear_products
from clear_supplies import clear_supplies
from clear_stock import clear_stock
from clear_all_safe import clear_all_safe


MENU = """
╔══════════════════════════════════════════════════════════════╗
║   🌸 Диантус — управление данными (безопасно)                ║
╠══════════════════════════════════════════════════════════════╣
║   1. 🔄 Очистить ЗАКАЗЫ (с возвратом остатков)               ║
║         Клиенты, справочник, поставки — на месте              ║
║                                                              ║
║   2. 🔄 Очистить КЛИЕНТОВ (с возвратом остатков их заказов)  ║
║         Админы, справочник, поставки — на месте               ║
║                                                              ║
║   3. 🗑  Очистить СПРАВОЧНИК ТОВАРОВ                          ║
║         Поставки, заказы, клиенты — на месте                  ║
║                                                              ║
║   4. 🗑  Очистить ПОСТАВКИ                                    ║
║         Справочник, заказы, клиенты — на месте                ║
║                                                              ║
║   5. 🧹 Снять всё с ПОЛОК (обнулить «На складе»)              ║
║         Товары, поставки, заказы, клиенты — на месте          ║
║                                                              ║
║   6. 💥 ПОЛНАЯ ОЧИСТКА (с возвратом остатков)                 ║
║         Останутся только админы                              ║
║                                                              ║
║   0. Выход                                                    ║
╚══════════════════════════════════════════════════════════════╝
"""


def main() -> None:
    actions = {
        "1": ("Заказы (с возвратом остатков)", clear_orders_return_stock),
        "2": ("Клиенты (с возвратом остатков)", clear_clients_return_stock),
        "3": ("Справочник товаров", clear_products),
        "4": ("Поставки", clear_supplies),
        "5": ("Снять с полок", clear_stock),
        "6": ("Полная очистка (с возвратом)", clear_all_safe),
    }

    while True:
        print(MENU)
        choice = input("Выберите пункт [0-6]: ").strip()

        if choice == "0":
            print("Выход.")
            return

        if choice not in actions:
            print("❌ Неверный выбор.\n")
            continue

        name, func = actions[choice]
        print(f"\n▶  {name}\n" + "─" * 60)
        try:
            func()
        except Exception as e:
            print(f"❌ Ошибка: {e}")

        input("\nНажмите Enter для возврата в меню…")


if __name__ == "__main__":
    main()