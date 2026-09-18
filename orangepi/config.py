"""
Configuracion central de Percy (Orange Pi 3B).

Todos los numeros de pin/canal de este archivo son el UNICO lugar que hay
que tocar si el cableado real termina siendo distinto. Los valores de aqui
salieron de:
  - `gpio readall` corrido en la propia Orange Pi (tabla real de pines).
  - El manual oficial Orange Pi 3B RK3566 User Manual v1.0, seccion 3.16.
  - Las decisiones de CLAUDE.md (mapeo de canales PCA9685, etc.)

Ver CLAUDE.md seccion "Pines / hardware - Orange Pi 3B" para el detalle
de por que se eligio cada pin.
"""

# ---------------------------------------------------------------------------
# I2C
# ---------------------------------------------------------------------------
# Bus del PCA9685: pines fisicos 3 (SDA) / 5 (SCL), overlay i2c2-m1.
I2C_BUS_SERVOS = 2

# Bus de sensores (ToF x3 + IMU): pines fisicos 27 (SDA) / 28 (SCL),
# overlay i2c3-m0. Separado del PCA9685 para no mezclar trafico.
I2C_BUS_SENSORS = 3

PCA9685_ADDRESS = 0x40

# ---------------------------------------------------------------------------
# Servos (PCA9685, 16 canales, se usan 10)
# ---------------------------------------------------------------------------
# Brazo HowToMechatronics de 6 servos (corregido 15-sept, ver CLAUDE.md -
# se habia contado mal, faltaba el giro de la garra como eje propio):
#   girar base + hombro + codo en MG996R, muneca + girador de garra +
#   apertura de garra en MG90.
CH_ARM_BASE = 0            # MG996R - girar brazo (base)
CH_ARM_SHOULDER = 1        # MG996R - hombro (subir/bajar brazo)
CH_ARM_ELBOW = 2           # MG996R - codo (extender/recoger alcance)
CH_ARM_WRIST = 3           # MG90 - muneca
CH_GRIPPER_ROTATE = 4      # MG90 - gira la garra sobre si misma
CH_GRIPPER = 5             # MG90 - abre/cierra la garra (pendiente de conectar)

CH_STEER_FL = 6      # SG90 - direccion rueda delantera izquierda
CH_STEER_FR = 7      # SG90 - direccion rueda delantera derecha
CH_STEER_RL = 8      # SG90 - direccion rueda trasera izquierda
CH_STEER_RR = 9      # SG90 - direccion rueda trasera derecha

# La bandeja abatible (CH_TRAY) se saco del diseno final (15-sept) - ya
# no existe fisicamente, no hay canal para ella.

# Rango de actuacion de cada servo (grados). Todos los MG996R/MG90/SG90
# usados aqui son de 0-180 grados tipicos; el ancho de pulso por defecto
# de adafruit_motor/ServoKit (750-2250us) funciona para la gran mayoria de
# clones. AJUSTAR si algun servo se ve entrecortado en los extremos.
SERVO_MIN_PULSE_US = 500
SERVO_MAX_PULSE_US = 2500
SERVO_ACTUATION_RANGE = 180

# Angulos de referencia - PROVISORIOS, calibrar con el brazo armado
# (igual que ya se advertia en la version ESP32 del firmware).
ANGLE_ARM_BASE_CENTER = 90
ANGLE_ARM_SHOULDER_REST = 90
ANGLE_ARM_SHOULDER_MIN = 10
ANGLE_ARM_SHOULDER_MAX = 175  # tope real probado 180 (pose "traslado") - se deja 175 de margen para el joystick
ANGLE_ARM_ELBOW_REST = 90
ANGLE_ARM_ELBOW_MIN = 10
ANGLE_ARM_ELBOW_MAX = 170
ANGLE_ARM_WRIST_CENTER = 176  # calibrado 16-sept: horn reubicado a mano para que quede paralela al piso aca
ANGLE_ARM_WRIST_MIN = 10   # sin calibrar el limite mecanico real todavia - margen generico
ANGLE_ARM_WRIST_MAX = 176  # tope real pedido 16-sept: no pasar del angulo paralelo al piso
ANGLE_GRIPPER_ROTATE_CENTER = 100  # calibrado 16-sept con la garra real
ANGLE_GRIPPER_ROTATE_MIN = 10   # idem wrist - sin calibrar el limite mecanico real todavia
ANGLE_GRIPPER_ROTATE_MAX = 170
ANGLE_GRIPPER_OPEN = 90   # calibrado 16-sept con la garra real
ANGLE_GRIPPER_CLOSED = 0  # calibrado 16-sept con la garra real

