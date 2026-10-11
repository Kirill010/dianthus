#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
# Получает бесплатный сертификат Let's Encrypt и включает HTTPS.
#   ./scripts/init-ssl.sh admin@dianthus-opt.ru
# Условия: DNS-записи A для домена и www уже указывают на ЭТОТ сервер,
# а стек запущен (docker compose up -d).
# ───────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."

EMAIL="${1:-}"
if [[ -z "$EMAIL" ]]; then
    echo "Использование: $0 <email для уведомлений Let's Encrypt>"
    exit 1
fi
[[ -f .env ]] || { echo "❌ Нет .env — сначала scripts/prepare.sh"; exit 1; }

DOMAIN="$(grep -E '^APP_URL=' .env | head -1 | sed -E 's|^APP_URL=https?://||; s|/.*$||')"
[[ -n "$DOMAIN" ]] || { echo "❌ Не удалось определить домен из APP_URL"; exit 1; }
echo "Домен: ${DOMAIN}"

# Проверка DNS: домен и www должны вести на этот сервер
MY_IP="$(curl -4fsS --max-time 8 https://ifconfig.me 2>/dev/null || true)"
for host in "$DOMAIN" "www.${DOMAIN}"; do
    ip="$(getent ahostsv4 "$host" | awk '{print $1; exit}' || true)"
    echo "  ${host} → ${ip:-НЕ НАЙДЕН}"
    if [[ -z "$ip" ]]; then
        echo "❌ Для ${host} нет A-записи. Добавьте её в DNS и подождите." >&2
        exit 1
    fi
    if [[ -n "$MY_IP" && "$ip" != "$MY_IP" ]]; then
        echo "❌ ${host} указывает на ${ip}, а этот сервер — ${MY_IP}." >&2
        echo "   DNS ещё не обновился (или запись неверная). Подождите и повторите." >&2
        exit 1
    fi
done

# Nginx с HTTP-конфигом должен работать (он отдаёт проверочный файл)
docker compose up -d nginx

echo "==> Запрашиваю сертификат"
docker compose run --rm --entrypoint certbot certbot certonly \
    --webroot -w /var/www/certbot \
    -d "$DOMAIN" -d "www.${DOMAIN}" \
    --email "$EMAIL" --agree-tos --no-eff-email --non-interactive

echo "==> Включаю HTTPS-конфиг"
cp nginx/conf.d/site.conf nginx/conf.d/site.conf.bak
sed "s/__DOMAIN__/${DOMAIN}/g" nginx/site-https.conf > nginx/conf.d/site.conf

if docker compose exec -T nginx nginx -t; then
    docker compose exec -T nginx nginx -s reload
    rm -f nginx/conf.d/site.conf.bak
    echo "✅ HTTPS включён: https://${DOMAIN}"
    echo "   Продление сертификата идёт автоматически (контейнер certbot)."
else
    echo "❌ Ошибка в конфиге nginx — возвращаю HTTP-вариант." >&2
    mv nginx/conf.d/site.conf.bak nginx/conf.d/site.conf
    docker compose exec -T nginx nginx -s reload || true
    exit 1
fi
