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

install -o root -g root -m 644 percy-wifi-watchdog.service /etc/systemd/system/percy-wifi-watchdog.service
systemctl daemon-reload
systemctl enable percy-wifi-watchdog.service
systemctl restart percy-wifi-watchdog.service

echo "ok - percy-wifi instalado, watchdog activo"
