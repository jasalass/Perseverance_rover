"""
Percy - servidor de control (Orange Pi 3B).

FastAPI + WebSocket recibe el JSON de control del dashboard (celular),
traduce a PCA9685 (servos) y GPIO/PWM (motores TB6612FNG). Sirve tambien
el dashboard estatico y el stream MJPEG de la camara.

Correr con:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import json
import logging
import math
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

import config
import kinematics
from camera import CameraStream
from motors import MotorController
from presets import PresetManager
from servos import ServoController

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("percy.main")

app = FastAPI(title="Percy Rover Control")


@app.middleware("http")
async def no_cache(request, call_next):
    """El dashboard esta en desarrollo activo y el celular de campo cachea
    agresivamente (ya causo que se viera roto con CSS viejo + HTML nuevo
    mezclados) - se desactiva el cache por completo, no hay ningun beneficio
    de rendimiento real en una app de un solo operador en la red local."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


servos = ServoController()
motors = MotorController()
camera = CameraStream()  # autodetecta el indice por nombre - ver camera.py
presets = PresetManager(servos)

_last_message_ts = time.monotonic()
_failsafe_tripped = False

# Velocidad PEDIDA por el operador (ultimo mensaje WS) vs velocidad ACTUAL
# que de verdad se le manda al brazo - el loop de easing rampea la actual
# hacia la pedida en vez de saltar de golpe (ver _arm_easing_loop abajo y
# config.ARM_MAX_ACCEL_PER_TICK). Sin esto, cada mensaje del joystick
# aplicaba su velocidad instantaneamente -> se sentia brusco/a tirones.
# Hombro/codo/inclinacion ya NO estan aca (18-sept) - pasaron a control
# por IK acoplada, ver _ik_target_vel/_ik_current_vel/_ik_pos mas abajo.
_arm_target = {"base_dir": 0.0}
_arm_current = {k: 0.0 for k in _arm_target}

# --- Control del brazo por cinematica inversa (rediseño 18-sept) ------------
# El joystick derecho mueve la POSICION (x,y) de la punta de la garra en
# linea recta, y los botones que antes eran de la muneca mueven el ANGULO
# DE ACERCAMIENTO (phi) - kinematics.solve3() calcula que angulo de
# hombro/codo/inclinacion hace falta para cumplir las dos cosas a la vez
# (brazo 2+3 fusionados, la inclinacion ya no gira sobre si misma, es un
# eslabon mas - ver kinematics.py). Mismo patron velocidad+rampa que el
# resto del brazo, integrando x/y/phi en vez de angulos de servo sueltos.
_ik_target_vel = {"x_dir": 0.0, "y_dir": 0.0, "phi_dir": 0.0}  # -1..1 pedido
_ik_current_vel = {k: 0.0 for k in _ik_target_vel}  # -1..1 ya rampeado

# Posicion cartesiana objetivo (mm, origen en el eje del hombro) y angulo
# de acercamiento objetivo (grados, misma convencion que phi_deg en
# solve3). Se sincronizan con la posicion REAL del brazo (via
# kinematics.forward_from_servo/current_phi) recien cuando hace falta, no
# con un valor inventado. _ik_pos_dirty fuerza esa resincronizacion cada
# vez que algo mueve hombro/codo/inclinacion por fuera de este loop
# (preset, macro de deposito, failsafe, debug endpoint) - si no, el
# control por IK seguiria de donde el software CREIA que estaba, no de
# donde el brazo quedo de verdad.
_ik_pos = {"x": 0.0, "y": 0.0}
_ik_phi = 0.0
_ik_pos_dirty = True
# Rama de la solucion IK (codo "arriba" o "abajo", ver kinematics.solve) -
# se recalcula desde la pose real al resincronizar, para no saltar de rama
# sola (ver kinematics.infer_elbow_up).
_ik_elbow_up = True

# Switch IK/LIBRE del dashboard (16-sept): "ik" = joystick derecho mueve la
# punta de la garra en linea recta y los botones mueven phi (ver arriba).
# "joint" = joystick derecho vuelve a mover hombro/codo cada uno por su
# cuenta y los botones mueven la inclinacion directo, sin coordinacion -
# util para calibrar o alcanzar posiciones que la IK no puede (la zona
# muerta del anillo, ver config.py). Mismos campos del joystick/botones
# para los dos modos, el servidor decide que significan segun _arm_mode -
# no hace falta un protocolo distinto.
_arm_mode = "ik"

