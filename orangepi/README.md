# Percy - backend Orange Pi

Servidor de control del rover (FastAPI + WebSocket + PCA9685 + TB6612FNG).
Ver `CLAUDE.md` en la raiz del repo para el contexto completo del proyecto
(concurso, mecanica, BOM). Este README documenta la implementacion real
del software: protocolo, modos de control, cableado y endpoints.

## Deploy en la Orange Pi

```bash
# copiar la carpeta completa a la Orange Pi (desde el PC, con pscp/scp)
pscp -r orangepi orangepi@<ip-orange-pi>:/home/orangepi/percy

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

## Arquitectura y protocolo de control

```
Celular (navegador) --WebSocket /ws--> FastAPI (main.py)
                                          |-- I2C --> PCA9685 --> 10 servos
                                          |                       (brazo x4 + gripper + steering x4)
                                          '-- GPIO --> 3x TB6612FNG --> 6 motores N20
```

El dashboard manda ~20 mensajes/seg por WebSocket con el estado completo
de joysticks/botones (no solo cambios). `main.py:_apply_command` traduce
cada mensaje a servos/motores; el servidor responde con un eco liviano
(`{"hit_limit": bool}`) que el dashboard usa para el feedback háptico.

### Tracción — 3 modos (botón "MODO" del dashboard, `drive_mode`)

| Modo | Cómo gira | Cuándo conviene |
|---|---|---|
| `combinado` (default) | Las 4 ruedas de esquina giran proporcional a `lx` **y** además hay diferencial de velocidad entre lados con el mismo `lx` | Uso general, un poco de las dos ventajas |
| `vehiculo` | Solo dirigen las ruedas de esquina (los 2 lados a la misma velocidad, como un auto) | Terreno suelto — menos desgaste, pero necesita más espacio para girar |
| `tanque` | Diferencial entre lados **+** las 4 ruedas en diagonal, patrón de rombo (cada una apuntando al centro del rover, ver `servos.set_pivot_steering`) | Pivote de radio cero de verdad, sin arrastrar las ruedas de costado — el ángulo (52.5°) se calculó de las medidas reales del chasis (18cm trocha, 23.5cm distancia entre ejes) |

### Brazo — cinemática inversa de 3 juntas acopladas (rediseño 18-sept)

El brazo real es hombro + codo + inclinación de garra, con los eslabones
2 y 3 fusionados en una sola pieza (la ex-muñeca ya no gira sobre sí
misma, ahora es un 3er eslabón en el mismo plano vertical que hombro y
codo). `kinematics.solve3(x_mm, y_mm, phi_deg, elbow_up)` calcula los 3
ángulos de servo necesarios para poner la punta de la garra en esa
posición **con ese ángulo de acercamiento**, no solo esa posición.

Dos modos, elegidos con el switch del dashboard (`arm_mode`):

- **`ik` (default):** el joystick derecho mueve la punta de la garra en
  línea recta (`arm_x_dir`/`arm_y_dir` = velocidad cartesiana, no
  ángulo), y los botones que antes movían la muñeca ahora mueven el
  ángulo de acercamiento (`wrist_dir` → `phi_dir`). Si el punto pedido
  no es alcanzable, o algún ángulo de servo necesario cae fuera de rango,
  el objetivo se congela en el último punto válido ("pared blanda") en
  vez de saltar o clampear — evita el bug de "el brazo salta al volver a
  tocar el joystick".
- **`joint` (LIBRE):** mismos 3 ejes, pero cada uno mueve su articulación
  por separado sin coordinación (vertical → hombro, horizontal → codo,
  botones → inclinación directo). Útil para calibrar o alcanzar
  posiciones que la IK no puede.

**Presets** (`presets.py`): guardan solo el ángulo final de los 5 servos
del brazo bajo un nombre — no el camino ni el tiempo. Al pedir un preset
guardado (`preset_goto`), el brazo rampea suave (60°/s) desde donde esté
hasta esos ángulos. `preset_save`/`preset_delete` completan el CRUD;
`/presets` lista los nombres guardados. Quedan en `presets.json`.

**Macro "depositar"** (`servos.deposit_macro`, botón deposit): mueve el
brazo sobre la bandeja, abre la garra, y vuelve a la posición de espera —
bloqueante y en 3 pasos fijos, sin coordinación con la IK.

### Interlock brazo/tracción (18-sept)

Los 3 MG996R del brazo y los 6 N20 de tracción terminaron compartiendo
el mismo riel de 6V (LM2596 + 3×18650) — no hay margen de corriente
medido para mover los dos a fondo a la vez. Mientras el brazo esté
pedido a moverse, todavía decelerando, o un preset/macro de depósito
esté en curso, `main.py` fuerza la potencia de los N20 a 0 (el steering
sigue funcionando normal, son SG90 de bajo consumo). Expuesto en
`/status` como `arm_busy` para confirmarlo sin adivinar.

### Feedback háptico

El dashboard vibra el celular en: cerrar/abrir garra, disparar el
depósito, comandos de voz reconocidos, y en el flanco ascendente de
`hit_limit` (el brazo empujando contra un límite — pared blanda de la IK
o tope de articulación en modo LIBRE).

### Control por voz (rama `control_voz`, no mergeada a `main`)

Reconocimiento de voz **100% local en el navegador** (Vosk compilado a
WebAssembly, `vosk-browser`) — el audio nunca sale del celular ni
depende de internet. Es un modelo acústico real entrenado con deep
learning (TDNN), no una simulación; lo que lo hace robusto al ruido de
un venue es que se le da una **gramática cerrada** en vez de
reconocimiento abierto — solo puede decidir entre estas 7 frases:

```
"abrir garra", "cerrar garra", "depositar",
"posicion inicial", "traslado", "parar", "alto"
```

Push-to-talk (mantener apretado el botón de voz, no "siempre
escuchando", para no captar ruido de fondo por error):
`getUserMedia` abre el micrófono → `AudioContext`/`ScriptProcessor`
cortan el audio en bloques de 4096 muestras → se los pasa al
`KaldiRecognizer` en tiempo real → si el resultado final coincide exacto
con una de las 7 frases, dispara la acción (grip, deposit, preset_goto,
o "parar" que corta joysticks/botones de eje al toque).

**Pendiente para que funcione en el celular real:** `getUserMedia` exige
HTTPS o `localhost` (ver sección de abajo) — hoy el dashboard sirve por
HTTP plano, así que el botón de voz falla con "SIN PERMISO DE MIC".
Falta además copiar el modelo (`model.tar.gz`, ~40MB) a
`static/voice/` en la Orange Pi.

## Cableado de motores N20 (3x TB6612FNG, confirmado 18-sept)

Rediseño 18-sept: cada placa TB6612FNG tiene su **propio** par IN1/IN2 y
su propio canal PWM — nada de empalmar un cable en 3. El PWM de
velocidad ya no usa el PWM de hardware de la Pi (solo 2 canales reales
en todo el header) — se movió a 6 canales libres del PCA9685
(`servos.set_motor_pwm`, escritura cruda por registro porque
`adafruit_pca9685.duty_cycle` tira `TypeError` en esta combinación de
SO/versión). Pines confirmados contra `gpio readall` corrido en la
propia placa (no adivinados).

| Señal (nombre en el TB6612FNG) | Placa 1 (delanteras) | Placa 2 (medias) | Placa 3 (traseras) |
|---|---|---|---|
| AIN1 (motor izquierdo) | pin 11 | pin 26 | pin 29 |
| AIN2 (motor izquierdo) | pin 12 | pin 31 | pin 33 |
| PWMA (motor izquierdo) | PCA9685 ch10 | ch11 | ch12 |
| BIN1 (motor derecho) | pin 13 | pin 35 | pin 36 |
| BIN2 (motor derecho) | pin 15 | pin 37 | pin 38 |
| PWMB (motor derecho) | PCA9685 ch13 | ch14 | ch15 |
| GND | pin 9 | pin 14 | pin 20 |

Nodos compartidos (junction real con bloque terminal, no splice de cable
pelado — son potencia/enable estáticos, no señales que necesiten
control independiente por placa):

- **STBY** de las 3 placas → pin 16
- **VCC** lógica de las 3 placas → pin 17 (3.3V — el pin 1 sigue solo
  para el VCC del PCA9685)
- **VM** (6V real, con corriente) de las 3 placas → salida del LM2596,
  en paralelo con el cable que alimenta el PCA9685, **no** encadenado a
  través de su terminal (esas pistas no están pensadas para la corriente
  de 6 motores)

Salidas a motor (directo, 1 a 1, sin empalme — cada placa ya sirve
exactamente a sus 2 motores):

| Placa | AO1/AO2 → | BO1/BO2 → |
|---|---|---|
| 1 | N20 Izq. Delantero | N20 Der. Delantero |
| 2 | N20 Izq. Medio | N20 Der. Medio |
| 3 | N20 Izq. Trasero | N20 Der. Trasero |

Si un motor gira al revés de lo esperado, invertir los 2 cables de esa
salida (AO1↔AO2) en vez de tocar el software.

Pin libre de sobra: **40** (GPIO121) — por si hace falta un botón de
parada de emergencia o un LED de estado.

Trade-off aceptado: el PWM de motor ahora corre a 50Hz (frecuencia fija
del PCA9685, compartida con los servos) en vez de los 1kHz que tenía
antes por sysfs — puede zumbar/vibrar un poco más a velocidad baja.

## HTTPS (necesario para el control por voz, rama `control_voz`)

El microfono del celular (`getUserMedia`) solo funciona en un "contexto
seguro" - `https://` o `localhost`. El dashboard servido por HTTP plano
en la IP de la red local (como esta hoy) no alcanza, asi que main.py
necesita un certificado. Se usa uno autofirmado (no hay forma de
conseguir uno real sin dominio publico) - `config.SSL_KEYFILE`/
`SSL_CERTFILE` apuntan a `percy.key`/`percy.crt` en esta misma carpeta;
si no existen, main.py arranca en HTTP plano igual (sirve para
desarrollo local sin voz).

