"""
Cinematica inversa (IK) para hombro+codo - brazo planar de 2 eslabones.

Dado un punto objetivo (x, y) en el plano vertical del brazo (x = distancia
horizontal desde el eje del hombro, y = altura sobre el eje del hombro,
ambos en mm), calcula los angulos de servo de hombro y codo para que la
punta de la garra llegue ahi. Matematica estandar de brazo de 2 eslabones
(ley de cosenos) - la misma idea que ya estaba en
`3dprint/scad/arm_gripper_assembly.scad` (no disponible en el working tree
ahora mismo, se reimplemento desde cero con la formula clasica).

NO USAR SIN CALIBRAR PRIMERO - ver las constantes de config.py mas abajo.
"""
import math
from dataclasses import dataclass
from typing import Optional

import config


@dataclass
class IKResult:
    shoulder_angle: float
    elbow_angle: float
    reachable: bool
    # Angulos SIN recortar a 0-180 - sirven para saber si el punto pedido
    # de verdad se puede alcanzar con un servo real (ver `achievable`) en
    # vez de confiar en shoulder_angle/elbow_angle, que ya vienen
    # recortados y pueden esconder que el resultado real no corresponde a
    # ese punto (main.py los usa para no seguir empujando un objetivo que
    # el servo ya no puede seguir - ver _arm_easing_loop).
    shoulder_angle_raw: float
    elbow_angle_raw: float

    @property
    def achievable(self) -> bool:
        """True si el punto es alcanzable Y ademas los dos angulos de
        servo necesarios entran de verdad en 0-180 (sin recortar) - a
        diferencia de `reachable`, que solo mira la distancia al hombro y
        puede dar True aunque el angulo de servo que hace falta sea
        fisicamente imposible (ver nota 16-sept sobre el "codo -28")."""
        return self.reachable and 0.0 <= self.shoulder_angle_raw <= 180.0 and 0.0 <= self.elbow_angle_raw <= 180.0


def solve(x_mm: float, y_mm: float, elbow_up: bool = True) -> IKResult:
    """x_mm: distancia horizontal desde el eje del hombro (positivo = hacia
    adelante). y_mm: altura sobre el eje del hombro (positivo = arriba).
    elbow_up: True/False elige entre las 2 soluciones geometricas posibles
    (codo hacia arriba o hacia abajo) - ambas llegan al mismo punto.

    Devuelve angulos de SERVO (0-180, listos para servos.set_angle), ya
    aplicando los offsets/signos de calibracion de config.py. Si el punto
    no es alcanzable, IKResult.reachable = False y los angulos son los mas
    cercanos posibles (clampeados), no confiar en ellos para mover el brazo."""
    l1 = config.IK_L1_MM
    l2 = config.IK_L2_MM

    d = math.hypot(x_mm, y_mm)
    reachable = abs(l1 - l2) <= d <= (l1 + l2)
    d_clamped = max(abs(l1 - l2) + 0.01, min(l1 + l2 - 0.01, d))

    # Ley de cosenos: angulo interior del codo
    cos_elbow = (l1 * l1 + l2 * l2 - d_clamped * d_clamped) / (2 * l1 * l2)
    cos_elbow = max(-1.0, min(1.0, cos_elbow))
    elbow_interior = math.acos(cos_elbow)  # 0 = brazo totalmente doblado, pi = totalmente extendido

    angle_to_target = math.atan2(y_mm, x_mm)
    cos_offset = (l1 * l1 + d_clamped * d_clamped - l2 * l2) / (2 * l1 * d_clamped)
    cos_offset = max(-1.0, min(1.0, cos_offset))
    shoulder_offset = math.acos(cos_offset)

    if elbow_up:
        theta1 = angle_to_target + shoulder_offset
        theta2 = elbow_interior - math.pi  # negativo = codo se dobla "hacia arriba"
    else:
        theta1 = angle_to_target - shoulder_offset
        theta2 = math.pi - elbow_interior

    # --- Conversion de angulos matematicos a angulos de SERVO -----------
    # theta1/theta2 estan en radianes, en el sistema matematico donde 0 =
    # horizontal hacia adelante. Los servos reales tienen su propio cero
    # segun como quedo montado el horn - estas 4 constantes son las que
    # hay que calibrar a mano con el brazo armado (ver config.py).
    shoulder_raw = config.IK_SHOULDER_SERVO_AT_ZERO + config.IK_SHOULDER_SIGN * math.degrees(theta1)
    elbow_raw = config.IK_ELBOW_SERVO_AT_ZERO + config.IK_ELBOW_SIGN * math.degrees(theta2)

    shoulder_servo = max(0.0, min(180.0, shoulder_raw))
    elbow_servo = max(0.0, min(180.0, elbow_raw))

    return IKResult(
        shoulder_angle=shoulder_servo, elbow_angle=elbow_servo, reachable=reachable,
        shoulder_angle_raw=shoulder_raw, elbow_angle_raw=elbow_raw,
    )


