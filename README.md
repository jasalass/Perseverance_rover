# Rover Percy

Rover teleoperado para el **Desafío de Innovación CITT-2026** (Duoc UC,
Escuela de Informática y Telecomunicaciones — Misión EREBUS-I,
exploración planetaria). Clasificatoria 8–11 sept 2026, final 25 sept
2026. Base mecánica adaptada de [Percy de BricoLabs](https://github.com/felixstdp/Perseverance)
(rocker-bogie 1:10), brazo del tutorial [HowToMechatronics](https://howtomechatronics.com/tutorials/arduino/diy-arduino-robot-arm-with-smartphone-control/),
cerebro y control propios.

## Qué es

Un rover de 6 ruedas (suspensión rocker-bogie) con dirección
independiente en las 4 esquinas y un brazo de 3 juntas con garra,
controlado en tiempo real desde el navegador de un celular por WiFi
local — sin apps nativas, sin internet en pista. Cámara, joysticks
táctiles y control por voz corren todos en el mismo dashboard.

## Arquitectura

```
Celular (navegador) --WebSocket /ws--> FastAPI (Orange Pi 3B)
                                          |-- I2C  --> PCA9685 --> 9 servos (brazo, garra, steering)
                                          '-- GPIO --> 3x TB6612FNG --> 6 motores N20 (tracción)
```

- **Cómputo:** Orange Pi 3B (Ubuntu 22.04), Python + FastAPI + WebSocket.
- **Brazo:** 3 servos MG996R (base, hombro, codo+inclinación fusionados)
  + 1 MG90S (garra), controlados por cinemática inversa — el joystick
  mueve la punta de la garra en línea recta, no articulación por
  articulación.
- **Dirección:** 4 servos SG90 en las esquinas, con 3 modos
  seleccionables: combinado, vehículo (Ackermann), y tanque (pivote de
  radio cero real, geometría en rombo calculada del chasis).
- **Tracción:** 6 motores N20 (1:150) vía 3 puentes H TB6612FNG — señal
  de dirección dedicada por placa, PWM de velocidad por 6 canales libres
  del PCA9685 (no por los 2 únicos PWM de hardware de la Orange Pi).
- **Alimentación:** rail de cómputo aislado (2×18650 → módulo 5V USB)
  separado del rail de actuadores (3×18650 → LM2596 → 6V, servos +
  motores) — con un interlock por software que corta la tracción
  mientras el brazo se está moviendo, por el presupuesto de corriente
  compartido.
- **Innovación:** control por voz 100% embebido en el navegador (Vosk
  WASM, sin nube, gramática cerrada para robustez al ruido de pista) y
  feedback háptico (vibración al agarrar/depositar/tocar un límite del
  brazo).

Detalle técnico completo — protocolo WebSocket, tabla de pines/cableado,
endpoints REST, estructura del código — en **[`orangepi/README.md`](orangepi/README.md)**.
Contexto del concurso, historial de decisiones de diseño y BOM completo
en **[`CLAUDE.md`](CLAUDE.md)**.

## Estructura del repo

```
orangepi/       backend real (FastAPI, servos, motores, dashboard) - ver su README
3dprint/        piezas STL/gcode del chasis, brazo y ruedas
docs/           bases del concurso, BOM y propósito del proyecto
CLAUDE.md       bitácora de decisiones de diseño e hilo de trabajo
```

## Estado

Chasis y ruedas impresos y calibrados. Brazo con cinemática inversa de 3
juntas funcionando. Dirección de las 4 esquinas calibrada. Motores de
tracción en proceso de cableado. Control por voz validado offline,
pendiente de HTTPS en la Orange Pi para probarlo en el celular real. Ver
la sección "Pendiente" de `orangepi/README.md` para el detalle exacto.
