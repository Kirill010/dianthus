#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
# Обновление сайта до последней версии из GitHub (с бэкапом перед этим).
#   bash scripts/update.sh      (или ./scripts/update.sh, если выдали chmod +x)
#
# Что делает:
#   1. резервная копия базы и фото (scripts/backup.sh) — откат возможен всегда;
#   2. git pull (только «быстрая перемотка», без неожиданных слияний);
#   3. пересборка образа и перезапуск контейнеров;
#   4. пересборка конфига nginx из шаблона, если шаблон в git изменился
#      (с проверкой `nginx -t` и автоматическим откатом при ошибке); если изменились файлы
#      nginx.conf или proxy_params.conf — проверка нового конфига в отдельном контейнере и
#      перезапуск nginx (иначе контейнер продолжил бы работать по старым настройкам);
#   5. ждёт, пока приложение станет «healthy», и показывает состояние.
# ───────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> 1/5 Бэкап перед обновлением"
bash ./scripts/backup.sh      # через bash: у файла в git может не быть бита «исполняемый»

echo "==> 2/5 Получаю изменения из GitHub"
git pull --ff-only

echo "==> 3/5 Пересборка и запуск"
docker compose up -d --build

echo "==> 4/5 Конфиг nginx"
DOMAIN="$(grep -E '^APP_URL=' .env | head -1 | sed -E 's|^APP_URL=https?://||; s|/.*$||')"
CONF=nginx/conf.d/site.conf
if [[ -n "$DOMAIN" && -f "$CONF" ]]; then
    # какой этап настроен сейчас: HTTPS (есть сертификат) или пока только HTTP
    if grep -q 'ssl_certificate' "$CONF"; then SRC=nginx/site_https.conf; else SRC=nginx/site_http.conf; fi
    sed "s/__DOMAIN__/${DOMAIN}/g" "$SRC" > "${CONF}.new"
    if cmp -s "${CONF}.new" "$CONF"; then
        rm -f "${CONF}.new"
        echo "    конфиг не менялся"
    else
        cp "$CONF" "${CONF}.bak"
        mv "${CONF}.new" "$CONF"
        if docker compose exec -T nginx nginx -t; then
            rm -f "${CONF}.bak"
            echo "    конфиг обновлён"
        else
            echo "❌ Новый конфиг nginx не прошёл проверку — возвращаю прежний." >&2
            mv "${CONF}.bak" "$CONF"
        fi
    fi
fi
# nginx.conf и proxy_params.conf смонтированы в контейнер ОТДЕЛЬНЫМИ ФАЙЛАМИ. Когда git заменяет такой файл,
# контейнер продолжает видеть старый (Docker держит файл по номеру, а не по имени), и «nginx -s reload»
# перечитывает старые настройки: новое сжатие, лимиты и т. п. не применяются. Поэтому сравниваем то, что видит
# контейнер, с файлами в репозитории; отличаются — проверяем новый конфиг в отдельном контейнере и перезапускаем
# nginx (перерыв около секунды). Не прошёл проверку — nginx остаётся на прежних настройках, скрипт завершится с ошибкой.
NGINX_PROBLEM=0
STALE=0
for f in nginx.conf proxy_params.conf; do
    if ! docker compose exec -T nginx cat "/etc/nginx/$f" 2>/dev/null | cmp -s - "nginx/$f"; then
        STALE=1
    fi
done
if [[ "$STALE" == 1 ]]; then
    echo "    nginx.conf или proxy_params.conf изменились: проверяю новый конфиг и перезапускаю nginx"
    if docker compose run --rm --no-deps --entrypoint nginx nginx -t; then
        if docker compose restart nginx; then
            echo "    nginx перезапущен с новыми настройками"
        else
            echo "❌ Не удалось перезапустить nginx: docker compose logs nginx --tail=40" >&2
            NGINX_PROBLEM=1
        fi
    else
        echo "❌ Новый nginx.conf не прошёл проверку — nginx оставлен на прежних настройках. Исправьте файл и запустите обновление ещё раз." >&2
        NGINX_PROBLEM=1
    fi
else
    docker compose exec -T nginx nginx -s reload || true
fi

echo "==> 5/5 Жду, пока приложение запустится"
for _ in $(seq 1 30); do
    status="$(docker compose ps --format '{{.Service}} {{.Health}}' app 2>/dev/null | awk '{print $2}')"
    [[ "$status" == "healthy" ]] && break
    sleep 4
done

docker image prune -f >/dev/null
echo
docker compose ps
if [[ "${status:-}" != "healthy" ]]; then
    echo "⚠️  Приложение не стало healthy за 2 минуты. Смотрите: docker compose logs app --tail=80" >&2
    exit 1
fi
if [[ "$NGINX_PROBLEM" == 1 ]]; then
    echo "⚠️  Код обновлён, но nginx работает по прежним настройкам (см. сообщение выше)." >&2
    exit 1
fi
echo "✅ Обновлено до $(git rev-parse --short HEAD)"
