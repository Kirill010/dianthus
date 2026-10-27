"""Статический анализ текста на встроенном языке 1С (BSL) - ловит то, что компилятор 1С
показал бы как «Переменная не определена», «Неверное число аргументов» и т.п.
Это НЕ компилятор: имена API платформы проверяются по белому списку из этого файла."""
import re
import sys

KEYWORDS = {
    "процедура", "конецпроцедуры", "функция", "конецфункции", "если", "тогда", "иначеесли", "иначе",
    "конецесли", "для", "каждого", "из", "по", "пока", "цикл", "конеццикла", "попытка", "исключение",
    "конецпопытки", "возврат", "прервать", "продолжить", "перем", "знач", "экспорт", "и", "или", "не",
    "новый", "истина", "ложь", "неопределено", "null", "выбратьтип", "вызватьисключение",
}
LITERALS = {"истина", "ложь", "неопределено", "null"}

# Глобальные имена платформы, которые использует этот код
BUILTIN = {n.lower() for n in """
Тип Строка Число Формат Окр Цел Макс Мин Лев Прав Сред Найти СтрДлина СтрЗаменить СокрЛП НРег ВРег
ПустаяСтрока КодСимвола ЗначениеЗаполнено ТекущаяДата НачалоДня КаталогДокументов СоздатьКаталог Вопрос
ВвестиСтроку Сообщить КраткоеПредставлениеОшибки ИнформацияОбОшибке ЗавершитьРаботуСистемы
ИмяКомпьютера ПараметрЗапуска Метаданные Справочники Символы СистемнаяИнформация КодировкаТекста
РежимДиалогаВопрос КодВозвратаДиалога ПравоДоступа XMLСтрока Предупреждение ЗапуститьПриложение
КаталогВременныхФайлов
""".split()}


def strip_comments_and_strings(text: str):
    """Возвращает список строк без комментариев; содержимое строк заменено на «S»."""
    out = []
    in_str = False
    for raw in text.splitlines():
        line = raw
        res = []
        i = 0
        if in_str:
            m = re.match(r"\s*\|", line)
            if m:
                i = m.end()
        while i < len(line):
            ch = line[i]
            if in_str:
                if ch == '"':
                    if i + 1 < len(line) and line[i + 1] == '"':
                        i += 2
                        continue
                    in_str = False
                    res.append('"')
                i += 1
                continue
            if ch == '"':
                in_str = True
                res.append('"~')
                i += 1
                continue
            if line.startswith("//", i):
                break
            res.append(ch)
            i += 1
        s = "".join(res)
        out.append("" if s.lstrip().startswith("#") else s)
    return out


IDENT = re.compile(r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9]*")


def tokenize(line: str):
    return [(m.group(0), m.start()) for m in IDENT.finditer(line)]


def split_args(text: str):
    """Число аргументов в тексте вызова между скобками (с учётом вложенности и пропусков)."""
    depth = 0
    count = 1
    has_content = False
    in_q = False
    for ch in text:
        if ch == '"':
            in_q = not in_q
            has_content = True
            continue
        if in_q:
            continue
        if ch in "([":
            depth += 1
            has_content = True
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
        elif not ch.isspace():
            has_content = True
    return count if (has_content or count > 1) else 0


def parse_header(line: str):
    m = re.match(r"\s*(Процедура|Функция)\s+([A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*)\s*\((.*)\)\s*(Экспорт)?\s*$", line)
    if not m:
        return None
    kind, name, params_text = m.group(1), m.group(2), m.group(3)
    params = []
    if params_text.strip():
        depth = 0
        cur = ""
        parts = []
        for ch in params_text:
            if ch in "(":
                depth += 1
            if ch in ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        for p in parts:
            p = p.strip()
            p = re.sub(r"^Знач\s+", "", p)
            pm = re.match(r"([\wА-Яа-яЁё]+)\s*(=.*)?$", p)
            if not pm:
                return None
            params.append((pm.group(1), bool(pm.group(2))))
    return kind, name, params


