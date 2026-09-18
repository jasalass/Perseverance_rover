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
# Hombro/codo ya NO estan aca (16-sept) - pasaron a control por IK, ver
# _ik_target_vel/_ik_current_vel/_ik_pos mas abajo.
_arm_target = {"base_dir": 0.0, "wrist_dir": 0.0, "gripper_rotate_dir": 0.0}
_arm_current = {k: 0.0 for k in _arm_target}

# --- Control del brazo por cinematica inversa (16-sept) ---------------------
# Cambio de paradigma: el joystick derecho ya no mueve hombro/codo como dos
# articulaciones independientes - mueve la PUNTA DE LA GARRA en linea recta
# (arriba/abajo, adelante/atras) y kinematics.solve() calcula que angulo de
# hombro/codo hace falta para llegar ahi. Mismo patron velocidad+rampa que
# el resto del brazo (ver _ik_target_vel/_ik_current_vel), pero integrando
# una posicion cartesiana (_ik_pos) en vez de un angulo de servo.
_ik_target_vel = {"x_dir": 0.0, "y_dir": 0.0}  # -1..1 pedido por el joystick
_ik_current_vel = {"x_dir": 0.0, "y_dir": 0.0}  # -1..1 ya rampeado

# Posicion cartesiana objetivo (mm, origen en el eje del hombro). None =
# "no inicializada todavia" - se sincroniza con la posicion REAL del brazo
# (via kinematics.forward_from_servo) recien cuando hace falta, no con un
# valor inventado. _ik_pos_dirty fuerza esa resincronizacion cada vez que
# algo mueve hombro/codo por fuera de este loop (preset, macro de deposito,
# failsafe) - si no, el control por IK seguiria de donde el software CREIA
# que estaba, no de donde el brazo quedo de verdad.
_ik_pos = {"x": 0.0, "y": 0.0}
_ik_pos_dirty = True
# Rama de la solucion IK (codo "arriba" o "abajo", ver kinematics.solve) -
# se recalcula desde la pose real al resincronizar, para no saltar de rama
# sola (ver kinematics.infer_elbow_up).
_ik_elbow_up = True

# Switch IK/LIBRE del dashboard (16-sept): "ik" = joystick derecho mueve la
# punta de la garra en linea recta (ver arriba). "joint" = joystick derecho
# vuelve a mover hombro/codo cada uno por su cuenta, como antes del cambio
# de paradigma - util para calibrar o alcanzar posiciones que la IK no
# puede (la zona muerta del anillo, ver config.py). Mismos campos del
# joystick (arm_x_dir/arm_y_dir) para los dos modos, el servidor decide
# que significan segun _arm_mode - no hace falta un protocolo distinto.
_arm_mode = "ik"