Generar el certificado (**regenerar si cambia la IP de la Orange Pi** -
el que había era para `192.168.1.41`, la Pi hoy está en `192.168.1.14`
por WiFi, hay que rehacerlo):

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

Todos los pines/canales estan centralizados en `config.py`. Los del
brazo IK (`IK_L2_MM`, `IK_L3_MM`, `IK_TILT_SERVO_AT_ZERO`,
`IK_TILT_SIGN`, `ANGLE_ARM_TILT_MIN/MAX`) son **provisorios** — quedan
pendientes de recalibrar con el brazo rediseñado (Arm2+3 fusionados,
MG996R en la inclinación) ya armado. Los ángulos de servo (`ANGLE_*`,
`DEPOSIT_*`, `STEER_CENTER`) también hay que confirmarlos con el brazo y
las ruedas ya armadas — el steering de las 4 esquinas ya está calibrado
(`STEER_CENTER = {"fl":85,"fr":90,"rl":85,"rr":95}`).

**`config.FAILSAFE_ENABLED = False` hoy, a propósito, para poder
calibrar por curl/SSH sin el dashboard abierto** — volver a poner en
`True` antes de cualquier prueba en pista o competencia.

## Estructura

- `config.py` - todos los pines/canales/angulos en un solo lugar.
- `servos.py` - PCA9685: 9 servos (brazo x4 + gripper + steering x4) por
  ángulo, más `set_motor_pwm` (PWM crudo de motor, ch10-15).
