"""
Control de los 10 servos del rover via PCA9685 (I2C bus 2, ver config.py).

Usa adafruit-pca9685 + adafruit-motor directo (no ServoKit) para poder fijar
el rango de pulso por canal - los MG996R/MG90/SG90 que tenemos no siempre
responden igual al rango por defecto de ServoKit.
"""
import logging
import time

try:
    from adafruit_extended_bus import ExtendedI2C
    from adafruit_pca9685 import PCA9685
    from adafruit_motor import servo
    import smbus2
    _HARDWARE_LIBS_OK = True
except ImportError:
    # Windows/Mac/dev sin hardware real - modo simulado directo, sin
    # instalar las librerias especificas de Linux (Blinka/PCA9685).
    _HARDWARE_LIBS_OK = False

import config

log = logging.getLogger("percy.servos")


class ServoController:
    def __init__(self):
        self._angles = {}  # ultimo angulo pedido por canal, para telemetria
        self._gripper_closed = False
        self._servos = {}
        self._pca = None
        self.simulated = False

        if not _HARDWARE_LIBS_OK:
            self.simulated = True
            log.warning(
                "Librerias de hardware (Blinka/PCA9685) no disponibles en este "
                "sistema - modo SIMULADO: los angulos se registran pero no se "
                "mueve ningun servo real. Normal en Windows/Mac para pruebas "
                "locales; en la Orange Pi esto no deberia pasar."
            )
            return

        # Reintentos al arrancar (17-sept): si percy.service arranca antes
        # de que el bus I2C/PCA9685 este listo (visto en la practica tras
        # un reboot de la Pi), la deteccion fallaba una sola vez y quedaba
        # en modo SIMULADO hasta el proximo reinicio MANUAL del servicio -
        # aunque el chip respondiera bien segundos despues. Mismo problema
        # que se le arreglo a camera.py, misma solucion.
        start_retries = 5
        start_retry_delay_s = 2.0
        last_exc = None
        for attempt in range(1, start_retries + 1):
            try:
                i2c = ExtendedI2C(config.I2C_BUS_SERVOS)
                self._pca = PCA9685(i2c, address=config.PCA9685_ADDRESS)
                self._pca.frequency = 50  # 50Hz estandar para servos analogicos
                for ch in range(16):
                    self._servos[ch] = servo.Servo(
                        self._pca.channels[ch],
                        min_pulse=config.SERVO_MIN_PULSE_US,
                        max_pulse=config.SERVO_MAX_PULSE_US,
                        actuation_range=config.SERVO_ACTUATION_RANGE,
                    )
                # NO se manda ninguna posicion inicial a proposito (16-sept):
                # el PCA9685 mantiene el ultimo PWM que tenia cada canal por
                # su cuenta, independiente de que el proceso Python se
                # reinicie - mandar "posiciones seguras" aca causaba que el
                # brazo se retorciera de golpe cada vez que arrancaba el
                # servicio, sin importar en que posicion real estuviera. Los
                # servos se quedan quietos donde ya estan hasta que llegue
                # un comando real del dashboard.
                last_exc = None
                break
            except (ValueError, OSError) as exc:
                last_exc = exc
                self._servos = {}
                log.warning(
                    "PCA9685 no responde en el bus (%s, intento %d/%d)",
                    exc, attempt, start_retries,
                )
                if attempt < start_retries:
                    time.sleep(start_retry_delay_s)
        if last_exc is not None:
            self.simulated = True
            log.warning(
                "PCA9685 no responde tras %d intentos (%s) - modo SIMULADO: "
                "los angulos se registran pero no se mueve ningun servo "
                "real. Conecta el PCA9685 y reinicia el proceso para operar "
                "de verdad.",
                start_retries, last_exc,
            )

    def set_angle(self, channel: int, angle: float):
        angle = max(0, min(config.SERVO_ACTUATION_RANGE, angle))
        self._angles[channel] = angle
        if not self.simulated:
            self._servos[channel].angle = angle

    # Registro base de LED0_ON_L en el PCA9685; cada canal ocupa 4 bytes
    # (ON_L, ON_H, OFF_L, OFF_H) a partir de ahi. Se lee crudo por smbus2 en
    # vez de usar adafruit_pca9685.duty_cycle porque esa propiedad tira
    # "TypeError: 'memoryview' object cannot be interpreted as an integer"
    # en esta combinacion de SO/version (ver notas de calibracion 16-sept).
    _PCA9685_LED0_ON_L = 0x06
    _PCA9685_PERIOD_US = 20000.0  # 1/50Hz - self._pca.frequency siempre es 50

    def _read_hardware_angle(self, channel: int):
        """Lee del PCA9685 el angulo que el canal tiene AHORA en hardware,
        sin pasar por self._angles - para cuando el software todavia no le
        mando nada a ese canal en esta corrida (ver bug 16-sept: get_angle
        devolvia 90 "a ciegas" para canales no tocados, lo que hacia que
        grabar/reproducir presets guardara/comparara contra un valor
        inventado en vez de la posicion real del servo). Devuelve None si
        no se pudo leer."""
        try:
            bus = smbus2.SMBus(config.I2C_BUS_SERVOS)
            try:
                reg = self._PCA9685_LED0_ON_L + channel * 4
                on_l = bus.read_byte_data(config.PCA9685_ADDRESS, reg)
                on_h = bus.read_byte_data(config.PCA9685_ADDRESS, reg + 1)
                off_l = bus.read_byte_data(config.PCA9685_ADDRESS, reg + 2)
                off_h = bus.read_byte_data(config.PCA9685_ADDRESS, reg + 3)
            finally:
                bus.close()
        except Exception as exc:
            log.warning("No se pudo leer el canal %d del PCA9685 (%s)", channel, exc)
            return None
        if off_h & 0x10:  # bit "full off" - canal sin senal
            return None
        on_count = ((on_h & 0x0F) << 8) | on_l
        off_count = ((off_h & 0x0F) << 8) | off_l
        pulse_us = ((off_count - on_count) % 4096) / 4096.0 * self._PCA9685_PERIOD_US
        angle = (pulse_us - config.SERVO_MIN_PULSE_US) / (config.SERVO_MAX_PULSE_US - config.SERVO_MIN_PULSE_US) * config.SERVO_ACTUATION_RANGE
        return max(0, min(config.SERVO_ACTUATION_RANGE, angle))

    def set_motor_pwm(self, channel: int, fraction: float):
        """Duty cycle CRUDO (0.0-1.0, no angulo) para un canal del PCA9685
        usado como PWM de velocidad de un TB6612FNG (18-sept: los 6 PWM de
        motor se movieron aca porque la Pi solo tiene 2 canales de PWM de
        hardware reales, ver config.py). Se escribe el registro directo por
        el mismo motivo que _read_hardware_angle: la propiedad
        adafruit_pca9685.duty_cycle tira 'TypeError: memoryview...' en esta
        combinacion de SO/version - no se puede usar la API normal.
        Frecuencia = self._pca.frequency (50Hz, fija para los 16 canales a
        la vez, ya la usan los servos) - mas baja de lo ideal para un motor
        DC (1-20kHz es lo tipico), puede zumbar/vibrar un poco a duty bajo,
        pero mueve el motor sin hardware extra."""
        fraction = max(0.0, min(1.0, fraction))
        if self.simulated:
            return
        off_count = 0 if fraction <= 0.0 else max(1, min(4095, round(fraction * 4095)))
        reg = self._PCA9685_LED0_ON_L + channel * 4
        try:
            bus = smbus2.SMBus(config.I2C_BUS_SERVOS)
            try:
                bus.write_byte_data(config.PCA9685_ADDRESS, reg, 0)      # ON_L = 0
                bus.write_byte_data(config.PCA9685_ADDRESS, reg + 1, 0)  # ON_H = 0
                bus.write_byte_data(config.PCA9685_ADDRESS, reg + 2, off_count & 0xFF)
                off_h = (off_count >> 8) & 0x0F
                if fraction <= 0.0:
                    off_h |= 0x10  # bit "full off" (igual bit que lee _read_hardware_angle)
                bus.write_byte_data(config.PCA9685_ADDRESS, reg + 3, off_h)
            finally:
                bus.close()
        except Exception as exc:
            log.warning("No se pudo escribir PWM crudo en canal %d del PCA9685 (%s)", channel, exc)

    def get_angle(self, channel: int) -> float:
        if channel in self._angles:
            return self._angles[channel]
        if not self.simulated:
            angle = self._read_hardware_angle(channel)
            if angle is not None:
                self._angles[channel] = angle  # cachear - no releer cada vez
                return angle
        return 90

    def get_all_angles(self) -> dict:
        """Angulo actual (segun lo ultimo mandado por software) de cada
        canal nombrado - util para leer el estado en vivo sin adivinar,
        ej. por SSH via el endpoint /servos de main.py."""
        names = {
            config.CH_ARM_BASE: "base",
            config.CH_ARM_SHOULDER: "hombro",
            config.CH_ARM_ELBOW: "codo",
            config.CH_ARM_WRIST: "inclinacion_garra",
            config.CH_GRIPPER_ROTATE: "giro_garra",
            config.CH_GRIPPER: "gripper",
            config.CH_STEER_FL: "steer_fl",
            config.CH_STEER_FR: "steer_fr",
            config.CH_STEER_RL: "steer_rl",
            config.CH_STEER_RR: "steer_rr",
        }
        return {name: self.get_angle(ch) for ch, name in names.items()}

    # Canales del brazo (sin steering) - lo que graba/reproduce presets.py.
    ARM_CHANNELS = (
        config.CH_ARM_BASE, config.CH_ARM_SHOULDER, config.CH_ARM_ELBOW,
        config.CH_ARM_WRIST, config.CH_GRIPPER_ROTATE, config.CH_GRIPPER,
    )

    def snapshot_arm_angles(self) -> dict:
        """{canal: angulo} de los 6 servos del brazo tal como estan ahora -
        usado por presets.py para guardar una posicion."""
        return {ch: self.get_angle(ch) for ch in self.ARM_CHANNELS}

    # --- API de alto nivel usada por main.py -------------------------------

    def move_arm(self, base_dir: float, gripper_rotate_dir: float = 0.0):
        """-1.0..1.0 (base y giro de garra, ambos independientes de la IK),
        velocidad ya rampeada por main.py:_arm_easing_loop,
        no el valor crudo del joystick/boton - movimiento suave, sin salto
        brusco.

        Hombro, codo Y la inclinacion de garra (ex-muneca) NO se mueven
        desde aca (18-sept): las 3 pasaron a control por cinematica
        inversa acoplada por defecto (ver kinematics.solve3() +
        main.py:_arm_easing_loop) - mover la inclinacion sola sin
        recalcular hombro/codo haria que la punta de la garra se corra de
        lugar en vez de solo rotar en el sitio (el brazo 2+3 fusionado ya
        no tiene un eje de giro propio como antes, es un eslabon mas).
        Para el modo libre/directo (switch IK/LIBRE del dashboard) ver
        move_arm_joint_direct() mas abajo."""
        if base_dir:
            self.set_angle(config.CH_ARM_BASE, self.get_angle(config.CH_ARM_BASE) + base_dir * config.BASE_STEP_DEG_PER_TICK)
        if gripper_rotate_dir:
            new = self.get_angle(config.CH_GRIPPER_ROTATE) + gripper_rotate_dir * config.ARM_STEP_DEG_PER_TICK
            new = max(config.ANGLE_GRIPPER_ROTATE_MIN, min(config.ANGLE_GRIPPER_ROTATE_MAX, new))
            self.set_angle(config.CH_GRIPPER_ROTATE, new)

    def move_arm_joint_direct(self, shoulder_dir: float, elbow_dir: float, tilt_dir: float, step: float = config.ARM_STEP_DEG_PER_TICK) -> bool:
        """Modo 'LIBRE': hombro, codo Y la inclinacion de garra se mueven
        cada uno por su cuenta, sin coordinacion - util para calibrar o
        para posiciones que la IK no puede alcanzar (ver notas de
        config.py sobre la zona muerta del anillo IK_L1_MM/IK_L2_MM/
        IK_L3_MM). Se activa con el switch IK/LIBRE del dashboard (ver
        main.py, _arm_mode). Devuelve True si alguno de los tres quedo
        pegado a su limite (para el feedback haptico - ver main.py,
        _arm_hit_limit)."""
        hit_limit = False
        if shoulder_dir:
            shoulder_step = step * (config.SHOULDER_DOWN_STEP_SCALE if shoulder_dir < 0 else 1.0)
            new = self.get_angle(config.CH_ARM_SHOULDER) + shoulder_dir * shoulder_step
            clamped = max(config.ANGLE_ARM_SHOULDER_MIN, min(config.ANGLE_ARM_SHOULDER_MAX, new))
            if clamped != new:
                hit_limit = True
            self.set_angle(config.CH_ARM_SHOULDER, clamped)
        if elbow_dir:
            new = self.get_angle(config.CH_ARM_ELBOW) + elbow_dir * step
            clamped = max(config.ANGLE_ARM_ELBOW_MIN, min(config.ANGLE_ARM_ELBOW_MAX, new))
            if clamped != new:
                hit_limit = True
            self.set_angle(config.CH_ARM_ELBOW, clamped)
        if tilt_dir:
            new = self.get_angle(config.CH_ARM_WRIST) + tilt_dir * step
            clamped = max(config.ANGLE_ARM_TILT_MIN, min(config.ANGLE_ARM_TILT_MAX, new))
            if clamped != new:
                hit_limit = True
            self.set_angle(config.CH_ARM_WRIST, clamped)
        return hit_limit

    def set_gripper(self, closed: bool):
        self._gripper_closed = closed
        angle = config.ANGLE_GRIPPER_CLOSED if closed else config.ANGLE_GRIPPER_OPEN
        self.set_angle(config.CH_GRIPPER, angle)

    def toggle_gripper(self):
        self.set_gripper(not self._gripper_closed)

    def set_steering(self, lx: float):
        """lx en -1..1 -> angulo proporcional de las 4 ruedas de esquina
        (modo vehiculo/combinado - gira en curva). Las traseras giran al
        REVES que las delanteras (24-sept: antes iban las 4 al mismo lado y
        el rover se desplazaba de costado en vez de girar; los 4 servos
        estan montados con la misma orientacion)."""
        delta = max(-1.0, min(1.0, lx)) * config.STEER_MAX_DELTA
        self.set_angle(config.CH_STEER_FL, config.STEER_CENTER["fl"] + delta)
        self.set_angle(config.CH_STEER_FR, config.STEER_CENTER["fr"] + delta)
        self.set_angle(config.CH_STEER_RL, config.STEER_CENTER["rl"] - delta)
        self.set_angle(config.CH_STEER_RR, config.STEER_CENTER["rr"] - delta)

    def set_pivot_steering(self, lx: float):
        """lx en -1..1 -> angulo proporcional EN DIAGONAL (patron de rombo,
        cada rueda apuntando hacia el centro del rover) - para el modo
        tanque de verdad, pivote sin arrastre lateral (18-sept). FL/RR
        giran para un lado, FR/RL para el otro - geometria confirmada con
        el diagrama de montaje original y las medidas reales del chasis
        (ver config.PIVOT_STEER_DELTA). Con lx=0 las ruedas quedan derechas
        (avanzar/retroceder normal); el angulo crece a medida que se
        empuja el joystick para el costado, hasta el maximo del pivote.

        El rombo es el MISMO para girar a la izquierda o a la derecha (el
        sentido lo pone el diferencial de los motores) - por eso se usa el
        valor absoluto de lx (24-sept: con el signo, al girar para un lado
        las ruedas formaban el rombo invertido y se oponian al giro)."""
        delta = min(1.0, abs(lx)) * config.PIVOT_STEER_DELTA
        self.set_angle(config.CH_STEER_FL, config.STEER_CENTER["fl"] + delta)
        self.set_angle(config.CH_STEER_FR, config.STEER_CENTER["fr"] - delta)
        self.set_angle(config.CH_STEER_RL, config.STEER_CENTER["rl"] - delta)
        self.set_angle(config.CH_STEER_RR, config.STEER_CENTER["rr"] + delta)

    def deposit_macro(self):
        """Secuencia 'soltar en bandeja': mueve el brazo sobre la bandeja,
        abre la garra, y vuelve a la posicion de espera. Bloqueante y simple
        a proposito - si hace falta que sea no bloqueante se puede mover a
        un generador/estado en main.py mas adelante."""
        log.info("Ejecutando macro deposit")
        self.set_angle(config.CH_ARM_BASE, config.DEPOSIT_ARM_BASE)
        self.set_angle(config.CH_ARM_SHOULDER, config.DEPOSIT_SHOULDER)
        self.set_angle(config.CH_ARM_ELBOW, config.DEPOSIT_ELBOW)
        self.set_gripper(False)
        self.set_angle(config.CH_ARM_SHOULDER, config.ANGLE_ARM_SHOULDER_REST)
        self.set_angle(config.CH_ARM_ELBOW, config.ANGLE_ARM_ELBOW_REST)
        self.set_angle(config.CH_ARM_BASE, config.ANGLE_ARM_BASE_CENTER)

    def failsafe_stop(self):
        """No hay 'parar' para un servo (mantiene su ultima posicion), pero
        dejamos el brazo en una postura de reposo segura."""
        self.set_angle(config.CH_ARM_SHOULDER, config.ANGLE_ARM_SHOULDER_REST)
        self.set_angle(config.CH_ARM_ELBOW, config.ANGLE_ARM_ELBOW_REST)
