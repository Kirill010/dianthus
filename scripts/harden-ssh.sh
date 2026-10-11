#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
# Отключает вход по паролю и вход root по SSH. Только вход по ключу.
# Запускать ТОЛЬКО после того, как вы убедились, что можете зайти
#   ssh deploy@<IP>   по SSH-ключу (без пароля) — в другом окне!
#   sudo bash scripts/harden-ssh.sh
# ───────────────────────────────────────────────────────────────
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Запустите через sudo: sudo bash $0" >&2
    exit 1
fi

DEPLOY_USER="${DEPLOY_USER:-deploy}"
KEYS="/home/${DEPLOY_USER}/.ssh/authorized_keys"

if [[ ! -s "$KEYS" ]]; then
    echo "❌ У пользователя ${DEPLOY_USER} нет SSH-ключей (${KEYS})." >&2
    echo "   Отключение пароля сейчас заперло бы вас на сервере. Остановлено." >&2
    exit 1
fi

echo "Вы проверили вход по ключу под ${DEPLOY_USER} в ДРУГОМ окне терминала?"
read -r -p "Введите yes чтобы продолжить: " ans
[[ "$ans" == "yes" ]] || { echo "Отменено."; exit 1; }

# Файл с префиксом 00- читается первым и имеет приоритет
# над настройками провайдера (например, 50-cloud-init.conf).
cat > /etc/ssh/sshd_config.d/00-hardening.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
MaxAuthTries 4
X11Forwarding no
CONF

sshd -t
systemctl restart ssh

echo "✅ Вход по паролю отключён. Проверьте новое подключение ПРЕЖДЕ чем закрывать это окно."
