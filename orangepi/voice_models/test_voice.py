"""
Prueba de reconocimiento de voz offline (Vosk, modelo chico en espanol) -
NO es el codigo final del rover, es solo para validar si el motor
reconoce bien los comandos por el microfono antes de integrarlo.

Vocabulario restringido (gramatica) en vez de reconocimiento abierto -
mucho mas robusto al ruido, ver notas de la conversacion.
"""
import json
import queue
import sys
import time

import sounddevice as sd
import vosk

MODEL_PATH = "vosk-model-small-es-0.42"
SAMPLE_RATE = 16000

# Comandos candidatos del rover - vocabulario cerrado.
COMMANDS = [
    "abrir garra", "cerrar garra", "depositar", "posicion inicial",
    "traslado", "parar", "alto",
]

vosk.SetLogLevel(-1)  # silenciar el log interno de Vosk/Kaldi

model = vosk.Model(MODEL_PATH)
grammar = json.dumps(COMMANDS + ["[unk]"], ensure_ascii=False)
rec = vosk.KaldiRecognizer(model, SAMPLE_RATE, grammar)

q = queue.Queue()


def callback(indata, frames, time_info, status):
    q.put(bytes(indata))


print("Comandos disponibles:")
for c in COMMANDS:
    print(f"  - {c}")
duration = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
for i in (3, 2, 1):
    print(f"Arrancando en {i}...")
    time.sleep(1)
print(f">>> HABLA AHORA (escuchando {duration:.0f}s) <<<")

start = time.time()

with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=8000, dtype="int16",
                        channels=1, device=1, callback=callback):
    while time.time() - start < duration:
        try:
            data = q.get(timeout=1)
        except queue.Empty:
            continue
        if rec.AcceptWaveform(data):
            result = json.loads(rec.Result())
            text = result.get("text", "").strip()
            if text:
                print(f">>> RECONOCIDO: '{text}'")
        else:
            partial = json.loads(rec.PartialResult()).get("partial", "")
            if partial:
                print(f"    (escuchando: {partial})", end="\r")

print("\nListo.")
