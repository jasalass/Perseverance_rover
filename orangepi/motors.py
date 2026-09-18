"""
Control de traccion diferencial: 6 motores N20 (3 izq + 3 der en paralelo
por cableado, no por driver - ver guia de conexion en CLAUDE.md), 3x
TB6612FNG, 1 motor por canal.

Direccion (IN1/IN2) via libgpiod (python3-libgpiod, API v1). Velocidad via
PWM real de hardware por sysfs (/sys/class/pwm/...), porque Adafruit Blinka
no tiene pwmio confirmado para esta placa - sysfs es lo que el propio
manual de Orange Pi usa y ya confirmamos que funciona.
"""
import logging
import subprocess
import time

try:
    import gpiod
    _HARDWARE_LIBS_OK = True
except ImportError:
    # Windows/Mac/dev sin hardware real - modo simulado directo.
    _HARDWARE_LIBS_OK = False

import config

log = logging.getLogger("percy.motors")

PWM_SYSFS_BASE = "/sys/class/pwm/pwmchip{chip}"


class _SysfsPWM:
    """Un canal de PWM real via sysfs (pwmchipN/pwm0)."""

    def __init__(self, chip: int, line: int, frequency_hz: int):
        self._base = f"{PWM_SYSFS_BASE.format(chip=chip)}/pwm{line}"
        self._period_ns = int(1e9 / frequency_hz)
        chip_path = PWM_SYSFS_BASE.format(chip=chip)

        if not self._exists(self._base):
            with open(f"{chip_path}/export", "w") as f:
                f.write(str(line))
            time.sleep(0.1)  # el kernel tarda un poco en crear los nodos
            # El export de PWM por sysfs no dispara un evento udev real
            # (limitacion conocida de esta interfaz) - el udev rule normal
            # no alcanza a los archivos period/duty_cycle/enable recien
            # creados, asi que los des-bloqueamos con un helper de sudo
            # bien acotado (ver /etc/sudoers.d/percy-pwm).
            subprocess.run(
                ["sudo", "-n", "/usr/local/bin/percy-fix-pwm-perms.sh", self._base],
                check=False,
            )

        self._write("period", self._period_ns)
        self._write("duty_cycle", 0)
        self._write("enable", 1)

    @staticmethod
    def _exists(path: str) -> bool:
        import os
        return os.path.isdir(path)

    def _write(self, prop: str, value):
        with open(f"{self._base}/{prop}", "w") as f:
            f.write(str(value))

    def set_duty(self, fraction: float):
        """fraction 0.0-1.0"""
        fraction = max(0.0, min(1.0, fraction))
        duty_ns = int(self._period_ns * fraction)
        self._write("duty_cycle", duty_ns)

    def off(self):
        self._write("duty_cycle", 0)


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


class MotorController:
    def __init__(self):
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
            self._left_in1 = _GpioOut(*config.PIN_LEFT_IN1, consumer="percy-left-in1")
            self._left_in2 = _GpioOut(*config.PIN_LEFT_IN2, consumer="percy-left-in2")
            self._right_in1 = _GpioOut(*config.PIN_RIGHT_IN1, consumer="percy-right-in1")
            self._right_in2 = _GpioOut(*config.PIN_RIGHT_IN2, consumer="percy-right-in2")
            self._stby = _GpioOut(*config.PIN_MOTOR_STBY, consumer="percy-stby", default_val=1)

            self._left_pwm = _SysfsPWM(config.PIN_LEFT_PWM_CHIP, config.PIN_LEFT_PWM_LINE, config.PWM_FREQUENCY_HZ)
            self._right_pwm = _SysfsPWM(config.PIN_RIGHT_PWM_CHIP, config.PIN_RIGHT_PWM_LINE, config.PWM_FREQUENCY_HZ)

            self._enabled = True
            self.stop()
        except (OSError, PermissionError) as exc:
            self.simulated = True
            self._enabled = False
            log.warning(
                "No se pudo inicializar GPIO/PWM de motores (%s) - modo "
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
        self._left_pwm.off()
        self._right_pwm.off()
        self._stby.set(0)
        self._enabled = False

    def _set_side(self, in1: _GpioOut, in2: _GpioOut, pwm: _SysfsPWM, value: float):
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
        pwm.set_duty(abs(value))

    def set_motors(self, left: float, right: float, speed_level: int = 1):
        """left/right en -1.0..1.0 (ya calculados en el cliente con la
        formula de traccion diferencial), speed_level 0/1/2."""
        if self.simulated:
            return
        if not self._enabled:
            self.enable()
        max_duty = config.SPEED_LEVELS.get(speed_level, config.SPEED_LEVELS[1])
        self._set_side(self._left_in1, self._left_in2, self._left_pwm, left * max_duty)
        self._set_side(self._right_in1, self._right_in2, self._right_pwm, right * max_duty)

    def stop(self):
        if self.simulated:
            return
        self._set_side(self._left_in1, self._left_in2, self._left_pwm, 0)
        self._set_side(self._right_in1, self._right_in2, self._right_pwm, 0)

    def failsafe_stop(self):
        log.warning("FAILSAFE: cortando motores")
        self.disable()
