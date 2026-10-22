# ───────────────────────────────────────────────────────────────
# Диантус — образ приложения (FastAPI + Uvicorn)
# (Строки «# syntax=docker/dockerfile:1» здесь намеренно нет: она заставляла
# Docker при каждой сборке скачивать лишний образ с Docker Hub, а он бывает
# недоступен с российских серверов. Все нужные возможности уже есть в Docker.)
# Двухэтапная сборка: компилятор остаётся в builder, в итоговый
# образ попадают только готовые пакеты.
# ───────────────────────────────────────────────────────────────

# ── Этап 1: сборка зависимостей ───────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Все зависимости из requirements.txt ставятся готовыми бинарными пакетами
# (wheels), компилятор обычно не нужен. build-essential и dev-библиотеки
# оставлены «про запас» — на случай, если для вашей платформы (например,
# ARM-сервер) какого-то wheel нет и пакет придётся собирать из исходников.
# Этот этап в итоговый образ не попадает. (Раньше здесь были libcairo2-dev и
# pycairo — новые xhtml2pdf/svglib в них больше не нуждаются.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config libffi-dev \
        libjpeg-dev zlib1g-dev libfreetype6-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Адрес индекса пакетов можно заменить, если pypi.org недоступен с вашего
# сервера: в .env добавьте PIP_INDEX_URL=https://зеркало/simple (см. docs).
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

COPY requirements.txt .
RUN pip install -r requirements.txt


# ── Этап 2: итоговый образ ────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# fonts-dejavu-core — шрифт с кириллицей для PDF-счетов (иначе вместо букв квадраты)
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Обычный пользователь без прав root
RUN groupadd --system --gid 10001 appuser \
    && useradd  --system --uid 10001 --gid 10001 \
                --create-home --home-dir /home/appuser appuser

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appuser . .

# PDF-шрифты: приложение ищет их в app/static/fonts (в репозитории их нет)
RUN mkdir -p app/static/fonts \
    && cp /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
          /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf \
          app/static/fonts/ \
    && mkdir -p app/static/uploads /tmp/dianthus \
    && chown -R appuser:appuser app/static/fonts app/static/uploads /tmp/dianthus

# Ранняя проверка: если vendor-файлов нет, сборка упадёт сразу, а не сайт без стилей
RUN test -f app/static/vendor/bootstrap.min.css \
    && test -f app/static/vendor/bootstrap.bundle.min.js \
    && test -f app/static/vendor/fontawesome/css/all.min.css

USER appuser

EXPOSE 8000

# Число воркеров задаётся переменной WEB_CONCURRENCY (см. docker-compose.yml)
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