def _apply_command(data: dict):
    global _last_message_ts, _failsafe_tripped, _arm_mode
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

    lx = float(data.get("lx", 0.0))
    ly = float(data.get("ly", 0.0))
    speed = int(data.get("speed", 1))

    left = ly + lx
    right = ly - lx
    m = max(abs(left), abs(right), 1.0)
    left, right = left / m, right / m

    motors.set_motors(left, right, speed)
    servos.set_steering(lx)

    # El brazo no se mueve aca directo - solo se actualiza la velocidad
    # PEDIDA. _arm_easing_loop es el que de verdad llama a servos.move_arm
    # (base/muneca/giro de garra) y kinematics.solve() (hombro/codo por IK).
    _arm_target["base_dir"] = float(data.get("base_dir", 0))
    _arm_target["wrist_dir"] = float(data.get("wrist_dir", 0))
    _arm_target["gripper_rotate_dir"] = float(data.get("gripper_rotate_dir", 0))
    _ik_target_vel["x_dir"] = float(data.get("arm_x_dir", 0.0))
    _ik_target_vel["y_dir"] = float(data.get("arm_y_dir", 0.0))

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
    global _ik_pos_dirty, _ik_elbow_up
    tick_s = 1.0 / config.ARM_EASING_HZ
    while True:
        await asyncio.sleep(tick_s)
        for axis, target in _arm_target.items():
            _arm_current[axis] = _ramp(_arm_current[axis], target, config.ARM_MAX_ACCEL_PER_TICK)
        if any(_arm_current.values()):
            servos.move_arm(
                base_dir=_arm_current["base_dir"],
                wrist_dir=_arm_current["wrist_dir"],
                gripper_rotate_dir=_arm_current["gripper_rotate_dir"],
            )

        for axis, target in _ik_target_vel.items():
            _ik_current_vel[axis] = _ramp(_ik_current_vel[axis], target, config.ARM_MAX_ACCEL_PER_TICK)

        if _arm_mode == "joint":
            # Modo LIBRE: mismos ejes del joystick derecho (x_dir/y_dir),
            # pero interpretados como velocidad de CADA articulacion por
            # separado - ver servos.move_arm_joint_direct. Vertical ->
            # hombro, horizontal -> codo (igual mapeo que tenia el control
            # viejo, antes del cambio a IK).
            if _ik_current_vel["x_dir"] or _ik_current_vel["y_dir"]:
                servos.move_arm_joint_direct(
                    shoulder_dir=_ik_current_vel["y_dir"],
                    elbow_dir=_ik_current_vel["x_dir"],
                )
            continue

        if _ik_current_vel["x_dir"] or _ik_current_vel["y_dir"]:
            if _ik_pos_dirty:
                # Recortar ACA a los limites de seguridad de la IK antes de
                # convertir a (x,y) - si el brazo quedo fuera de ese rango
                # por algo que no lo respeta (un preset, ej. "traslado" a
                # 180/0 con ANGLE_ARM_SHOULDER_MAX=175), arrancar desde el
                # valor crudo real dejaria el punto de partida ya invalido,
                # y la "pared blanda" de mas abajo lo revierte siempre
                # contra si mismo -> el joystick queda trabado para
                # siempre (bug 16-sept). Arrancando ya recortado, el primer
                # movimiento puede "pegar el salto" final hasta el limite
                # seguro, pero despues responde normal.
                # Un margen chico (no justo al borde) para que el error de
                # punto flotante del viaje angulo->xy->angulo no vuelva a
                # caer del otro lado del limite (ver bug 16-sept - clampear
                # a EXACTO 10.0 podia volver como 9.999999999999986).
                current_shoulder_servo = max(config.ANGLE_ARM_SHOULDER_MIN + 0.5, min(
                    config.ANGLE_ARM_SHOULDER_MAX - 0.5, servos.get_angle(config.CH_ARM_SHOULDER)))
                current_elbow_servo = max(config.ANGLE_ARM_ELBOW_MIN + 0.5, min(
                    config.ANGLE_ARM_ELBOW_MAX - 0.5, servos.get_angle(config.CH_ARM_ELBOW)))
                _ik_pos["x"], _ik_pos["y"] = kinematics.forward_from_servo(current_shoulder_servo, current_elbow_servo)
                _ik_elbow_up = kinematics.infer_elbow_up(current_elbow_servo)
                _ik_pos_dirty = False

            prev_x, prev_y = _ik_pos["x"], _ik_pos["y"]

            y_dir = _ik_current_vel["y_dir"]
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
            _ik_pos["y"] += y_dir * config.IK_CARTESIAN_SPEED_MM_S * tick_s

            # "Pared blanda" #1: si el objetivo se va mas lejos o mas cerca
            # del hombro de lo que el largo de los eslabones permite, se
            # proyecta de vuelta al borde alcanzable.
            l1, l2 = config.IK_L1_MM, config.IK_L2_MM
            d = math.hypot(_ik_pos["x"], _ik_pos["y"])
            d_min, d_max = abs(l1 - l2) + 1.0, l1 + l2 - 1.0
            if d > d_max:
                scale = d_max / d
                _ik_pos["x"] *= scale
                _ik_pos["y"] *= scale
            elif d < d_min:
                scale = d_min / d if d > 1e-6 else 0.0
                if scale:
                    _ik_pos["x"] *= scale
                    _ik_pos["y"] *= scale
                else:
                    _ik_pos["x"], _ik_pos["y"] = d_min, 0.0

            result = kinematics.solve(_ik_pos["x"], _ik_pos["y"], elbow_up=_ik_elbow_up)
            # +-0.01 de margen: el viaje angulo->xy->angulo no siempre
            # vuelve exacto (error de punto flotante), sin esto un punto
            # justo en el limite podia rechazarse por una diferencia de
            # 1e-14 grados (ver bug 16-sept).
            eps = 0.01
            shoulder_ok = (config.ANGLE_ARM_SHOULDER_MIN - eps) <= result.shoulder_angle_raw <= (config.ANGLE_ARM_SHOULDER_MAX + eps)
            elbow_ok = (config.ANGLE_ARM_ELBOW_MIN - eps) <= result.elbow_angle_raw <= (config.ANGLE_ARM_ELBOW_MAX + eps)
            if shoulder_ok and elbow_ok:
                servos.set_angle(config.CH_ARM_SHOULDER, result.shoulder_angle)
                servos.set_angle(config.CH_ARM_ELBOW, result.elbow_angle)
            else:
                # "Pared blanda" #2: el punto esta dentro del anillo por
                # distancia, pero el angulo de servo que hace falta para
                # llegar ahi NO entra en el rango real (ver nota 16-sept:
                # cerca del piso el codo pedia un angulo imposible). Si se
                # dejara clampear el angulo de salida nomas, el objetivo
                # interno (_ik_pos) seguiria alejandose de lo que el servo
                # puede cumplir de verdad, y el brazo "saltaba" al volver a
                # tocar el joystick. Se congela el objetivo en el ultimo
                # punto que si se pudo en vez de eso - no se manda nada
                # nuevo, el brazo se queda quieto ahi.
                _ik_pos["x"], _ik_pos["y"] = prev_x, prev_y


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
    if channel in (config.CH_ARM_SHOULDER, config.CH_ARM_ELBOW):
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
    import uvicorn

    uvicorn.run(app, host=config.WS_HOST, port=config.WS_PORT)
