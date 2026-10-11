#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
# Первичная настройка НОВОГО VPS (Ubuntu 24.04). Запускать от root, один раз:
#   bash server-setup.sh
# Что делает:
#   1. обновляет систему и включает автоустановку security-обновлений
#   2. создаёт пользователя deploy (sudo + docker), переносит ему SSH-ключи root
#   3. ставит Docker из официального репозитория
#   4. включает файрвол (22, 80, 443) и fail2ban для SSH
#   5. создаёт swap 4 ГБ
# SSH-пароль здесь НЕ отключается — это отдельный шаг (harden-ssh.sh),
# чтобы вы не заперли себя на сервере.
# ───────────────────────────────────────────────────────────────
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Запустите от root: sudo bash $0" >&2
    exit 1
fi

DEPLOY_USER="${DEPLOY_USER:-deploy}"
SWAP_SIZE="${SWAP_SIZE:-4G}"

export DEBIAN_FRONTEND=noninteractive

echo "==> 1/6 Обновление системы"
apt-get update -y
apt-get upgrade -y
apt-get install -y --no-install-recommends \
    curl git nano htop ufw fail2ban ca-certificates gnupg \
    unattended-upgrades cron dnsutils
dpkg-reconfigure -f noninteractive unattended-upgrades || true

echo "==> 2/6 Пользователь ${DEPLOY_USER}"
if ! id "$DEPLOY_USER" &>/dev/null; then
    adduser --disabled-password --gecos "" "$DEPLOY_USER"
fi
usermod -aG sudo "$DEPLOY_USER"
if [[ -s /root/.ssh/authorized_keys ]]; then
    install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/${DEPLOY_USER}/.ssh"
    install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
        /root/.ssh/authorized_keys "/home/${DEPLOY_USER}/.ssh/authorized_keys"
    echo "    SSH-ключи root скопированы пользователю ${DEPLOY_USER}"
else
    echo "    ВНИМАНИЕ: у root нет SSH-ключей. Добавьте ключ вручную (см. DEPLOY.md, шаг 3)."
fi

echo "==> 3/6 Docker"
if ! command -v docker &>/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
usermod -aG docker "$DEPLOY_USER"

# Ограничиваем размер логов Docker (иначе диск может забиться)
cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" }
}
JSON
systemctl restart docker

echo "==> 4/6 Файрвол"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "==> 5/6 fail2ban (защита SSH от подбора пароля)"
cat > /etc/fail2ban/jail.d/sshd.local <<'CONF'
[sshd]
enabled  = true
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 1h
CONF
systemctl enable --now fail2ban
systemctl restart fail2ban

echo "==> 6/6 Swap ${SWAP_SIZE}"
if ! swapon --show | grep -q '/swapfile'; then
    fallocate -l "$SWAP_SIZE" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -w vm.swappiness=10 >/dev/null
echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf

echo
echo "✅ Готово."
docker --version
docker compose version
free -h | sed -n '1,2p;3p'
ufw status | head -8
echo
echo "Дальше (в НОВОМ окне терминала, не закрывая это):"
echo "  ssh ${DEPLOY_USER}@<IP сервера>"
echo "Если вход под ${DEPLOY_USER} работает — продолжайте по DEPLOY.md."