# El brazo ya NO se mueve directo desde el mensaje del WebSocket - hay un
# loop de "easing" aparte en main.py (_arm_easing_loop) que corre a
# ARM_EASING_HZ y rampea la velocidad actual hacia la velocidad pedida
# (el joystick/boton), en vez de saltar de golpe. Esto da aceleracion y
# frenado suaves en vez del movimiento "a tirones"/brusco de antes.
ARM_EASING_HZ = 50
# Cuanto puede cambiar la velocidad (-1.0..1.0) del brazo por tick del
# loop de easing. Con 0.08 a 50Hz, pasar de quieto a velocidad maxima
# tarda ~250ms - rampa perceptible pero no lenta. Mas chico = mas suave
# pero mas "flotante"; mas grande = mas directo pero menos suave.
ARM_MAX_ACCEL_PER_TICK = 0.08

# Grados por tick del loop de easing (a ARM_EASING_HZ=50) a velocidad
# maxima (-1.0/1.0). 2.0 grados/tick * 50 ticks/seg = 100 grados/seg tope,
# igual velocidad tope que antes, ahora repartida en pasos mas chicos y
# frecuentes por el easing.
ARM_STEP_DEG_PER_TICK = 2.0
BASE_STEP_DEG_PER_TICK = 1.2  # giro de base mas lento que el resto (mas inercia)
WRIST_STEP_DEG_PER_TICK = 2.0  # 16-sept: al maximo (igual que ARM_STEP_DEG_PER_TICK, 100 grados/seg) para descartar velocidad como causa del esfuerzo/tics

# Bajar la garra va a favor de la gravedad, que la acelera entre un tick y
# el siguiente -> se ve a tirones aunque el paso sea el mismo que al subir
# (donde el hombro lucha contra la gravedad y va parejo). Se reduce la
# velocidad cartesiana solo para ese sentido (ver main.py:_arm_easing_loop,
# rama IK). Ajustar si sigue a tirones al bajar.
SHOULDER_DOWN_STEP_SCALE = 0.12

# ---------------------------------------------------------------------------
# Cinematica inversa (kinematics.py) - CALIBRADO 16-sept con el brazo real
# (largos medidos + los 4 IK_*_SERVO_AT_ZERO/IK_*_SIGN de mas abajo,
# ajustados a mano viendo el brazo). El joystick derecho del dashboard
# mueve hombro/codo a traves de esta IK (ver main.py - _arm_easing_loop).
# ---------------------------------------------------------------------------
# Largo de los eslabones en mm. hombro->codo y codo->punta de la garra.
# Medido con el brazo real armado (16-sept), eje de giro a eje de giro /
# eje de giro a punta de la garra, linea recta.
IK_L1_MM = 120.0  # hombro -> codo
IK_L2_MM = 250.0  # codo -> punta de garra (con adaptador incluido)

# Calibracion servo<->matematica: que angulo de SERVO corresponde al
# "cero matematico" (brazo horizontal hacia adelante) de cada junta, y si
# aumentar el angulo matematico aumenta o disminuye el angulo de servo
# (+1 o -1, segun para que lado quedo montado el horn). SE CALIBRAN A MANO:
# 1. Mover el servo a IK_SHOULDER_SERVO_AT_ZERO grados.
# 2. Ver si el brazo quedo horizontal hacia adelante de verdad.
# 3. Si no, ajustar el numero hasta que sea asi.
# 4. Para el signo: mover el servo unos grados mas y ver si el brazo sube
#    o baja - si sube, IK_SHOULDER_SIGN=1 hace que theta1 positivo tambien
#    suba (coherente); si baja, cambiar a -1.
IK_SHOULDER_SERVO_AT_ZERO = 90.0
IK_SHOULDER_SIGN = 1
IK_ELBOW_SERVO_AT_ZERO = 115.0  # calibrado 16-sept con el brazo real
IK_ELBOW_SIGN = 1

# Velocidad de movimiento cartesiano (mm/s de la punta de la garra) al
# fondo del joystick derecho, ya con el nuevo control por IK (16-sept) -
# reemplaza el control directo de hombro/codo por articulacion. Mismo
# orden de magnitud que la velocidad angular vieja (100 grados/seg con
# brazos de 70mm ronda los ~120mm/s en el extremo, se deja mas
# conservador para no perder precision).
IK_CARTESIAN_SPEED_MM_S = 80.0

