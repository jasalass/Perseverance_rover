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
Al 24-sept la Pi está en `192.168.1.46` (la IP la asigna el router por DHCP
y ya cambió varias veces: .41, .14, .13, 60.100 — si no responde, buscarla
por el puerto 8000).

**Reglas operativas aprendidas a la mala:**
- **Un solo dashboard abierto a la vez.** Cada uno manda su estado 20 veces
  por segundo; dos conectados se pisan (el que está quieto manda "parar").
- **Después de copiar archivos, `sync`**, y apagar con `sudo poweroff`, no
  cortando la energía: la SD escribe con retraso y un corte pierde los
  cambios recientes (pasó el 24-sept con `config.py`).
- **Cablear siempre con las dos baterías desconectadas.**

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
                                          |-- I2C --> PCA9685 --> 10 servos + PWM de motores (ch10-15)
                                          |                       (brazo x4, giro garra, garra, steering x4)
                                          '-- GPIO --> 2x TB6612FNG --> 6 motores N20 (en paralelo por lado)
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
| `tanque` | Diferencial entre lados **+** las 4 ruedas en diagonal, patrón de rombo (ver `servos.set_pivot_steering`) | Pivote de radio cero de verdad, sin arrastrar las ruedas de costado — el ángulo (52.5°) se calculó de las medidas reales del chasis (18cm trocha, 23.5cm distancia entre ejes) |

Corregido 24-sept: en `combinado`/`vehiculo` las ruedas **traseras giran al
revés que las delanteras** (antes las 4 iban al mismo lado y el rover se
desplazaba de costado en vez de girar). En `tanque` el rombo es el mismo
para los dos sentidos de giro (se usa `abs(lx)`; el sentido lo pone el
diferencial) — antes, hacia un lado, formaba el rombo invertido y las
ruedas se oponían al giro.

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

Mejoras de la IK del 21-sept: si el punto pedido cabe pero el ángulo de
acercamiento no (la inclinación tiene rango corto y suele ser la primera en
tocar tope), **se prioriza la posición de la punta** y se suelta el ángulo
lo mínimo necesario, en vez de congelar todo el brazo. Además hay una
**guarda anti-latigazo** (`config.IK_MAX_JOINT_STEP_DEG = 8`): una solución
que pida mover una junta más de 8° en un solo tick se rechaza.

**Giro de garra** (canal 4, MG90S): eje independiente de la IK (girar la
garra no mueve la punta), con sus propios botones "GIRO GARRA" en el
dashboard (`gripper_rotate_dir`). Centro en 90°.

Calibración del brazo rearmado (21-sept, en `config.py`):

| Junta | Cero (horizontal) | Escala/signo | Rango servo |
|---|---|---|---|
| Hombro (ch1) | 90° | +1 | 45°–175° |
| Codo (ch2) | 68° (horn movido 3 dientes) | −1.19 | 10°–175° |
| Inclinación (ch3) | 30° (horn movido 1 diente) | +1 | 5°–135° |
| Giro garra (ch4) | 90° = derecha | — | 10°–170° |

Largos: L1 = 120 mm (medido), **L2 = 115 / L3 = 135 mm estimados** (suman
los 250 mm medidos de codo a punta) — confirmar si la punta no se mueve recta.

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

### Control por voz

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

## Cableado de motores N20 (estado real al 24-sept)

Cada placa TB6612FNG tiene su **propio** par IN1/IN2 por GPIO dedicado y su
propio canal PWM en el PCA9685 (`servos.set_motor_pwm`, escritura cruda por
registro porque `adafruit_pca9685.duty_cycle` tira `TypeError` en esta
combinación de SO/versión). La Pi solo tiene 2 PWM de hardware reales, por
eso el PWM de motores va por el PCA9685.

**Cómo quedó el hardware:**
- **Solo 2 placas.** La placa 3 se dañó (STBY y VCC en corto con GND por
  dentro, <10 Ω) y se retiró. El canal B de la placa 2 también está dañado
  (su entrada BIN2 carga la línea: 2.7 V con el cable, 3.2 V sin él); su
  cable de BIN2 queda desconectado.
- **Motores cableados cruzados:** canal **A (AO) = motores derechos**,
  canal **B (BO) = motores izquierdos**. Se corrigió en `config.py` (el
  lado izquierdo usa los pines del canal B), no se recableó.
- **Motores en paralelo por lado** (todos los de un lado reciben siempre el
  mismo comando, así que no se pierde control):

| Salida | Motores |
|---|---|
| Placa 1, BO1/BO2 | izquierdos (delantero, central y trasero) |
| Placa 1, AO1/AO2 | derecho delantero |
| Placa 2, AO1/AO2 | derecho central + derecho trasero |
| Placa 2, canal B | nada (dañado) |

**Señales (pines físicos del header):**

| Señal | Placa 1 | Placa 2 |
|---|---|---|
| AIN1 / AIN2 (derecha) | 11 / 12 | 26 / 31 |
| PWMA | PCA9685 ch10 | ch11 |
| BIN1 / BIN2 (izquierda) | 13 / 15 | 35 / (40, sin conectar) |
| PWMB | PCA9685 ch13 | ch14 |
| STBY | pin 16 (puenteado) | pin 16 (puenteado) |
| VCC lógica | pin 17 (puenteado) | pin 17 (puenteado) |
| VM | +6V del LM2596 | +6V del LM2596 |
| GND | tierra de potencia (estrella) | tierra de potencia (estrella) |