# Feedback haptico (18-sept): True mientras el brazo esta empujando contra
# un limite (pared blanda de la IK, o tope de articulacion en modo LIBRE) -
# se manda de vuelta al dashboard por WS para que vibre el celular. Flanco
# ascendente nomas (el frontend vibra en el cambio False->True, no todo el
# rato que se mantiene contra el limite).
_arm_hit_limit = False

# Modo de traccion (18-sept): "combinado" (default) mueve las 4 ruedas de
# esquina Y crea diferencia de velocidad entre lados con el mismo lx a la
# vez - un poco de las dos ventajas en cada giro. "vehiculo" gira SOLO con
# las ruedas de esquina (los dos lados a la misma velocidad, como un auto)
# - menos esfuerzo/desgaste en terreno suelto, pero necesita mas espacio
# para girar. "tanque" gira SOLO por diferencial entre lados, con las
# ruedas de esquina fijas al centro (derecho) - radio de giro cero, util
# en espacios chicos, pero arrastra las ruedas lateralmente contra el piso.
_drive_mode = "combinado"


def _apply_command(data: dict):
    global _last_message_ts, _failsafe_tripped, _arm_mode, _drive_mode
    _last_message_ts = time.monotonic()
    _failsafe_tripped = False

    arm_mode = data.get("arm_mode")
    if arm_mode in ("ik", "joint") and arm_mode != _arm_mode:
        _arm_mode = arm_mode
        if arm_mode == "ik":
            # volviendo de LIBRE a IK - releer la pose real en vez de
            # seguir desde el objetivo cartesiano viejo (puede haber
            # cambiado mientras estuvo en modo LIBRE).
            _mark_ik_dirty()

    drive_mode = data.get("drive_mode")
    if drive_mode in ("combinado", "vehiculo", "tanque"):
        _drive_mode = drive_mode

    lx = float(data.get("lx", 0.0))
    ly = float(data.get("ly", 0.0))
    speed = int(data.get("speed", 1))

    if _drive_mode == "vehiculo":
        # Solo dirigen las ruedas de esquina - los dos lados a la misma
        # velocidad, sin diferencial. lx NO entra en left/right.
        left = right = ly
        servos.set_steering(lx)
    elif _drive_mode == "tanque":
        # Solo diferencial entre lados - ruedas de esquina fijas al centro
        # (no arrastran contra el giro).
        left = ly + lx
        right = ly - lx
        m = max(abs(left), abs(right), 1.0)
        left, right = left / m, right / m
        servos.set_steering(0.0)
    else:  # "combinado" (default)
        left = ly + lx
        right = ly - lx
        m = max(abs(left), abs(right), 1.0)
        left, right = left / m, right / m
        servos.set_steering(lx)

    motors.set_motors(left, right, speed)

    # El brazo no se mueve aca directo - solo se actualiza la velocidad
    # PEDIDA. _arm_easing_loop es el que de verdad llama a servos.move_arm
    # (base) y kinematics.solve3() (hombro/codo/inclinacion por IK) o
    # servos.move_arm_joint_direct() (modo LIBRE). "wrist_dir" es el mismo
    # campo del protocolo de siempre (los botones que eran de la muneca) -
    # ahora alimenta phi_dir en vez de mover un canal directo.
    _arm_target["base_dir"] = float(data.get("base_dir", 0))
    _ik_target_vel["x_dir"] = float(data.get("arm_x_dir", 0.0))
    _ik_target_vel["y_dir"] = float(data.get("arm_y_dir", 0.0))
    _ik_target_vel["phi_dir"] = float(data.get("wrist_dir", 0.0))

    if data.get("grip"):
        servos.toggle_gripper()
    if data.get("deposit"):
        servos.deposit_macro()
        _mark_ik_dirty()

    preset_save = data.get("preset_save")
    if preset_save:
        presets.save_current(str(preset_save))
    preset_goto = data.get("preset_goto")
    if preset_goto:
        asyncio.create_task(_goto_preset_and_resync(str(preset_goto)))
    preset_delete = data.get("preset_delete")
    if preset_delete:
        presets.delete(str(preset_delete))