# Posicion fija sobre la bandeja para la macro "soltar en bandeja"
# (ver Especificacion de software en CLAUDE.md) - PROVISORIO.
DEPOSIT_ARM_BASE = 90
DEPOSIT_SHOULDER = 60
DEPOSIT_ELBOW = 130

# Steering: centro y giro maximo por rueda - PROVISORIO, calibrar en pista.
STEER_CENTER = {"fl": 90, "fr": 90, "rl": 90, "rr": 90}
STEER_MAX_DELTA = 35  # grados +/- desde el centro

# ---------------------------------------------------------------------------
# Motores de traccion (2x TB6612FNG por lado + 1 modulo mas por si acaso,
# 1 motor por canal, control compartido por lado - ver guia de cableado
# en CLAUDE.md). GPIO como (chip, linea) para libgpiod, calculados de la
# tabla real `gpio readall`: chip = gpio_global // 32, linea = gpio_global % 32.
# ---------------------------------------------------------------------------
# Lado izquierdo
PIN_LEFT_IN1 = (3, 22)   # fisico 11 - GPIO118 (GPIO3_C6)
PIN_LEFT_IN2 = (3, 23)   # fisico 12 - GPIO119 (GPIO3_C7)
PIN_LEFT_PWM_CHIP = 2    # pwmchip2 (fe700030.pwm) = PWM15, fisico 7
PIN_LEFT_PWM_LINE = 0

# Lado derecho
PIN_RIGHT_IN1 = (4, 0)   # fisico 13 - GPIO128 (GPIO4_A0)
PIN_RIGHT_IN2 = (4, 2)   # fisico 15 - GPIO130 (GPIO4_B... vecino, ver tabla)
PIN_RIGHT_PWM_CHIP = 1   # pwmchip1 (fe6f0030.pwm) = PWM11, fisico 32
PIN_RIGHT_PWM_LINE = 0

# Standby compartido por los 3 TB6612FNG - permite cortar motores por
# hardware ademas de por software (capa extra del failsafe).
PIN_MOTOR_STBY = (4, 3)  # fisico 16 - GPIO131 (GPIO4_A3)

PWM_FREQUENCY_HZ = 1000  # frecuencia tipica para TB6612FNG (100Hz-100kHz OK)

# Velocidad: multiplica el duty cycle maximo de traccion (0.0 - 1.0)
SPEED_LEVELS = {0: 0.45, 1: 0.65, 2: 1.0}  # lenta / media / rapida

# ---------------------------------------------------------------------------
# Sensores (ToF VL53L0X x3 + IMU MPU-9250) - bus I2C_BUS_SENSORS
# ---------------------------------------------------------------------------
# Los 3 VL53L0X comparten direccion de fabrica 0x29: se apagan de a uno via
# XSHUT y se les asigna una direccion nueva al arrancar. GPIO como (chip,linea).
PIN_TOF_XSHUT_FRONT = (4, 1)  # fisico 18 - GPIO129
PIN_TOF_XSHUT_LEFT = (4, 4)   # fisico 22 - GPIO132
PIN_TOF_XSHUT_RIGHT = (4, 6)  # fisico 24 - GPIO134

TOF_ADDRESS_FRONT = 0x30
TOF_ADDRESS_LEFT = 0x31
TOF_ADDRESS_RIGHT = 0x32
TOF_ADDRESS_DEFAULT = 0x29

IMU_ADDRESS = 0x68  # MPU-9250, direccion por defecto (AD0 a GND)

# ---------------------------------------------------------------------------
# Failsafe / red
# ---------------------------------------------------------------------------
FAILSAFE_TIMEOUT_S = 0.5  # sin mensajes por mas de esto -> detener todo

# Desactivado 16-sept para poder calibrar servos por script SSH sin que el
# failsafe interrumpa cada 500ms (no hay dashboard mandando mensajes
# continuos durante esas pruebas). *** VOLVER A PONER EN True ANTES DE
# CUALQUIER PRUEBA EN PISTA O DE LA COMPETENCIA *** - sin esto, un corte de
# WiFi en plena mision no frena nada.
FAILSAFE_ENABLED = False
WS_HOST = "0.0.0.0"
WS_PORT = 8000

# Indice de la camara para cv2.VideoCapture. En la Orange Pi con una sola
# camara USB, 0 alcanza. En un PC con camara integrada + la USB conectada
# (pruebas locales), puede haber mas de un indice - ajustar si toma la
# camara equivocada (0 = integrada del notebook, 1 = la USB externa, en
# la prueba local del 15-sept).
CAMERA_DEVICE_INDEX = 1  # 0 es el RGA interno del SoC, no una camara - la LifeCam quedo en /dev/video1
