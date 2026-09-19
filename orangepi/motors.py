"""
Control de traccion diferencial: 6 motores N20 (3 izq + 3 der), 3x
TB6612FNG, 1 motor por canal.

Rediseño 18-sept: cada placa tiene su PROPIO par IN1/IN2 (libgpiod, 3
lineas espejadas por señal en vez de empalmar 1 cable en 3) y su PROPIO
canal PWM - pero el PWM ya no es el de hardware de la Pi (solo 2 reales
en todo el header), se movio a 6 canales libres del PCA9685 (ver
servos.set_motor_pwm) - por eso MotorController ahora necesita la
instancia de ServoController (comparten el mismo PCA9685/bus I2C, no
tiene sentido abrir una conexion aparte). Direccion sigue siendo GPIO
puro via libgpiod (python3-libgpiod, API v1) - eso no cambio.
"""
import logging

try:
    import gpiod
    _HARDWARE_LIBS_OK = True
except ImportError:
    # Windows/Mac/dev sin hardware real - modo simulado directo.
    _HARDWARE_LIBS_OK = False

import config

log = logging.getLogger("percy.motors")


class _GpioOut:
    """Una linea GPIO de salida via libgpiod v1."""

    _chips = {}  # cache de gpiod.Chip abiertos, uno por numero de chip

    def __init__(self, chip_num: int, line_num: int, consumer: str, default_val: int = 0):
        if chip_num not in self._chips:
            self._chips[chip_num] = gpiod.Chip(f"gpiochip{chip_num}")
        chip = self._chips[chip_num]
        self._line = chip.get_line(line_num)
        self._line.request(consumer=consumer, type=gpiod.LINE_REQ_DIR_OUT, default_val=default_val)

    def set(self, value: int):
        self._line.set_value(1 if value else 0)


class _MirroredGpioOut:
    """N lineas GPIO (una por placa TB6612FNG) que siempre reciben el mismo
    valor a la vez - reemplaza al empalme fisico de cable: en vez de 1 pin
    de la Pi repartido en 3 placas, son 3 pines distintos que el software
    escribe juntos."""

    def __init__(self, pins: list, consumer_prefix: str):
        self._lines = [
            _GpioOut(chip, line, consumer=f"{consumer_prefix}{i}")
            for i, (chip, line) in enumerate(pins)
        ]

    def set(self, value: int):
        for line in self._lines:
            line.set(value)


class MotorController:
    def __init__(self, servos):
        """servos: instancia ya creada de ServoController (main.py la crea
        primero) - se reusa su conexion al PCA9685 para el PWM de motor,
        ver servos.set_motor_pwm."""
        self._servos = servos
        self.simulated = False
        if not _HARDWARE_LIBS_OK:
            log.warning(
                "libgpiod no disponible en este sistema - modo SIMULADO: no "
                "se mueve ningun motor real. Normal en Windows/Mac para "
                "pruebas locales; en la Orange Pi esto no deberia pasar."
            )
            self.simulated = True
            self._enabled = False
            return
        try:
            self._left_in1 = _MirroredGpioOut(config.PINS_LEFT_IN1, "percy-left-in1-")
            self._left_in2 = _MirroredGpioOut(config.PINS_LEFT_IN2, "percy-left-in2-")
            self._right_in1 = _MirroredGpioOut(config.PINS_RIGHT_IN1, "percy-right-in1-")
            self._right_in2 = _MirroredGpioOut(config.PINS_RIGHT_IN2, "percy-right-in2-")
            self._stby = _GpioOut(*config.PIN_MOTOR_STBY, consumer="percy-stby", default_val=1)

            self._enabled = True
            self.stop()
        except (OSError, PermissionError) as exc:
            self.simulated = True
            self._enabled = False
            log.warning(
                "No se pudo inicializar GPIO de motores (%s) - modo "
                "SIMULADO: no se mueve ningun motor real.", exc,
            )

    def enable(self):
        if self.simulated:
            return
        self._stby.set(1)
        self._enabled = True

    def disable(self):
        """Corte duro por hardware - usado por el failsafe ademas de poner
        el PWM en 0, para no depender solo de que el driver respete PWM=0."""
        if self.simulated:
            return
        for ch in config.CH_MOTOR_PWM_L + config.CH_MOTOR_PWM_R:
            self._servos.set_motor_pwm(ch, 0.0)
        self._stby.set(0)
        self._enabled = False

    def _set_side(self, in1: _MirroredGpioOut, in2: _MirroredGpioOut, pwm_channels: list, value: float):
        if self.simulated:
            return
        value = max(-1.0, min(1.0, value))
        if value > 0:
            in1.set(1)
            in2.set(0)
        elif value < 0:
            in1.set(0)
            in2.set(1)
        else:
            in1.set(0)
            in2.set(0)
        for ch in pwm_channels:
            self._servos.set_motor_pwm(ch, abs(value))

    def set_motors(self, left: float, right: float, speed_level: int = 1):
        """left/right en -1.0..1.0 (ya calculados en el cliente con la
        formula de traccion diferencial), speed_level 0/1/2."""
        if self.simulated:
            return
        if not self._enabled:
            self.enable()
        max_duty = config.SPEED_LEVELS.get(speed_level, config.SPEED_LEVELS[1])
        self._set_side(self._left_in1, self._left_in2, config.CH_MOTOR_PWM_L, left * max_duty)
        self._set_side(self._right_in1, self._right_in2, config.CH_MOTOR_PWM_R, right * max_duty)

    def stop(self):
        if self.simulated:
            return
        self._set_side(self._left_in1, self._left_in2, config.CH_MOTOR_PWM_L, 0)
        self._set_side(self._right_in1, self._right_in2, config.CH_MOTOR_PWM_R, 0)

    def failsafe_stop(self):
        log.warning("FAILSAFE: cortando motores")
        self.disable()
