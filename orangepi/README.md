# Percy - backend Orange Pi

Servidor de control del rover (FastAPI + WebSocket + PCA9685 + TB6612FNG).
Ver `CLAUDE.md` en la raiz del repo para el contexto completo del proyecto.

## Deploy en la Orange Pi

```bash
# copiar la carpeta completa a la Orange Pi (desde el PC, con pscp/scp)
pscp -r orangepi orangepi@192.168.1.41:/home/orangepi/percy

# en la Orange Pi
cd ~/percy
pip3 install --user -r requirements.txt   # ver nota en requirements.txt:
                                           # NO instala opencv/gpiod (ya son del sistema)
```

Dashboard en `http://<ip-orange-pi>:8000/` desde el celular (misma red WiFi).

### Arranque automático (systemd) — instalado 14-sept

Dado lo inestable que fue la energía durante el armado (varios reinicios
de la Orange Pi), el servidor corre como servicio systemd
(`/etc/systemd/system/percy.service`, copia en `orangepi/percy.service`)
en vez de lanzarse a mano — **arranca solo en cada reboot**.

```bash
sudo systemctl status percy.service   # ver estado
sudo systemctl restart percy.service  # reiniciar tras un cambio de codigo
sudo journalctl -u percy.service -f   # ver logs en vivo
```

Si cambias algo en el código y lo vuelves a copiar con `pscp`, hay que
`sudo systemctl restart percy.service` para que tome el cambio — ya no
alcanza con `python3 main.py` a mano (quedaría un segundo proceso
compitiendo por el puerto 8000 con el que ya administra systemd).

## HTTPS (necesario para el control por voz, rama `control_voz`)

El microfono del celular (`getUserMedia`) solo funciona en un "contexto
seguro" - `https://` o `localhost`. El dashboard servido por HTTP plano
en la IP de la red local (como esta hoy) no alcanza, asi que main.py
necesita un certificado. Se usa uno autofirmado (no hay forma de
conseguir uno real sin dominio publico) - `config.SSL_KEYFILE`/
`SSL_CERTFILE` apuntan a `percy.key`/`percy.crt` en esta misma carpeta;
si no existen, main.py arranca en HTTP plano igual (sirve para
desarrollo local sin voz).

Generar el certificado (regenerar si cambia la IP de la Orange Pi, ej.
al pasar a un punto de acceso propio):

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout percy.key -out percy.crt -days 825 \
  -subj "/CN=percy.local" \
  -addext "subjectAltName=IP:<ip-orange-pi>,DNS:localhost,IP:127.0.0.1"
```

Copiar `percy.key`/`percy.crt` junto con el resto de la carpeta al
hacer `pscp`. La primera vez que el celular entra a
`https://<ip-orange-pi>:8000/`, el navegador va a avisar "la conexion
no es privada" (normal con un cert autofirmado) - hay que aceptarlo
manualmente una vez ("Avanzado" -> "Continuar de todas formas").

## Antes de conectar servos/motores de verdad

Todos los pines/canales estan centralizados en `config.py`. **No estan
medidos contra el cableado fisico real** (igual advertencia que tenia el
`perseverance.ino` del ESP32) - confirmar contra la guia de cableado en
CLAUDE.md y corregir ahi si algo no calza.

Los angulos de servo (`ANGLE_*`, `DEPOSIT_*`, `STEER_CENTER`) son
provisorios, hay que calibrarlos con el brazo y las ruedas ya armadas.

## Estructura

- `config.py` - todos los pines/canales/angulos en un solo lugar.
- `servos.py` - PCA9685 (10 servos: brazo x5, steering x4, bandeja).
- `motors.py` - 6 motores N20 via 3x TB6612FNG (GPIO direccion + PWM sysfs).
- `camera.py` - stream MJPEG de la camara USB (no rompe nada si no hay camara).
- `main.py` - FastAPI, WebSocket `/ws`, failsafe de 500ms, sirve `static/`.
- `static/` - dashboard (2 joysticks + botones, ver app.js).

## Pendiente / no incluido en esta primera version

- AP WiFi propio (`hostapd`+`dnsmasq`) - por ahora corre sobre la red WiFi
  existente, configurada en una sesion anterior.
- ToF VL53L0X / IMU MPU-9250 (AutoNav) - los pines XSHUT ya estan en
  `config.py` pero el modulo de sensores no esta escrito todavia.
- Macro "soltar en bandeja" es bloqueante (no permite mover otra cosa
  mientras corre) - si hace falta que sea no bloqueante hay que pasarla a
  una maquina de estados en `main.py`.
