"""
Posiciones guardadas ("presets") del brazo de Percy.

Un preset es solo el angulo final de cada uno de los 6 servos del brazo
(ver servos.ServoController.ARM_CHANNELS), guardado bajo un nombre
elegido por el operador (ej. "bajar garra", "posicion de inicio"). No se
graba el camino ni el tiempo que tomo moverse - al pedir un preset, el
brazo simplemente se mueve suave (rampa a velocidad fija) desde donde
este ahora hasta esos angulos finales.
"""
import asyncio
import json
import logging

log = logging.getLogger("percy.presets")

PRESETS_PATH = "presets.json"

# Velocidad de acercamiento a un preset. Mismo orden de magnitud que el
# tope de movimiento manual (config.ARM_STEP_DEG_PER_TICK * ARM_EASING_HZ
# = 100 grados/seg) pero un poco mas conservador, sin operador mirando el
# joystick en tiempo real para frenar si algo se traba.
GOTO_DEG_PER_S = 60.0
GOTO_HZ = 50


class PresetManager:
    def __init__(self, servos):
        self._servos = servos
        self._presets = self._load()
        self._moving = False

    def _load(self) -> dict:
        try:
            with open(PRESETS_PATH) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self):
        with open(PRESETS_PATH, "w") as f:
            json.dump(self._presets, f, ensure_ascii=False, indent=2)

    def list_names(self) -> list:
        return sorted(self._presets.keys())

    def save_current(self, name: str):
        name = name.strip()
        if not name:
            return
        self._presets[name] = self._servos.snapshot_arm_angles()
        self._save()
        log.info("Preset '%s' guardado: %s", name, self._presets[name])

    def delete(self, name: str):
        if name in self._presets:
            del self._presets[name]
            self._save()
            log.info("Preset '%s' borrado", name)

    @property
    def moving(self) -> bool:
        return self._moving

    async def goto(self, name: str):
        target = self._presets.get(name)
        if target is None or self._moving:
            return
        self._moving = True
        log.info("Moviendo a preset '%s': %s", name, target)
        try:
            tick_s = 1.0 / GOTO_HZ
            step_per_tick = GOTO_DEG_PER_S / GOTO_HZ
            while True:
                remaining = False
                for ch_str, target_angle in target.items():
                    ch = int(ch_str)
                    current = self._servos.get_angle(ch)
                    diff = target_angle - current
                    if abs(diff) <= step_per_tick:
                        if diff != 0:
                            self._servos.set_angle(ch, target_angle)
                    else:
                        remaining = True
                        self._servos.set_angle(ch, current + (step_per_tick if diff > 0 else -step_per_tick))
                if not remaining:
                    break
                await asyncio.sleep(tick_s)
        finally:
            self._moving = False
            log.info("Preset '%s' alcanzado", name)