def _mark_ik_dirty():
    """Hombro/codo se movieron por fuera del loop de IK (preset, macro de
    deposito, failsafe) - la proxima vez que el joystick de IK se use, hay
    que releer la posicion real del brazo en vez de seguir desde el ultimo
    objetivo cartesiano calculado, que ya no es donde esta el brazo."""
    global _ik_pos_dirty
    _ik_pos_dirty = True


async def _goto_preset_and_resync(name: str):
    await presets.goto(name)
    _mark_ik_dirty()


def _ramp(current: float, target: float, max_step: float) -> float:
    diff = target - current
    if abs(diff) <= max_step:
        return target
    return current + (max_step if diff > 0 else -max_step)


async def _arm_easing_loop():
    """Rampea _arm_current/_ik_current_vel hacia sus objetivos a lo sumo
    config.ARM_MAX_ACCEL_PER_TICK por tick, y recien ahi mueve el brazo de
    verdad - esto es lo que da la aceleracion/frenado suave en vez de saltar
    directo a la velocidad pedida por el joystick/boton."""
    global _ik_pos_dirty, _ik_elbow_up, _arm_hit_limit, _ik_phi
    tick_s = 1.0 / config.ARM_EASING_HZ
    while True:
        await asyncio.sleep(tick_s)
        for axis, target in _arm_target.items():
            _arm_current[axis] = _ramp(_arm_current[axis], target, config.ARM_MAX_ACCEL_PER_TICK)
        if any(_arm_current.values()):
            servos.move_arm(base_dir=_arm_current["base_dir"])

        for axis, target in _ik_target_vel.items():
            _ik_current_vel[axis] = _ramp(_ik_current_vel[axis], target, config.ARM_MAX_ACCEL_PER_TICK)

        if _arm_mode == "joint":
            # Modo LIBRE: mismos 3 ejes (x_dir/y_dir/phi_dir), pero
            # interpretados como velocidad de CADA articulacion por
            # separado (sin coordinar) - ver servos.move_arm_joint_direct.
            # Vertical -> hombro, horizontal -> codo, botones -> inclinacion
            # directo (igual mapeo que tenia el control viejo).
            if any(_ik_current_vel.values()):
                _arm_hit_limit = servos.move_arm_joint_direct(
                    shoulder_dir=_ik_current_vel["y_dir"],
                    elbow_dir=_ik_current_vel["x_dir"],
                    tilt_dir=_ik_current_vel["phi_dir"],
                )
            else:
                _arm_hit_limit = False
            continue

        if any(_ik_current_vel.values()):
            if _ik_pos_dirty:
                # Recortar ACA a los limites de seguridad de la IK antes de
                # convertir a (x,y,phi) - si el brazo quedo fuera de ese
                # rango por algo que no lo respeta (un preset, o modo
                # LIBRE), arrancar desde el valor crudo real dejaria el
                # punto de partida ya invalido, y el freeze de mas abajo lo
                # revierte siempre contra si mismo -> el joystick queda
                # trabado para siempre (bug 16-sept, ver ese fix original).
                # Margen chico (no justo al borde) para que el error de
                # punto flotante del viaje angulo->xy->angulo no vuelva a
                # caer del otro lado del limite.
                current_shoulder_servo = max(config.ANGLE_ARM_SHOULDER_MIN + 0.5, min(
                    config.ANGLE_ARM_SHOULDER_MAX - 0.5, servos.get_angle(config.CH_ARM_SHOULDER)))
                current_elbow_servo = max(config.ANGLE_ARM_ELBOW_MIN + 0.5, min(
                    config.ANGLE_ARM_ELBOW_MAX - 0.5, servos.get_angle(config.CH_ARM_ELBOW)))
                current_tilt_servo = max(config.ANGLE_ARM_TILT_MIN + 0.5, min(
                    config.ANGLE_ARM_TILT_MAX - 0.5, servos.get_angle(config.CH_ARM_WRIST)))
                wrist_x, wrist_y = kinematics.forward_from_servo(current_shoulder_servo, current_elbow_servo)
                _ik_elbow_up = kinematics.infer_elbow_up(current_elbow_servo)
                _ik_phi = kinematics.current_phi(current_shoulder_servo, current_elbow_servo, current_tilt_servo)
                phi_rad = math.radians(_ik_phi)
                _ik_pos["x"] = wrist_x + config.IK_L3_MM * math.cos(phi_rad)
                _ik_pos["y"] = wrist_y + config.IK_L3_MM * math.sin(phi_rad)
                _ik_pos_dirty = False

            prev_x, prev_y, prev_phi = _ik_pos["x"], _ik_pos["y"], _ik_phi

            # SHOULDER_DOWN_STEP_SCALE (0.12) YA NO se aplica aca (16-sept):
            # era para el control viejo por articulacion, donde el hombro
            # solo podia acelerar de golpe por gravedad entre un paso grande
            # y el siguiente. Con el objetivo cartesiano moviendose en pasos
            # chicos y continuos (mm/tick, no grados/tick de un joint), ese
            # riesgo es mucho menor - y dejarlo aplicado hacia abajo hacia
            # que bajar tardara ~20s completos desde una posicion alta (se
            # sentia como que "no baja"). Si al bajar se siente a tirones de
            # verdad, se puede reintroducir un factor mas suave (ej. 0.6-0.8)
            # en vez del 0.12 original.
            _ik_pos["x"] += _ik_current_vel["x_dir"] * config.IK_CARTESIAN_SPEED_MM_S * tick_s
            _ik_pos["y"] += _ik_current_vel["y_dir"] * config.IK_CARTESIAN_SPEED_MM_S * tick_s
            _ik_phi += _ik_current_vel["phi_dir"] * config.IK_PHI_SPEED_DEG_S * tick_s

            result = kinematics.solve3(_ik_pos["x"], _ik_pos["y"], _ik_phi, elbow_up=_ik_elbow_up)
            # +-0.01 de margen: el viaje angulo->xy->angulo no siempre
            # vuelve exacto (error de punto flotante), sin esto un punto
            # justo en el limite podia rechazarse por una diferencia de
            # 1e-14 grados (ver bug 16-sept).
            eps = 0.01
            shoulder_ok = (config.ANGLE_ARM_SHOULDER_MIN - eps) <= result.shoulder_angle_raw <= (config.ANGLE_ARM_SHOULDER_MAX + eps)
            elbow_ok = (config.ANGLE_ARM_ELBOW_MIN - eps) <= result.elbow_angle_raw <= (config.ANGLE_ARM_ELBOW_MAX + eps)
            tilt_ok = (config.ANGLE_ARM_TILT_MIN - eps) <= result.tilt_angle_raw <= (config.ANGLE_ARM_TILT_MAX + eps)
            if result.reachable and shoulder_ok and elbow_ok and tilt_ok:
                servos.set_angle(config.CH_ARM_SHOULDER, result.shoulder_angle)
                servos.set_angle(config.CH_ARM_ELBOW, result.elbow_angle)
                servos.set_angle(config.CH_ARM_WRIST, result.tilt_angle)
                _arm_hit_limit = False
            else:
                # "Pared blanda": el punto/angulo pedido no se puede
                # cumplir de verdad (fuera de alcance, o algun angulo de
                # servo necesario cae fuera de su rango real - ver nota
                # 16-sept del bug original con el codo). Si se dejara
                # clampear el angulo de salida nomas, el objetivo interno
                # (_ik_pos/_ik_phi) seguiria alejandose de lo que el servo
                # puede cumplir de verdad, y el brazo "saltaba" al volver a
                # tocar el joystick. Se congela el objetivo en el ultimo
                # punto que si se pudo en vez de eso - no se manda nada
                # nuevo, el brazo se queda quieto ahi.
                _ik_pos["x"], _ik_pos["y"], _ik_phi = prev_x, prev_y, prev_phi
                _arm_hit_limit = True
        else:
            _arm_hit_limit = False


