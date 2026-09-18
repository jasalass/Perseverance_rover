"""
Stream MJPEG de la camara USB para el dashboard.

El indice de /dev/videoN de la camara NO es estable entre reinicios de la
Orange Pi - el orden de enumeracion USB varia y a veces la UVC cae en
video1, otras en video3, etc. (visto en la practica el 15-sept: goteo de
"video_available: false" tras un reboot con un indice fijo). Por eso este
modulo busca la camara por NOMBRE de driver (/sys/class/video4linux/*/name)
en vez de por numero fijo - `config.CAMERA_DEVICE_INDEX` queda solo como
respaldo si la busqueda por nombre no encuentra nada (o en Windows, donde
no existe /sys/class/video4linux).
"""
import glob
import logging
import platform
import threading
import time

import cv2

import config

log = logging.getLogger("percy.camera")

# Dispositivos internos del SoC, no una camara real - se excluyen de la
# busqueda automatica. OJO: /sys/class/video4linux/videoN/name contiene el
# nombre de TARJETA v4l2 ("rockchip,rk3568-vpu-dec"), no el de driver
# ("hantro-vpu") - hay que filtrar por el primero, no el segundo (bug real
# detectado el 15-sept: el filtro con "hantro-vpu" no excluia nada y la
# busqueda devolvia el decoder de video en vez de la camara).
_INTERNAL_DRIVERS = ("rockchip-rga", "rk3568-vpu", "rk3568-vepu")


def _find_camera_index() -> int:
    """Busca en /sys/class/video4linux/videoN/name el primer video device
    que no sea uno de los internos del SoC. Devuelve el numero N, o
    config.CAMERA_DEVICE_INDEX si no encuentra nada (Windows, o ningun
    dispositivo externo conectado)."""
    for name_path in sorted(glob.glob("/sys/class/video4linux/video*/name")):
        try:
            with open(name_path) as f:
                driver_name = f.read().strip()
        except OSError:
            continue
        if any(internal in driver_name for internal in _INTERNAL_DRIVERS):
            continue
        # video3 y video4 suelen ser el mismo dispositivo UVC (captura +
        # metadata) - nos quedamos con el primero que aparezca.
        idx = int(name_path.split("video")[-1].split("/")[0])
        log.info("Camara detectada por nombre: '%s' en /dev/video%d", driver_name, idx)
        return idx
    log.warning(
        "No se detecto ninguna camara externa por nombre - usando "
        "config.CAMERA_DEVICE_INDEX=%d de respaldo", config.CAMERA_DEVICE_INDEX
    )
    return config.CAMERA_DEVICE_INDEX


class CameraStream:
    def __init__(self, device_index: int = None, width: int = 640, height: int = 480, fps: int = 20):
        # device_index=None (por defecto real, ver main.py) -> autodetectar
        # por nombre en vez de confiar en un numero fijo.
        self._device_index = device_index
        self._width = width
        self._height = height
        self._fps = fps
        self._cap = None
        self._frame = None
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._available = False

    # Reintentos al arrancar (17-sept): si percy.service arranca antes de
    # que USB termine de enumerar la camara, la deteccion/apertura fallaba
    # una sola vez y quedaba sin video hasta el proximo reinicio MANUAL del
    # servicio - un reboot de la Pi no se recuperaba solo. Con este retry
    # alcanza esperar unos segundos en vez de tener que reiniciar a mano.
    _START_RETRIES = 5
    _START_RETRY_DELAY_S = 2.0

    def start(self):
        for attempt in range(1, self._START_RETRIES + 1):
            if self._device_index is None or attempt > 1:
                # Redetectar por nombre en cada intento - si en el primer
                # intento todavia no habia enumerado, el indice tambien
                # puede haber cambiado para cuando si aparezca.
                self._device_index = _find_camera_index() if platform.system() != "Windows" else config.CAMERA_DEVICE_INDEX
            if platform.system() == "Windows":
                # El backend por defecto en Windows (MSMF) se cuelga con
                # algunas camaras (probado con LifeCam HD-3000) - DSHOW abre
                # rapido. Irrelevante en la Orange Pi (Linux usa V4L2 directo).
                self._cap = cv2.VideoCapture(self._device_index, cv2.CAP_DSHOW)
            else:
                self._cap = cv2.VideoCapture(self._device_index)
            if self._cap.isOpened():
                break
            log.warning(
                "No se pudo abrir la camara (indice %s, intento %d/%d)",
                self._device_index, attempt, self._START_RETRIES,
            )
            if attempt < self._START_RETRIES:
                time.sleep(self._START_RETRY_DELAY_S)
        else:
            log.warning("Camara no disponible tras %d intentos - stream deshabilitado", self._START_RETRIES)
            self._available = False
            return
        # MJPG en vez del formato crudo por defecto (YUYV) - 17-sept, stream
        # se veia lento/feo. La LifeCam soporta MJPG nativo (confirmado con
        # v4l2-ctl --list-formats-ext), asi que la camara entrega el cuadro
        # ya comprimido en vez de que el Orange Pi tenga que convertir YUYV
        # a color el solo - menos CPU y menos ancho de banda USB, permite
        # mas FPS real. Hay que pedir el FOURCC ANTES de fijar resolucion,
        # algunos drivers UVC ignoran el cambio si se hace despues.
        self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
        self._cap.set(cv2.CAP_PROP_FPS, self._fps)
        # Buffer de captura al minimo - sin esto, V4L2 puede acumular
        # cuadros viejos en cola y el video se ve con delay creciente en
        # vez de en vivo (algunos backends ignoran esto, no rompe si no
        # esta soportado).
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._available = True
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("Camara iniciada (indice %s)", self._device_index)

    _JPEG_QUALITY = 85  # default de cv2 es 95 - se baja un poco para que la recompresion sea mas rapida

    def _loop(self):
        interval = 1.0 / self._fps
        while self._running:
            ok, frame = self._cap.read()
            if ok:
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self._JPEG_QUALITY])
                if ok:
                    with self._lock:
                        self._frame = buf.tobytes()
            time.sleep(interval)

    def is_available(self) -> bool:
        return self._available

    def get_jpeg(self):
        with self._lock:
            return self._frame

    def mjpeg_generator(self):
        boundary = b"--frame"
        while self._running:
            frame = self.get_jpeg()
            if frame is not None:
                yield (
                    boundary + b"\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
            time.sleep(1.0 / self._fps)

    def stop(self):
        self._running = False
        if self._cap is not None:
            self._cap.release()
