# syntax=docker/dockerfile:1
# ───────────────────────────────────────────────────────────────
# Диантус — образ приложения (FastAPI + Uvicorn)
# Двухэтапная сборка: компилятор остаётся в builder, в итоговый
# образ попадают только готовые пакеты.
# ───────────────────────────────────────────────────────────────

# ── Этап 1: сборка зависимостей ───────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential/pkg-config/libcairo2-dev нужны, чтобы собрать pycairo
# (его тянет xhtml2pdf → svglib). libffi/libjpeg/zlib/freetype — запас для Pillow/bcrypt.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config libcairo2-dev libffi-dev \
        libjpeg-dev zlib1g-dev libfreetype6-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt


# ── Этап 2: итоговый образ ────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# libcairo2 — рантайм для pycairo; fonts-dejavu-core — кириллица в PDF-счетах.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libcairo2 fonts-dejavu-core \
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