async def _failsafe_watchdog():
    global _failsafe_tripped
    if not config.FAILSAFE_ENABLED:
        log.warning(
            "FAILSAFE DESACTIVADO (config.FAILSAFE_ENABLED=False) - solo para "
            "calibrar por script sin el dashboard. Volver a activar antes de "
            "pista/competencia."
        )
        return
    while True:
        await asyncio.sleep(0.1)
        if not _failsafe_tripped and (time.monotonic() - _last_message_ts) > config.FAILSAFE_TIMEOUT_S:
            motors.failsafe_stop()
            servos.failsafe_stop()
            for k in _arm_target:
                _arm_target[k] = 0.0
                _arm_current[k] = 0.0
            for k in _ik_target_vel:
                _ik_target_vel[k] = 0.0
                _ik_current_vel[k] = 0.0
            _mark_ik_dirty()
            _failsafe_tripped = True


@app.on_event("startup")
async def on_startup():
    camera.start()
    asyncio.create_task(_failsafe_watchdog())
    asyncio.create_task(_arm_easing_loop())
    log.info("Percy listo en http://%s:%s", config.WS_HOST, config.WS_PORT)


@app.websocket("/ws")
async def ws_control(websocket: WebSocket):
    await websocket.accept()
    log.info("Cliente conectado")
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            _apply_command(data)
            # Eco liviano de vuelta (18-sept) - el dashboard lo usa para el
            # feedback haptico (vibrar al tocar un limite del brazo). El WS
            # ya estaba abierto y el cliente ya manda 20 msj/seg, asi que
            # no hace falta polling nuevo para esto.
            await websocket.send_text(json.dumps({"hit_limit": _arm_hit_limit}))
    except WebSocketDisconnect:
        log.info("Cliente desconectado")