def main(path: str) -> int:
    text = open(path, encoding="utf-8").read()
    lines = strip_comments_and_strings(text)
    problems = []

    # ---- 1. разбор процедур и функций ----
    procs = {}          # имя(lower) -> (kind, min_args, max_args, line_no)
    bodies = []         # (kind, name, params, [(line_no, text)])
    module_vars = set()
    current = None
    for no, line in enumerate(lines, start=1):
        h = parse_header(line)
        if h and current is None:
            kind, name, params = h
            low = name.lower()
            if low in procs:
                problems.append(f"строка {no}: повторное определение {name}")
            required = sum(1 for _, opt in params if not opt)
            procs[low] = (kind, required, len(params), no)
            current = (kind, name, params, [])
            continue
        if re.match(r"\s*Конец(Процедуры|Функции)\b", line):
            if current is None:
                problems.append(f"строка {no}: лишний конец процедуры/функции")
            else:
                bodies.append(current)
                current = None
            continue
        if current is None:
            m = re.match(r"\s*Перем\s+(.*?);", line)
            if m:
                for v in m.group(1).split(","):
                    module_vars.add(v.strip().lower())
            elif line.strip() and not line.strip().startswith("Перем"):
                # операторы основной программы
                bodies.append(("Основная программа", "__main__", [], [(no, line)]))
        else:
            current[3].append((no, line))
    if current is not None:
        problems.append("не закрыта последняя процедура/функция")

    proc_names = set(procs)

    # ---- 2. проверка тел ----
    for kind, name, params, body in bodies:
        local = {p.lower() for p, _ in params}
        # переменные, которым что-то присваивается или которые вводит цикл
        for no, line in body:
            for stmt in re.split(r";", line):
                st = stmt.strip()
                m = re.match(r"(?:(?:Если|ИначеЕсли)\b.*?\bТогда\s+)?([A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*)\s*=(?!=)", st)
                if m and m.group(1).lower() not in KEYWORDS:
                    local.add(m.group(1).lower())
                m = re.match(r"Для\s+Каждого\s+([\wА-Яа-яЁё]+)\s+Из\b", st)
                if m:
                    local.add(m.group(1).lower())
                m = re.match(r"Для\s+([\wА-Яа-яЁё]+)\s*=", st)
                if m:
                    local.add(m.group(1).lower())
        # проверка идентификаторов
        depth_loops = 0
        for no, line in body:
            stripped = line.strip()
            if re.match(r"(Для|Пока)\b", stripped):
                depth_loops += 1
            if re.match(r"КонецЦикла\b", stripped):
                depth_loops = max(0, depth_loops - 1)
            if re.match(r"(Прервать|Продолжить)\b", stripped) and depth_loops == 0:
                problems.append(f"строка {no}: Прервать/Продолжить вне цикла")
            # возврат
            if kind == "Процедура" and re.match(r"Возврат\s+\S", stripped):
                problems.append(f"строка {no}: Возврат со значением в процедуре {name}")
            if kind == "Функция" and re.match(r"Возврат\s*;", stripped):
                problems.append(f"строка {no}: Возврат без значения в функции {name}")
            tokens = tokenize(line)
            for ident, pos in tokens:
                low = ident.lower()
                before = line[:pos].rstrip()
                if before.endswith("."):
                    continue                       # свойство или метод объекта
                if before.lower().endswith("новый"):
                    continue                       # имя типа
                if low in KEYWORDS or low in LITERALS or low in BUILTIN:
                    continue
                if low in local or low in module_vars or low in proc_names:
                    continue
                # имя структуры в «Новый Структура("A, B")» живёт в строке - уже вырезано
                problems.append(f"строка {no}: неизвестное имя «{ident}» в {name}")
            # вызовы своих процедур/функций: число аргументов и тип
            for m in re.finditer(r"([A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*)\s*\(", line):
                low = m.group(1).lower()
                if line[:m.start()].rstrip().endswith("."):
                    continue
                if low not in procs:
                    continue
                if line[:m.start()].strip().lower().startswith(("процедура", "функция")):
                    continue
                # текст аргументов до парной скобки
                depth = 0
                end = None
                for i in range(m.end() - 1, len(line)):
                    if line[i] == "(":
                        depth += 1
                    elif line[i] == ")":
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
                if end is None:
                    continue                          # вызов продолжается на следующей строке
                count = split_args(line[m.end():end])
                pkind, lo, hi, defline = procs[low]
                if not (lo <= count <= hi):
                    problems.append(f"строка {no}: {m.group(1)} вызвана с {count} арг., ожидается {lo}..{hi}")
                is_statement_start = line[:m.start()].strip() == ""
                if pkind == "Процедура" and not is_statement_start:
                    problems.append(f"строка {no}: процедура {m.group(1)} использована как функция")

    for p in problems:
        print("ПРОБЛЕМА:", p)
    print(f"процедур/функций: {len(procs)}, проверено строк: {len(lines)}, проблем: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