El software sigue manejando las líneas de la placa 3 (29/33/36/38, ch12/15):
no hay nada conectado ahí y no afecta.

**Ojo con la revisión de la Orange Pi:** en la placa de reemplazo (24-sept)
el **pin físico 26 es GPIO126**; en la original era GPIO135. Si se cambia de
Orange Pi, correr `gpio readall` y comparar antes de conectar.

**Tierra:** los GND de las placas van a la tierra de potencia (negativo del
LM2596), no a los pines GND de la Pi. Si se suelta el negativo de los
motores, no gira ninguno (pasó el 24-sept). Diagrama completo (con 3 placas,
antes de retirar la tercera) en `docs/diagrama_electrico.html`, local.

Si un motor gira al revés de sus compañeros de lado, invertir sus 2 cables
en el borne en vez de tocar el software.

Trade-off aceptado: el PWM de motor corre a 50Hz (frecuencia fija del
PCA9685, compartida con los servos) — puede zumbar un poco a velocidad baja.

## WiFi: redes de la casa + red propia de respaldo (24-sept)

- **En la casa** se conecta solo a las redes guardadas (`Wifi_Mesh-9ABF91B8`, `GLADYS`).
- **En cualquier otro lugar**, si en ~1 minuto no logra conectarse a ninguna,
  el servicio `percy-wifi-watchdog` crea la **red propia `PercyRover`**
  (clave `marte2026`, cambiable) → dashboard en **`http://10.42.0.1:8000`**.
  No dejar guardadas redes del evento: si la Pi se conecta a una red ajena,
  hay que averiguar su IP.
- **Panel WIFI en el dashboard** (ícono al lado de las posiciones, mismo PIN):
  estado, buscar redes, conectarse (la clave se guarda en la Pi), olvidar,
  desconectar, y activar/apagar la red propia. Cambiar de red corta la
  conexión del celular: el panel avisa a qué red pasarse.
- Si una conexión nueva falla, la Pi vuelve a la red anterior; si tampoco
  puede, crea la red propia. Nunca queda incomunicada.
- **Encontrar el rover en una red nueva** (IP desconocida):
  - **Aviso ntfy:** cada vez que el rover queda en una red (o cambia de IP)
    publica su IP en un canal privado de ntfy; tocar la notificación abre
    el dashboard. Canal generado al azar por `install.sh` en
    `/etc/percy-wifi-topic` (no está en el repo; se ve en el panel WIFI).
    Necesita internet en esa red.
  - **Nombre fijo:** `http://percy.local:8000` (hostname `percy` + avahi).
    Funciona en PC e iPhone; en Android no siempre.

Implementación: `orangepi/wifi/percy-wifi` (script sobre `nmcli`, instalado en
`/usr/local/bin` como root; el backend lo llama con una regla `sudo` acotada a
ese único script) + `percy-wifi-watchdog.service`. Instalar con
`sudo sh orangepi/wifi/install.sh`. Las claves quedan solo en la Pi
(NetworkManager), nunca en este repo; para agregar una red a mano:
`sudo percy-wifi save "<ssid>" "<clave>"`.

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
el que había era para `192.168.1.41`, la Pi hoy está en `192.168.1.46`,
hay que rehacerlo):

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

## Calibración y seguridad

Todos los pines/canales/ángulos están en `config.py`. Brazo recalibrado el
21-sept (tabla en la sección del brazo). Steering de las 4 esquinas
calibrado (`STEER_CENTER = {"fl":85,"fr":90,"rl":85,"rr":95}`). Sin
calibrar todavía: garra abrir/cerrar, pose de depósito (`DEPOSIT_*`) y pose
de traslado.

**`config.FAILSAFE_ENABLED = False` hoy, a propósito, para poder calibrar
por curl/SSH sin el dashboard abierto** — volver a poner en `True` antes de
cualquier prueba en pista o competencia.

## Estructura

- `config.py` - todos los pines/canales/angulos en un solo lugar.
- `servos.py` - PCA9685: 10 servos (brazo x4, giro garra, garra, steering x4) por
  ángulo, más `set_motor_pwm` (PWM crudo de motor, ch10-15).
- `kinematics.py` - cinemática inversa del brazo, `solve()` (2 juntas,
  histórico) y `solve3()` (3 juntas acopladas, posición + ángulo de
  acercamiento, la que usa main.py hoy).
- `presets.py` - guardar/ir a posiciones nombradas del brazo (`presets.json`).
- `motors.py` - 6 motores N20 via 2x TB6612FNG (GPIO dirección dedicado
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
  "base_dir": 0, "gripper_rotate_dir": 0, "grip": 0, "deposit": 0,
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

- `config.FAILSAFE_ENABLED` en `False` - volver a `True` antes de pista.
- Garra abrir/cerrar, pose de depósito y pose de traslado sin calibrar.
- Largos L2/L3 de la IK estimados, no medidos.
- Control por voz: falta el certificado HTTPS para la IP actual y copiar
  el modelo a la Pi - probado offline en PC, no en el celular real.
- ToF VL53L0X / IMU MPU-9250 (AutoNav) - pines reservados, código no escrito.
- Macro "soltar en bandeja" es bloqueante.
- Faltan fusible, interruptor general y condensadores en el rail de 6V.
- Conseguir TB6612FNG de repuesto (hoy 2 placas, una con un canal dañado).