@app.get("/video")
async def video_feed():
    if not camera.is_available():
        return {"error": "camara no disponible"}
    return StreamingResponse(
        camera.mjpeg_generator(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/status")
async def status():
    return {
        "failsafe_tripped": _failsafe_tripped,
        "camera_available": camera.is_available(),
        "seconds_since_last_command": round(time.monotonic() - _last_message_ts, 2),
        "preset_moving": presets.moving,
        "arm_mode": _arm_mode,
        "drive_mode": _drive_mode,
    }


@app.get("/presets")
async def presets_list():
    """Nombres de los presets guardados - el dashboard los pide apenas se
    destraba el panel de posiciones para armar el menu."""
    return {"names": presets.list_names()}


@app.get("/servos")
async def servos_status():
    """Angulos actuales de cada servo con nombre, tal como los tiene
    registrados el software - util para leer el estado en vivo (ej. por
    curl via SSH) sin tener que adivinar ni sacar el brazo de servicio."""
    return {"simulated": servos.simulated, "angles": servos.get_all_angles()}


@app.get("/debug/set_angle")
async def debug_set_angle(channel: int, angle: float):
    """SOLO para calibracion manual por curl/SSH (ej. calibrar IK_*_SERVO_AT_ZERO)
    - mueve un canal puntual sin pasar por el protocolo normal del
    dashboard. Sin autenticacion - no exponer fuera de la red local."""
    servos.set_angle(channel, angle)
    if channel in (config.CH_ARM_SHOULDER, config.CH_ARM_ELBOW, config.CH_ARM_WRIST):
        _mark_ik_dirty()  # si no, el IK sigue el objetivo cartesiano viejo
    return {"channel": channel, "angle": servos.get_angle(channel)}


@app.get("/debug/preset_save")
async def debug_preset_save(name: str):
    """SOLO para calibracion manual por curl/SSH - guarda la posicion
    actual del brazo como preset sin pasar por el dashboard."""
    presets.save_current(name)
    return {"names": presets.list_names()}


app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import os

    import uvicorn

    ssl_kwargs = {}
    if os.path.exists(config.SSL_KEYFILE) and os.path.exists(config.SSL_CERTFILE):
        ssl_kwargs = {"ssl_keyfile": config.SSL_KEYFILE, "ssl_certfile": config.SSL_CERTFILE}
        log.info("HTTPS activo (cert autofirmado) - el navegador va a pedir aceptar la conexion la primera vez")
    else:
        log.warning(
            "Sin %s/%s - arrancando en HTTP plano. El control por voz (getUserMedia) "
            "NO va a funcionar en el celular sin HTTPS (localhost si sirve para pruebas locales).",
            config.SSL_KEYFILE, config.SSL_CERTFILE,
        )
    uvicorn.run(app, host=config.WS_HOST, port=config.WS_PORT, **ssl_kwargs)
