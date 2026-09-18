// ============================================================
// Rover Percy — firmware ESP32 (control WiFi + WebSocket)
// Desafío de Innovación CITT-2026, Duoc UC
//
// Reemplaza al firmware RC original (control por receptor RC leído
// como entradas digitales). Ahora el ESP32 levanta su propio punto de
// acceso WiFi y recibe comandos JSON por WebSocket desde la web app
// (control_app.html), tal como quedó definido en CLAUDE.md.
//
// Librerías necesarias (Arduino IDE > Herramientas > Administrar
// Bibliotecas, instalar por nombre exacto):
//   - WebSockets (de Markus Sattler / Links2004)
//   - ArduinoJson (de Benoit Blanchon)
//   - ESP32Servo (de Kevin Harrington / madhephaestus)
// WiFi.h y WebServer.h ya vienen incluidas con el core de ESP32.
//
// Dirección del rover una vez encendido: conectarse a la red WiFi
// "PercyRover" (clave más abajo) y abrir http://192.168.4.1/ en el
// navegador del celular/tablet.
//
// Nota de compatibilidad: este código usa la API de LEDC "clásica"
// (ledcSetup/ledcAttachPin/ledcWrite con número de canal), válida para
// el core ESP32 de Arduino 2.x. Si el core instalado es 3.x, esas
// funciones cambiaron de firma (ahora ledcAttach(pin, freq, res) sin
// número de canal) y el sketch no va a compilar tal cual — avisar si
// pasa eso para adaptar esa parte.
// ============================================================

#include <WiFi.h>
#include <WebServer.h>
#include <WebSocketsServer.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>

// ---------------- WiFi (access point propio) ----------------
const char* AP_SSID = "PercyRover";
const char* AP_PASS = "marte2026";   // WPA2 pide mínimo 8 caracteres

// ---------------- Pines: motores de tracción (2 grupos, izq/der) ----------------
// AJUSTAR estos números a como quedó cableado el puente H real.
#define MOTOR_L_IN1 16
#define MOTOR_L_IN2 17
#define MOTOR_L_PWM 25
#define MOTOR_R_IN1 18
#define MOTOR_R_IN2 19
#define MOTOR_R_PWM 26

// canales LEDC para el PWM de los motores (altos a propósito, para no
// chocar con los canales que ESP32Servo va tomando para cada servo)
#define PWM_CH_L 14
#define PWM_CH_R 15
#define PWM_FREQ 5000
#define PWM_RES  8      // resolución 0-255

// ---------------- Pines: servos de dirección (4 ruedas de esquina) ----------------
// AJUSTAR a los pines reales. Los ángulos de centro/giro SÍ son los ya
// calibrados a mano en el rover (rescatados del firmware RC anterior).
#define PIN_S1 27   // frontal izquierda
#define PIN_S3 5    // frontal derecha
#define PIN_S4 13   // trasera izquierda
#define PIN_S6 4    // trasera derecha

#define S1_CENTER 86
#define S3_CENTER 95
#define S4_CENTER 95
#define S6_CENTER 95
#define S1_GIRO 140
#define S3_GIRO 37
#define S4_GIRO 150
#define S6_GIRO 40

// ---------------- Pines: brazo y accesorios ----------------
// AJUSTAR según el cableado real del brazo HowToMechatronics.
#define PIN_SHOULDER 32
#define PIN_ELBOW    33
#define PIN_WRIST    21
#define PIN_GRIPPER  23
#define PIN_TRAY     22

// Rango y "home" del brazo — CALIBRAR con el brazo ya armado.
#define SHOULDER_MIN 0
#define SHOULDER_MAX 180
#define SHOULDER_HOME 90
#define ELBOW_MIN 20
#define ELBOW_MAX 160
#define ELBOW_HOME 90
#define WRIST_HOME 90
#define GRIPPER_OPEN 40
#define GRIPPER_CLOSED 120
#define TRAY_UP 0
#define TRAY_DUMP 100

