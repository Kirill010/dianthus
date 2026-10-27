"""Грубая проверка структуры текста на встроенном языке 1С (BSL): парность блоков,
скобок, кавычек, точек с запятой. Это НЕ компилятор - ловит только опечатки."""
import re
import sys

path = sys.argv[1]
text = open(path, encoding="utf-8").read()

problems = []

# 1) убрать комментарии и содержимое строк, сохранив номера строк
clean_lines = []
for no, raw in enumerate(text.splitlines(), start=1):
    out = []
    i = 0
    in_str = False
    while i < len(raw):
        ch = raw[i]
        if in_str:
            if ch == '"':
                if i + 1 < len(raw) and raw[i + 1] == '"':
                    i += 2
                    continue
                in_str = False
                out.append('"')
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append('"')
            i += 1
            continue
        if raw.startswith("//", i):
            break
        out.append(ch)
        i += 1
    if in_str:
        problems.append(f"строка {no}: не закрыта кавычка")
    joined = "".join(out)
    if joined.lstrip().startswith("#"):      # директивы препроцессора проверяем отдельно
        joined = ""
    clean_lines.append(joined)

pairs = [
    ("Процедура", "КонецПроцедуры"),
    ("Функция", "КонецФункции"),
    ("Если", "КонецЕсли"),
    ("Попытка", "КонецПопытки"),
]
tok = re.compile(r"\b(Процедура|КонецПроцедуры|Функция|КонецФункции|Если|ИначеЕсли|КонецЕсли|"
                 r"Для|Пока|КонецЦикла|Попытка|Исключение|КонецПопытки)\b")

stack = []
for no, line in enumerate(clean_lines, start=1):
    for m in tok.finditer(line):
        w = m.group(1)
        # «Для Каждого», «Пока» открывают цикл; «Если» внутри ?( ) - не блок
        if w == "Если":
            if line[:m.start()].rstrip().endswith("?(") or "?(" in line[:m.start()] and line.count("(") > line.count(")"):
                continue
            stack.append(("Если", no))
        elif w == "ИначеЕсли":
            if not stack or stack[-1][0] != "Если":
                problems.append(f"строка {no}: ИначеЕсли вне Если")
        elif w == "КонецЕсли":
            if not stack or stack[-1][0] != "Если":
                problems.append(f"строка {no}: КонецЕсли без Если (стек: {stack[-1] if stack else None})")
            else:
                stack.pop()
        elif w in ("Для", "Пока"):
            stack.append(("Цикл", no))
        elif w == "КонецЦикла":
            if not stack or stack[-1][0] != "Цикл":
                problems.append(f"строка {no}: КонецЦикла без цикла (стек: {stack[-1] if stack else None})")
            else:
                stack.pop()
        elif w == "Попытка":
            stack.append(("Попытка", no))
        elif w == "Исключение":
            if not stack or stack[-1][0] != "Попытка":
                problems.append(f"строка {no}: Исключение вне Попытки")
            else:
                stack[-1] = ("Исключение", stack[-1][1])
        elif w == "КонецПопытки":
            if not stack or stack[-1][0] != "Исключение":
                problems.append(f"строка {no}: КонецПопытки без Исключения (стек: {stack[-1] if stack else None})")
            else:
                stack.pop()
        elif w in ("Процедура", "Функция"):
            if stack:
                problems.append(f"строка {no}: {w} внутри незакрытого блока {stack[-1]}")
            stack.append((w, no))
        elif w in ("КонецПроцедуры", "КонецФункции"):
            want = "Процедура" if w == "КонецПроцедуры" else "Функция"
            if not stack or stack[-1][0] != want:
                problems.append(f"строка {no}: {w} без {want} (стек: {stack[-1] if stack else None})")
            else:
                stack.pop()
if stack:
    problems.append(f"не закрыты блоки: {stack}")

# 2) баланс скобок по всему тексту
depth = 0
for no, line in enumerate(clean_lines, start=1):
    depth += line.count("(") - line.count(")")
    if depth < 0:
        problems.append(f"строка {no}: лишняя закрывающая скобка")
        depth = 0
if depth:
    problems.append(f"не закрыто скобок: {depth}")

# 3) каждый оператор должен заканчиваться «;» (кроме заголовков/служебных строк)
no_semicolon_ok = re.compile(
    r"^\s*(Процедура|Функция|КонецПроцедуры;?|КонецФункции;?|Если|ИначеЕсли|Иначе|КонецЕсли;|Для|Пока|КонецЦикла;|"
    r"Попытка|Исключение|КонецПопытки;)\b|^\s*$")
buf = ""
buf_start = 0
for no, line in enumerate(clean_lines, start=1):
    stripped = line.strip()
    if not stripped:
        continue
    if not buf and no_semicolon_ok.match(line):
        # заголовки блоков заканчиваются Тогда/Цикл/скобкой
        if re.match(r"^\s*(Если|ИначеЕсли)\b", line) and not stripped.endswith("Тогда"):
            buf, buf_start = stripped, no
        elif re.match(r"^\s*(Для|Пока)\b", line) and not stripped.endswith("Цикл"):
            buf, buf_start = stripped, no
        elif re.match(r"^\s*(Процедура|Функция)\b", line) and not stripped.endswith(")") and not stripped.endswith("Экспорт"):
            buf, buf_start = stripped, no
        continue
    buf = (buf + " " + stripped).strip() if buf else stripped
    if not buf_start:
        buf_start = no
    # продолжение: оператор считается законченным, если строка кончается на «;» или на Тогда/Цикл
    if stripped.endswith(";") or stripped.endswith("Тогда") or stripped.endswith("Цикл") or stripped.endswith(")") and re.match(r"^\s*(Процедура|Функция)", buf):
        buf, buf_start = "", 0
    else:
        # оператор переносится на следующую строку - допустимо, если строка заканчивается оператором/запятой/скобкой
        # или если следующая непустая строка начинается с «+», «Или», «И»
        nxt = ""
        for later in clean_lines[no:]:
            if later.strip():
                nxt = later.strip()
                break
        if nxt.startswith(("+", "Или ", "И ")):
            continue
        if not re.search(r"(\+|,|\(|Или|И|=|\?\(.*,)$", stripped):
            problems.append(f"строка {no}: возможно, нет «;» в конце: {stripped[:70]}")
            buf, buf_start = "", 0

for p in problems:
    print("ПРОБЛЕМА:", p)
print("проверено строк:", len(clean_lines), "| проблем:", len(problems))
sys.exit(1 if problems else 0)