- `kinematics.py` - cinemática inversa del brazo, `solve()` (2 juntas,
  histórico) y `solve3()` (3 juntas acopladas, posición + ángulo de
  acercamiento, la que usa main.py hoy).
- `presets.py` - guardar/ir a posiciones nombradas del brazo (`presets.json`).
- `motors.py` - 6 motores N20 via 3x TB6612FNG (GPIO dirección dedicado
  por placa + PWM por PCA9685, ver sección de cableado arriba).
- `camera.py` - stream MJPEG de la camara USB (no rompe nada si no hay camara).
- `main.py` - FastAPI, WebSocket `/ws`, failsafe, interlock brazo/tracción, sirve `static/`.
- `static/` - dashboard (2 joysticks, botones, control por voz, ver `app.js`).

## API

### WebSocket `/ws`

Mensaje del cliente (todos los campos opcionales, default 0/false):

```json
{
  "lx": -0.8, "ly": 0.6, "speed": 1, "drive_mode": "combinado",
  "arm_mode": "ik", "arm_x_dir": 0, "arm_y_dir": 0, "wrist_dir": 0,
  "base_dir": 0, "grip": 0, "deposit": 0,
  "preset_save": null, "preset_goto": null, "preset_delete": null
}
```

Respuesta del servidor (eco liviano, una vez por mensaje recibido):

```json
{"hit_limit": false}
```

### REST

| Endpoint | Uso |
|---|---|
| `GET /status` | failsafe, cámara disponible, `arm_mode`, `drive_mode`, `arm_busy`, `preset_moving` |
| `GET /servos` | ángulos actuales de cada servo nombrado + si está en modo simulado |
| `GET /presets` | nombres de presets guardados |
| `GET /video` | stream MJPEG de la cámara (si hay) |
| `GET /debug/set_angle?channel=&angle=` | mover un canal puntual — **solo calibración manual, sin auth** |
| `GET /debug/preset_save?name=` | guardar preset actual sin pasar por el dashboard |

## Pendiente / no incluido en esta version

- AP WiFi propio (`hostapd`+`dnsmasq`) - por ahora corre sobre una red
  WiFi existente ("Veronica").
- ToF VL53L0X / IMU MPU-9250 (AutoNav) - los pines XSHUT ya estan en
  `config.py` pero el modulo de sensores no esta escrito todavia.
- Macro "soltar en bandeja" es bloqueante (no permite mover otra cosa
  mientras corre) - si hace falta que sea no bloqueante hay que pasarla a
  una maquina de estados en `main.py`.
- Control por voz: falta el certificado HTTPS para la IP actual y copiar
  el modelo a la Pi (ver secciones arriba) - probado offline en PC, no
  probado todavía en el celular real.
- Recalibrar constantes de IK (`IK_L2_MM`, `IK_L3_MM`, `IK_TILT_*`,
  `ANGLE_ARM_TILT_MIN/MAX`) con el brazo rediseñado ya armado.
- `config.FAILSAFE_ENABLED` en `False` - volver a `True` antes de pista.
- 6 motores N20: cableado en curso, solo el delantero derecho montado al
  18-sept (ver tabla de cableado arriba para el resto).
