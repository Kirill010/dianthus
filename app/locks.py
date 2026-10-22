# app/locks.py
"""Кроссплатформенная файловая блокировка.

ЗАЧЕМ. Раньше и миграция схемы (`app/migrations.py`), и планировщик
(`app/scheduler.py`) использовали `fcntl.flock` — модуль есть только в POSIX.
На Windows приложение вообще не импортировалось (`ModuleNotFoundError: fcntl`),
поэтому проект нельзя было запустить и протестировать на компьютере
разработчика. Теперь:

  * Linux/macOS — `fcntl.flock` (как было);
  * Windows     — `msvcrt.locking`;
  * нет ни того, ни другого — работаем без блокировки (предупреждение в лог),
    но не падаем: блокировка — оптимизация, а не условие работы.

ВАЖНО: на POSIX блокировка снимается ядром при закрытии файла, даже если
процесс упал, — «залипших» lock-файлов не остаётся.
"""
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

try:                     # POSIX
    import fcntl
except ImportError:      # Windows
    fcntl = None

try:                     # Windows
    import msvcrt
except ImportError:      # POSIX
    msvcrt = None

# Коды «занято»: EAGAIN/EWOULDBLOCK (11), EACCES (13), EDEADLK (35/36).
_BUSY_ERRNOS = (11, 13, 35, 36)


def lock_path(env_var: str, filename: str) -> str:
    """Где лежит lock-файл.

    1) явно заданный путь в переменной окружения (в docker-compose — /tmp/dianthus);
    2) /run/dianthus — стандартное место под systemd (RuntimeDirectory), если
       каталог доступен для записи;
    3) иначе временная папка системы. Раньше на Windows путь «/run/dianthus»
       превращался в D:/run/dianthus: приложение мусорило в корне диска.
    """
    explicit = (os.getenv(env_var) or "").strip()
    if explicit:
        return explicit
    if os.name != "nt":
        run_dir = "/run/dianthus"
        try:
            os.makedirs(run_dir, exist_ok=True)
            if os.access(run_dir, os.W_OK):
                return f"{run_dir}/{filename}"
        except OSError:
            pass
    return str(Path(tempfile.gettempdir()) / "dianthus" / filename)


def _ensure_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if not directory:
        return
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as e:
        logger.warning("Не создал каталог %s: %s", directory, e)


def _lock_fd(fd: int, blocking: bool) -> None:
    """Блокирует дескриптор. Бросает OSError (для неблокирующего режима — «занято»)."""
    if fcntl is not None:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(fd, flags)
        return

    if msvcrt is not None:
        # msvcrt блокирует диапазон от текущей позиции. Файл может быть пустым —
        # дописываем нулевой байт, иначе Windows не даёт залочить регион.
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        return

    logger.debug("Механизм блокировки недоступен — продолжаю без блокировки")


def _unlock_fd(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def acquire_file_lock(path: str, blocking: bool = True) -> int | None:
    """Открывает lock-файл и блокирует его для процесса.

    Возвращает:
        fd >= 0 — блокировка получена (снять: release_file_lock);
        -1      — снять блокировку нельзя (нет механизма/нет прав):
                  вызывающий код продолжает работу без блокировки;
        None    — файл занят другим процессом (только при blocking=False).
    """
    _ensure_dir(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        logger.warning("Не открыл lock-файл %s (%s) — работаю без блокировки", path, e)
        return -1

    try:
        _lock_fd(fd, blocking)
    except OSError as e:
        os.close(fd)
        busy = isinstance(e, BlockingIOError) or (
            not blocking and getattr(e, "errno", None) in _BUSY_ERRNOS
        )
        if busy:
            return None
        logger.warning(
            "Блокировка %s недоступна (%s) — работаю без блокировки", path, e
        )
        return -1

    try:
        if fd >= 0:
            os.ftruncate(fd, 0)
            os.write(fd, f"pid={os.getpid()}\n".encode())
    except OSError:
        pass
    return fd


def release_file_lock(fd: int | None) -> None:
    """Снимает блокировку и закрывает дескриптор. Безопасно для None и -1."""
    if fd is None:
        return
    if fd >= 0:
        _unlock_fd(fd)
    try:
        os.close(fd)
    except OSError:
        pass


@contextmanager
def file_lock(path: str, blocking: bool = True):
    """Контекстный менеджер. yield True, если блокировка получена (или не нужна).

    При blocking=False и занятом файле yield False — вызывающий код сам решает,
    пропустить работу или продолжить.
    """
    fd = acquire_file_lock(path, blocking=blocking)
    try:
        yield fd is not None
    finally:
        release_file_lock(fd)
