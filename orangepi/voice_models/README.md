# Modelo de voz (control por voz, rama `control_voz`)

No versionado en git (~40MB). Para regenerarlo:

```bash
curl -L -o vosk-model-small-es.zip https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip
unzip vosk-model-small-es.zip
tar -czf model.tar.gz vosk-model-small-es-0.42
cp model.tar.gz ../static/voice/model.tar.gz
```

`test_page.html` + `test_voice.py` son pruebas locales (no forman parte
del dashboard real) - sirvieron para validar que Vosk reconoce bien los
comandos en espanol antes de integrarlo a `static/app.js`.

**Pendiente antes de que esto funcione en el celular real:** el
navegador exige HTTPS (o `localhost`) para dar acceso al microfono -
`http://192.168.x.x:8000` (como se sirve el dashboard hoy) no alcanza.
Hay que ponerle HTTPS (certificado autofirmado) al servidor FastAPI de
la Orange Pi antes de probar esto en el celular de verdad.