// Posición calibrada para la macro "soltar en bandeja" — AJUSTAR en
// pruebas físicas, apuntando el brazo justo sobre la bandeja.
#define DEPOSIT_SHOULDER 55
#define DEPOSIT_ELBOW 130

// velocidad de movimiento del brazo (grados por ciclo de loop, ~20 ms)
#define ARM_STEP 1.5

// failsafe: si no llega ningún mensaje en este tiempo, todo se detiene
#define FAILSAFE_MS 500

// ---------------- objetos ----------------
Servo servoS1, servoS3, servoS4, servoS6;
Servo servoShoulder, servoElbow, servoWrist, servoGripper, servoTray;

WebServer httpServer(80);
WebSocketsServer webSocket(81);

// ---------------- estado de control (actualizado por los mensajes JSON) ----------------
float g_lx = 0, g_ly = 0;
float g_shoulderDir = 0, g_elbowDir = 0;  // -1..1, proporcional a la inclinación del joystick
bool g_grip = false;
bool g_tray = false;
int g_speed = 1;
unsigned long g_lastMsg = 0;

float g_shoulderAngle = SHOULDER_HOME;
float g_elbowAngle = ELBOW_HOME;

bool g_depositing = false;
int g_depositStep = 0;
unsigned long g_depositT0 = 0;

// ---------------- control de motores ----------------
void setMotors(float left, float right, int speedLevel) {
  int maxPWM = speedLevel <= 0 ? 140 : (speedLevel == 1 ? 200 : 255);
  int leftPWM  = (int)(fabs(left)  * maxPWM);
  int rightPWM = (int)(fabs(right) * maxPWM);
  digitalWrite(MOTOR_L_IN1, left  >= 0 ? HIGH : LOW);
  digitalWrite(MOTOR_L_IN2, left  >= 0 ? LOW  : HIGH);
  digitalWrite(MOTOR_R_IN1, right >= 0 ? HIGH : LOW);
  digitalWrite(MOTOR_R_IN2, right >= 0 ? LOW  : HIGH);
  ledcWrite(PWM_CH_L, leftPWM);
  ledcWrite(PWM_CH_R, rightPWM);
}

// mezcla proporcional entre centro y giro en las 4 esquinas, según lx
void setSteering(float lx) {
  float t = constrain(fabs(lx), 0.0f, 1.0f);
  float sign = (lx >= 0) ? 1.0f : -1.0f;
  int s1 = S1_CENTER + (int)((S1_GIRO - S1_CENTER) * t * sign);
  int s3 = S3_CENTER + (int)((S3_GIRO - S3_CENTER) * t * sign);
  int s4 = S4_CENTER + (int)((S4_GIRO - S4_CENTER) * t * sign);
  int s6 = S6_CENTER + (int)((S6_GIRO - S6_CENTER) * t * sign);
  servoS1.write(constrain(s1, 0, 180));
  servoS3.write(constrain(s3, 0, 180));
  servoS4.write(constrain(s4, 0, 180));
  servoS6.write(constrain(s6, 0, 180));
}

void stopAll() {
  setMotors(0, 0, 0);
  servoS1.write(S1_CENTER);
  servoS3.write(S3_CENTER);
  servoS4.write(S4_CENTER);
  servoS6.write(S6_CENTER);
}

// ---------------- macro "soltar en bandeja" ----------------
void startDepositMacro() {
  g_depositing = true;
  g_depositStep = 0;
  g_depositT0 = millis();
}

void updateDepositMacro() {
  if (!g_depositing) return;
  unsigned long t = millis() - g_depositT0;
  switch (g_depositStep) {
    case 0: // moverse a la posición calibrada sobre la bandeja
      g_shoulderAngle = DEPOSIT_SHOULDER;
      g_elbowAngle = DEPOSIT_ELBOW;
      if (t > 800) { g_depositStep = 1; g_depositT0 = millis(); }
      break;
    case 1: // abrir la garra
      servoGripper.write(GRIPPER_OPEN);
      if (t > 400) { g_depositStep = 2; g_depositT0 = millis(); }
      break;
    case 2: // volver a la posición de espera
      g_shoulderAngle = SHOULDER_HOME;
      g_elbowAngle = ELBOW_HOME;
      if (t > 800) { g_depositing = false; }
      break;
  }
}

