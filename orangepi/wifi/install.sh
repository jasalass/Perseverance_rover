#!/bin/sh
# Instala el manejo de WiFi del rover. Correr en la Orange Pi, desde esta
# carpeta:  sudo sh install.sh
# Las claves de las redes NO estan en este repo: se agregan despues con
#   sudo percy-wifi save "<ssid>" "<clave>"
set -e
cd "$(dirname "$0")"

install -o root -g root -m 755 percy-wifi /usr/local/bin/percy-wifi

# El backend (usuario orangepi) solo puede correr ESTE script como root.
printf 'orangepi ALL=(root) NOPASSWD: /usr/local/bin/percy-wifi\n' > /tmp/percy-wifi.sudoers
visudo -cf /tmp/percy-wifi.sudoers
install -o root -g root -m 440 /tmp/percy-wifi.sudoers /etc/sudoers.d/percy-wifi
rm -f /tmp/percy-wifi.sudoers

# Canal ntfy privado para el aviso de IP (nombre al azar, solo en la Pi).
if [ ! -s /etc/percy-wifi-topic ]; then
  printf 'percy-%s\n' "$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')" > /etc/percy-wifi-topic
  chmod 644 /etc/percy-wifi-topic
fi

# Nombre fijo en cualquier red: http://percy.local:8000 (mDNS via avahi).
if [ "$(hostname)" != "percy" ]; then
  old="$(hostname)"
  hostnamectl set-hostname percy
  sed -i "s/\b$old\b/percy/g" /etc/hosts
  grep -q '\bpercy\b' /etc/hosts || printf '127.0.1.1\tpercy\n' >> /etc/hosts
fi
if ! command -v avahi-daemon >/dev/null 2>&1; then
  apt-get install -y avahi-daemon || echo "AVISO: no se pudo instalar avahi-daemon (sin internet?)"
fi
systemctl enable --now avahi-daemon 2>/dev/null || true

install -o root -g root -m 644 percy-wifi-watchdog.service /etc/systemd/system/percy-wifi-watchdog.service
systemctl daemon-reload
systemctl enable percy-wifi-watchdog.service
systemctl restart percy-wifi-watchdog.service

echo "ok - percy-wifi instalado, watchdog activo"
echo "canal ntfy del aviso de IP: $(cat /etc/percy-wifi-topic)"
echo "nombre en la red: http://$(hostname).local:8000"
