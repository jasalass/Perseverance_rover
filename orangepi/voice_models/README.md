# Modelo de voz (control por voz)

## En pocas palabras

Percy puede recibir algunas órdenes habladas, como "abrir garra" o
"parar", además de los joysticks. El reconocimiento de voz funciona
**dentro del navegador del celular, sin internet**: el audio nunca sale
del teléfono.

Para eso el navegador necesita un **modelo de voz**, un archivo que
"sabe" reconocer español hablado. Pesa unos 40 MB, así que no se guarda
en el repositorio: hay que descargarlo y prepararlo una vez con los
pasos de abajo.

Para robustez frente al ruido, el rover solo escucha 7 frases:

```
"abrir garra", "cerrar garra", "depositar",
"posicion inicial", "traslado", "parar", "alto"
```

Se usa manteniendo apretado el botón del micrófono en el panel de
control mientras se dice el comando.

---

## Detalle técnico

### Generar el modelo

Modelo [Vosk](https://alphacephei.com/vosk/models) pequeño en español
(`vosk-model-small-es-0.42`), empaquetado como `.tar.gz` para
`vosk-browser`:

```bash
curl -L -o vosk-model-small-es.zip https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip
unzip vosk-model-small-es.zip
tar -czf model.tar.gz vosk-model-small-es-0.42
cp model.tar.gz ../static/voice/model.tar.gz
```

Después, copiar `static/voice/model.tar.gz` a la Orange Pi junto con el
resto de la carpeta `orangepi/`.

### Requisito: HTTPS

El navegador solo da acceso al micrófono en un contexto seguro
(`https://` o `localhost`). Servido por `http://<ip>:8000`, el botón de
voz falla con "SIN PERMISO DE MIC". Ver la sección "HTTPS" de
[`orangepi/README.md`](../README.md) para generar el certificado
autofirmado.

### Archivos de esta carpeta

- `test_page.html` + `test_voice.py`: pruebas locales, no forman parte
  del dashboard. Sirvieron para validar que Vosk reconoce bien los
  comandos en español antes de integrarlo a `static/app.js`.

Estado: probado offline en PC; pendiente de probar en el celular real
con HTTPS.