// ---------------- WebSocket: recepción de comandos ----------------
void handleMessage(uint8_t* payload, size_t len) {
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, payload, len);
  if (err) return;

  g_lx = doc["lx"] | 0.0;
  g_ly = doc["ly"] | 0.0;
  g_shoulderDir = doc["shoulder_dir"] | 0.0;
  g_elbowDir = doc["elbow_dir"] | 0.0;
  g_grip = (int)(doc["grip"] | 0) == 1;
  g_tray = (int)(doc["tray"] | 0) == 1;
  g_speed = doc["speed"] | 1;

  if ((int)(doc["deposit"] | 0) == 1 && !g_depositing) {
    startDepositMacro();
  }

  g_lastMsg = millis();
}

// ---------------- web app (servida directo desde el ESP32, sin SPIFFS) ----------------
// Un solo archivo embebido para no depender de subir un filesystem
// aparte — más simple de flashear bajo apuro. Dos joysticks táctiles
// (canvas), botones de garra/bandeja/soltar, selector de velocidad.
// Se conecta por WebSocket al puerto 81 del mismo host.
const char INDEX_HTML[] PROGMEM = R"HTMLPAGE(
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>Percy Control</title>
<style>
  * { box-sizing: border-box; -webkit-user-select: none; user-select: none; touch-action: none; }
  html, body { margin:0; padding:0; background:#0c151d; color:#dfe9f1;
    font-family: system-ui, sans-serif; height:100%; overflow:hidden; }
  #fpv { width:100%; height:22vh; background:#000 center/cover no-repeat;
    display:flex; align-items:center; justify-content:center; color:#5b7185; font-size:.8rem; }
  #main { display:flex; justify-content:space-between; align-items:center;
    height:70vh; padding:0 10px; }
  .stick-wrap { display:flex; flex-direction:column; align-items:center; gap:6px; }
  canvas.stick { background:#142230; border-radius:50%; touch-action:none; }
  #mid { display:flex; flex-direction:column; align-items:center; gap:10px; flex:1; }
  button { font-family:inherit; font-size:.85rem; font-weight:600; color:#dfe9f1;
    background:#1a2934; border:1px solid #243645; border-radius:8px; padding:10px 14px; }
  button.active { background:#e2925a; color:#1a0f06; border-color:#e2925a; }
  #speedRow { display:flex; gap:4px; }
  #speedRow button { padding:6px 10px; font-size:.75rem; }
  #status { font-size:.7rem; color:#5b7185; text-align:center; padding:4px; }
</style>
</head>
<body>
  <div id="fpv">(feed FPV opcional — ESP32-CAM)</div>
  <div id="main">
    <div class="stick-wrap">
      <canvas id="stickL" class="stick" width="140" height="140"></canvas>
      <span style="font-size:.7rem;color:#5b7185">Tracción</span>
    </div>
    <div id="mid">
      <button id="btnGrip">Garra</button>
      <button id="btnDeposit">Soltar en bandeja</button>
      <button id="btnTray">Bandeja</button>
      <div id="speedRow">
        <button id="spd0">Lenta</button>
        <button id="spd1" class="active">Media</button>
        <button id="spd2">Rápida</button>
      </div>
    </div>
    <div class="stick-wrap">
      <canvas id="stickR" class="stick" width="140" height="140"></canvas>
      <span style="font-size:.7rem;color:#5b7185">Brazo</span>
    </div>
  </div>
  <div id="status">conectando...</div>

<script>
(function(){
  "use strict";

  function makeStick(canvasId){
    var c = document.getElementById(canvasId);
    var ctx = c.getContext('2d');
    var r = c.width/2;
    var knob = {x:0, y:0}; // -1..1
    var active = false;

    function draw(){
      ctx.clearRect(0,0,c.width,c.height);
      ctx.beginPath();
      ctx.arc(r,r,r-2,0,Math.PI*2);
      ctx.strokeStyle = '#243645';
      ctx.lineWidth = 2;
      ctx.stroke();
      var kx = r + knob.x*(r-24);
      var ky = r + knob.y*(r-24);
      ctx.beginPath();
      ctx.arc(kx,ky,20,0,Math.PI*2);
      ctx.fillStyle = active ? '#e2925a' : '#3a4c5c';
      ctx.fill();
    }

    function setFromEvent(clientX, clientY){
      var rect = c.getBoundingClientRect();
      var x = (clientX - rect.left - r) / (r-24);
      var y = (clientY - rect.top - r) / (r-24);
      var mag = Math.sqrt(x*x+y*y);
      if (mag > 1){ x/=mag; y/=mag; }
      knob.x = x; knob.y = y;
      draw();
    }

    function reset(){ knob.x=0; knob.y=0; active=false; draw(); }

    c.addEventListener('touchstart', function(e){ active=true; var t=e.touches[0]; setFromEvent(t.clientX,t.clientY); e.preventDefault(); }, {passive:false});
    c.addEventListener('touchmove', function(e){ if(!active) return; var t=e.touches[0]; setFromEvent(t.clientX,t.clientY); e.preventDefault(); }, {passive:false});
    c.addEventListener('touchend', function(e){ reset(); e.preventDefault(); }, {passive:false});
    c.addEventListener('mousedown', function(e){ active=true; setFromEvent(e.clientX,e.clientY); });
    window.addEventListener('mousemove', function(e){ if(!active) return; setFromEvent(e.clientX,e.clientY); });
    window.addEventListener('mouseup', function(){ reset(); });

    draw();
    return knob;
  }

  var stickL = makeStick('stickL'); // traccion: x=lx, y=ly (arriba = adelante)
  var stickR = makeStick('stickR'); // brazo: y=hombro, x=codo

  var state = { grip:0, tray:0, deposit:0, speed:1 };

  var btnGrip = document.getElementById('btnGrip');
  btnGrip.addEventListener('click', function(){
    state.grip = state.grip ? 0 : 1;
    btnGrip.classList.toggle('active', !!state.grip);
  });
  var btnTray = document.getElementById('btnTray');
  btnTray.addEventListener('click', function(){
    state.tray = state.tray ? 0 : 1;
    btnTray.classList.toggle('active', !!state.tray);
  });
  document.getElementById('btnDeposit').addEventListener('click', function(){
    state.deposit = 1;
    setTimeout(function(){ state.deposit = 0; }, 150);
  });
  [['spd0',0],['spd1',1],['spd2',2]].forEach(function(pair){
    document.getElementById(pair[0]).addEventListener('click', function(){
      state.speed = pair[1];
      ['spd0','spd1','spd2'].forEach(function(id){
        document.getElementById(id).classList.toggle('active', id===pair[0]);
      });
    });
  });

  var statusEl = document.getElementById('status');
  var ws;
  function connect(){
    ws = new WebSocket('ws://' + location.hostname + ':81/');
    ws.onopen = function(){ statusEl.textContent = 'conectado'; };
    ws.onclose = function(){ statusEl.textContent = 'desconectado — reintentando...'; setTimeout(connect, 1000); };
    ws.onerror = function(){ ws.close(); };
  }
  connect();

  setInterval(function(){
    if (!ws || ws.readyState !== 1) return;
    var msg = {
      lx: Math.round(stickL.x*100)/100,
      ly: Math.round(-stickL.y*100)/100,
      shoulder_dir: Math.round(-stickR.y*100)/100,
      elbow_dir: Math.round(stickR.x*100)/100,
      grip: state.grip,
      tray: state.tray,
      deposit: state.deposit,
      speed: state.speed
    };
    ws.send(JSON.stringify(msg));
    if (state.deposit) state.deposit = 0;
  }, 100);
})();
</script>
</body>
</html>
)HTMLPAGE";

void onWsEvent(uint8_t clientId, WStype_t type, uint8_t* payload, size_t len) {
  switch (type) {
    case WStype_CONNECTED:
      g_lastMsg = millis();
      break;
    case WStype_DISCONNECTED:
      stopAll();
      break;
    case WStype_TEXT:
      handleMessage(payload, len);
      break;
    default:
      break;
  }
}

// ---------------- setup ----------------
void setup() {
  Serial.begin(115200);

  pinMode(MOTOR_L_IN1, OUTPUT);
  pinMode(MOTOR_L_IN2, OUTPUT);
  pinMode(MOTOR_R_IN1, OUTPUT);
  pinMode(MOTOR_R_IN2, OUTPUT);
  ledcSetup(PWM_CH_L, PWM_FREQ, PWM_RES);
  ledcSetup(PWM_CH_R, PWM_FREQ, PWM_RES);
  ledcAttachPin(MOTOR_L_PWM, PWM_CH_L);
  ledcAttachPin(MOTOR_R_PWM, PWM_CH_R);

  servoS1.attach(PIN_S1);
  servoS3.attach(PIN_S3);
  servoS4.attach(PIN_S4);
  servoS6.attach(PIN_S6);
  servoShoulder.attach(PIN_SHOULDER);
  servoElbow.attach(PIN_ELBOW);
  servoWrist.attach(PIN_WRIST);
  servoGripper.attach(PIN_GRIPPER);
  servoTray.attach(PIN_TRAY);

  stopAll();
  servoShoulder.write(SHOULDER_HOME);
  servoElbow.write(ELBOW_HOME);
  servoWrist.write(WRIST_HOME);
  servoGripper.write(GRIPPER_OPEN);
  servoTray.write(TRAY_UP);

  WiFi.softAP(AP_SSID, AP_PASS);
  Serial.print("Access point listo. IP: ");
  Serial.println(WiFi.softAPIP()); // por defecto 192.168.4.1

  httpServer.on("/", HTTP_GET, []() {
    httpServer.send_P(200, "text/html", INDEX_HTML);
  });
  httpServer.begin();

  webSocket.begin();
  webSocket.onEvent(onWsEvent);

  g_lastMsg = millis();
}

// ---------------- loop ----------------
void loop() {
  httpServer.handleClient();
  webSocket.loop();

  if (millis() - g_lastMsg > FAILSAFE_MS) {
    stopAll();
  } else {
    // control diferencial (igual fórmula que en la web app) + dirección
    float left = g_ly + g_lx;
    float right = g_ly - g_lx;
    float maxv = max(max(fabs(left), fabs(right)), 1.0f);
    left /= maxv;
    right /= maxv;
    setMotors(left, right, g_speed);
    setSteering(g_lx);
  }

  if (g_shoulderDir != 0) g_shoulderAngle += g_shoulderDir * ARM_STEP;
  if (g_elbowDir != 0) g_elbowAngle += g_elbowDir * ARM_STEP;
  g_shoulderAngle = constrain(g_shoulderAngle, SHOULDER_MIN, SHOULDER_MAX);
  g_elbowAngle = constrain(g_elbowAngle, ELBOW_MIN, ELBOW_MAX);

  updateDepositMacro();

  servoShoulder.write((int)g_shoulderAngle);
  servoElbow.write((int)g_elbowAngle);
  if (!g_depositing) {
    servoGripper.write(g_grip ? GRIPPER_CLOSED : GRIPPER_OPEN);
  }
  servoTray.write(g_tray ? TRAY_DUMP : TRAY_UP);

  delay(20);
}