def forward_from_servo(shoulder_servo: float, elbow_servo: float) -> tuple:
    """Inversa de la calibracion de solve(): dado el angulo de SERVO actual
    de hombro y codo, devuelve el punto (x_mm, y_mm) al que corresponde.
    Se usa para inicializar/resincronizar el objetivo cartesiano del
    control por IK con la posicion real del brazo (ver main.py,
    _ik_pos_dirty) - sin esto, el control por IK arrancaria asumiendo que
    la garra esta en un punto inventado en vez de donde esta de verdad."""
    l1 = config.IK_L1_MM
    l2 = config.IK_L2_MM
    theta1 = math.radians((shoulder_servo - config.IK_SHOULDER_SERVO_AT_ZERO) / config.IK_SHOULDER_SIGN)
    theta2 = math.radians((elbow_servo - config.IK_ELBOW_SERVO_AT_ZERO) / config.IK_ELBOW_SIGN)
    x = l1 * math.cos(theta1) + l2 * math.cos(theta1 + theta2)
    y = l1 * math.sin(theta1) + l2 * math.sin(theta1 + theta2)
    return x, y


def infer_elbow_up(elbow_servo: float) -> bool:
    """A que rama (elbow_up/elbow_down, ver solve()) corresponde un angulo
    de SERVO de codo ya montado. Se usa al resincronizar el control por IK
    con la posicion real del brazo (main.py, _ik_pos_dirty) para seguir
    pidiendo soluciones de la MISMA rama - si no, solve() podria devolver
    la solucion geometrica del otro lado del codo para el mismo punto (x,y)
    y el brazo saltaria de golpe al tocar el joystick por primera vez."""
    theta2 = (elbow_servo - config.IK_ELBOW_SERVO_AT_ZERO) / config.IK_ELBOW_SIGN
    return theta2 <= 0


@dataclass
class IKResult3:
    """Como IKResult, pero para el brazo de 3 juntas (hombro+codo+
    inclinacion de garra) desde el rediseño 18-sept - ver solve3()."""
    shoulder_angle: float
    elbow_angle: float
    tilt_angle: float
    reachable: bool
    shoulder_angle_raw: float
    elbow_angle_raw: float
    tilt_angle_raw: float

    @property
    def achievable(self) -> bool:
        return (
            self.reachable
            and 0.0 <= self.shoulder_angle_raw <= 180.0
            and 0.0 <= self.elbow_angle_raw <= 180.0
            and 0.0 <= self.tilt_angle_raw <= 180.0
        )


def solve3(x_mm: float, y_mm: float, phi_deg: float, elbow_up: bool = True) -> IKResult3:
    """Brazo de 3 juntas en el mismo plano (hombro, codo, inclinacion de
    garra - rediseño 18-sept, la ex-muneca ya no gira sobre si misma, es
    un eslabon mas). x_mm/y_mm: punto objetivo de la PUNTA de la garra
    (misma convencion que solve()). phi_deg: angulo de acercamiento
    deseado, en grados y en la misma convencion matematica que
    theta1/theta2 (0 = horizontal hacia adelante) - no solo A DONDE llega
    la garra, tambien CON QUE ANGULO.

    El truco: se le resta al punto objetivo el ultimo eslabon (L3) en la
    direccion de phi, para hallar donde tendria que estar la junta de
    inclinacion - ese punto intermedio se le pasa TAL CUAL a la misma
    solve() de 2 eslabones de siempre (hombro/codo no se enteran de que
    ahora hay una tercera junta). La junta de inclinacion es literalmente
    "lo que falta" para que theta1+theta2+theta3 sume phi."""
    phi_rad = math.radians(phi_deg)
    l3 = config.IK_L3_MM
    wrist_x = x_mm - l3 * math.cos(phi_rad)
    wrist_y = y_mm - l3 * math.sin(phi_rad)

    arm2 = solve(wrist_x, wrist_y, elbow_up=elbow_up)

    theta1 = math.radians((arm2.shoulder_angle_raw - config.IK_SHOULDER_SERVO_AT_ZERO) / config.IK_SHOULDER_SIGN)
    theta2 = math.radians((arm2.elbow_angle_raw - config.IK_ELBOW_SERVO_AT_ZERO) / config.IK_ELBOW_SIGN)
    theta3 = phi_rad - theta1 - theta2

    tilt_raw = config.IK_TILT_SERVO_AT_ZERO + config.IK_TILT_SIGN * math.degrees(theta3)
    tilt_servo = max(0.0, min(180.0, tilt_raw))

    return IKResult3(
        shoulder_angle=arm2.shoulder_angle, elbow_angle=arm2.elbow_angle, tilt_angle=tilt_servo,
        reachable=arm2.reachable,
        shoulder_angle_raw=arm2.shoulder_angle_raw, elbow_angle_raw=arm2.elbow_angle_raw, tilt_angle_raw=tilt_raw,
    )


def current_phi(shoulder_servo: float, elbow_servo: float, tilt_servo: float) -> float:
    """Angulo de acercamiento ACTUAL de la garra (grados, misma convencion
    que solve3/phi_deg) a partir de los 3 angulos de servo reales - para
    resincronizar el control por IK con la pose real del brazo (main.py,
    _ik_pos_dirty), misma idea que forward_from_servo() pero sumando el
    aporte de la tercera junta."""
    theta1 = math.radians((shoulder_servo - config.IK_SHOULDER_SERVO_AT_ZERO) / config.IK_SHOULDER_SIGN)
    theta2 = math.radians((elbow_servo - config.IK_ELBOW_SERVO_AT_ZERO) / config.IK_ELBOW_SIGN)
    theta3 = math.radians((tilt_servo - config.IK_TILT_SERVO_AT_ZERO) / config.IK_TILT_SIGN)
    return math.degrees(theta1 + theta2 + theta3)
