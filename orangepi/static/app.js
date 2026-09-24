(function () {
  "use strict";

  // Mantener presionado un boton (base/muneca/giro de garra) dispara el
  // menu contextual nativo del navegador (compartir/copiar en iOS,
  // "Traducir"/etc en Android) - preventDefault en touchstart no alcanza
  // solo en algunos navegadores/versiones, asi que se bloquea el evento
  // de mas tambien, para toda la pagina (no hace falta en ningun lado).
  document.addEventListener("contextmenu", (e) => e.preventDefault());

  const state = {
    lx: 0, ly: 0,
    base_dir: 0, gripper_rotate_dir: 0, arm_x_dir: 0, arm_y_dir: 0, wrist_dir: 0, // wrist_dir = angulo de acercamiento (phi) desde el rediseno 18-sept
    grip: 0, deposit: 0,
    preset_save: null, preset_goto: null, preset_delete: null,
    speed: 1,
    arm_mode: "ik", // "ik" = joystick derecho mueve la garra en linea recta; "joint" = hombro/codo directo (ver btn-arm-mode)
    drive_mode: "combinado", // "combinado" | "vehiculo" | "tanque" (ver btn-drive-mode)
  };

  const SEND_INTERVAL_MS = 50; // 20Hz - mas fluido, sigue bien por debajo del failsafe de 500ms

  // Curva exponencial para el joystick del brazo: empujar poco mueve poco
  // (control fino), solo al fondo del stick se llega a velocidad maxima.
  // Sin esto, un empuje moderado ya daba un paso grande y se sentia brusco.
  function expoCurve(v) {
    return v * v * v; // cubica: preserva signo, suave cerca del centro
  }

  // --- Feedback haptico (18-sept) ------------------------------------------
  // navigator.vibrate no existe en iOS Safari - se degrada a no-hacer-nada
  // en vez de romper, no hay forma de dar vibracion real ahi.
  function vibrate(pattern) {
    try {
      if (navigator.vibrate) navigator.vibrate(pattern);
    } catch (e) {
      // ignorar - sin vibracion en este navegador
    }
  }

  // --- WebSocket ----------------------------------------------------------
  let ws = null;
  let missionStart = null; // Date.now() de la primera conexion, para el reloj de mision
  let lastHitLimit = false; // para vibrar solo en el flanco ascendente, no todo el rato
  const statusEl = document.getElementById("conn-status");
  const statusLabel = document.getElementById("conn-label");

  function connect() {
    // wss:// si la pagina se sirvio por https (18-sept, HTTPS para el
    // control por voz) - un ws:// plano desde una pagina https se bloquea
    // como "contenido mixto", el navegador ni intenta conectar.
    const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${wsProto}//${location.hostname}:8000/ws`;
    ws = new WebSocket(url);
    ws.onopen = () => {
      statusLabel.textContent = "EN LINEA";
      statusEl.className = "connected";
      if (missionStart === null) missionStart = Date.now();
    };
    ws.onclose = () => {
      statusLabel.textContent = "SIN SEÑAL";
      statusEl.className = "disconnected";
      setTimeout(connect, 1000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (ev) => {
      // Eco del servidor con el estado del brazo (ver main.py ws_control) -
      // hoy solo trae hit_limit, para el feedback haptico al tocar un tope.
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      if (msg.hit_limit && !lastHitLimit) vibrate(35);
      lastHitLimit = !!msg.hit_limit;
    };
  }
  connect();

  setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(state));
      // los triggers de un solo disparo se resetean despues de enviarlos
      state.grip = 0;
      state.deposit = 0;
      state.preset_save = null;
      state.preset_goto = null;
      state.preset_delete = null;
    }
  }, SEND_INTERVAL_MS);

  // --- Camara ---------------------------------------------------------------
  const fpvImg = document.getElementById("fpv");
  const fpvOff = document.getElementById("fpv-off");
  fpvImg.classList.add("hidden");
  fpvImg.addEventListener("load", () => { fpvImg.classList.remove("hidden"); fpvOff.classList.add("hidden"); });
  fpvImg.addEventListener("error", () => { fpvImg.classList.add("hidden"); fpvOff.classList.remove("hidden"); });
  fpvImg.src = "/video";

  // --- Reloj de mision (T+MM:SS desde la primera conexion) ----------------
  const clockEl = document.getElementById("mission-clock");
  setInterval(() => {
    if (missionStart === null) return;
    const elapsed = Math.floor((Date.now() - missionStart) / 1000);
    const mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
    const ss = String(elapsed % 60).padStart(2, "0");
    clockEl.textContent = `T+${mm}:${ss}`;
  }, 1000);

  // --- Joysticks --------------------------------------------------------
  function setupJoystick(elId, onMove) {
    const el = document.getElementById(elId);
    const stick = el.querySelector(".stick");
    let active = false;
    let originX = 0, originY = 0, radius = 0;

    function start(x, y) {
      const rect = el.getBoundingClientRect();
      originX = rect.left + rect.width / 2;
      originY = rect.top + rect.height / 2;
      radius = rect.width / 2;
      active = true;
      move(x, y);
    }

    function move(x, y) {
      if (!active) return;
      let dx = x - originX;
      let dy = y - originY;
      const dist = Math.min(Math.hypot(dx, dy), radius);
      const angle = Math.atan2(dy, dx);
      dx = Math.cos(angle) * dist;
      dy = Math.sin(angle) * dist;
      stick.style.left = `${30 + (dx / radius) * 30}%`;
      stick.style.top = `${30 + (dy / radius) * 30}%`;
      const nx = dx / radius;
      const ny = -dy / radius; // arriba = positivo
      onMove(nx, ny);
    }

    function end() {
      active = false;
      stick.style.left = "30%";
      stick.style.top = "30%";
      onMove(0, 0);
    }

    el.addEventListener("touchstart", (e) => { e.preventDefault(); const t = e.touches[0]; start(t.clientX, t.clientY); }, { passive: false });
    el.addEventListener("touchmove", (e) => { e.preventDefault(); const t = e.touches[0]; move(t.clientX, t.clientY); }, { passive: false });
    el.addEventListener("touchend", (e) => { e.preventDefault(); end(); }, { passive: false });

    // soporte mouse para probar en escritorio
    el.addEventListener("mousedown", (e) => start(e.clientX, e.clientY));
    window.addEventListener("mousemove", (e) => { if (active) move(e.clientX, e.clientY); });
    window.addEventListener("mouseup", () => { if (active) end(); });
  }

  setupJoystick("joy-left", (nx, ny) => {
    state.lx = nx;
    state.ly = ny;
  });

  setupJoystick("joy-right", (nx, ny) => {
    // Control por cinematica inversa (16-sept): el stick ya no mueve
    // hombro/codo como articulaciones separadas - mueve la punta de la
    // garra en linea recta. Eje vertical -> arriba/abajo, eje horizontal
    // -> adelante/atras; el backend calcula los angulos (ver kinematics.py).
    // Proporcional (no umbral -1/0/1): mientras mas empujas el stick, mas
    // rapido se mueve. Zona muerta chica cerca del centro para que el
    // reposo no derive por ruido del stick.
    const deadzone = 0.08;
    state.arm_y_dir = Math.abs(ny) < deadzone ? 0 : expoCurve(ny);
    state.arm_x_dir = Math.abs(nx) < deadzone ? 0 : expoCurve(nx);
  });

  // --- Switch IK / LIBRE (16-sept) -----------------------------------------
  // Mismos ejes del joystick derecho para los dos modos - el backend decide
  // que significan segun el modo activo (ver main.py, _arm_mode). "LIBRE"
  // vuelve al control directo de hombro/codo, cada uno por su cuenta, para
  // calibrar o alcanzar posiciones que la IK no puede (zona muerta del
  // anillo IK_L1_MM/IK_L2_MM - ver CLAUDE.md).
  const armModeBtn = document.getElementById("btn-arm-mode");
  const armModeLabel = document.getElementById("arm-mode-label");
  armModeBtn.addEventListener("click", () => {
    const toJoint = state.arm_mode === "ik";
    state.arm_mode = toJoint ? "joint" : "ik";
    armModeBtn.textContent = toJoint ? "LIBRE" : "IK";
    armModeBtn.classList.toggle("on", toJoint);
    armModeLabel.textContent = toJoint ? "HOMBRO / CODO" : "GARRA · ALCANCE";
  });

  // --- Switch modo de traccion (18-sept) -----------------------------------
  // Combinado (default) = las ruedas de esquina Y el diferencial giran a
  // la vez con el mismo lx. Vehiculo = solo dirigen las ruedas (los dos
  // lados a igual velocidad, como un auto). Tanque = solo diferencial
  // entre lados, ruedas de esquina fijas al centro (radio de giro cero).
  // El backend decide que hacer con lx/ly segun _drive_mode (ver main.py).
  const DRIVE_MODE_LABELS = { combinado: "COMB", vehiculo: "VEHÍCULO", tanque: "TANQUE" };
  const DRIVE_MODES = Object.keys(DRIVE_MODE_LABELS);
  const driveModeBtn = document.getElementById("btn-drive-mode");
  driveModeBtn.addEventListener("click", () => {
    const next = DRIVE_MODES[(DRIVE_MODES.indexOf(state.drive_mode) + 1) % DRIVE_MODES.length];
    state.drive_mode = next;
    driveModeBtn.textContent = DRIVE_MODE_LABELS[next];
    driveModeBtn.classList.toggle("on", next !== "combinado");
    vibrate(15);
  });

  // --- Botones extra del brazo (base + muneca) ---------------------------
  document.querySelectorAll("#arm-extra button[data-axis]").forEach((btn) => {
    const axis = btn.dataset.axis;
    const val = parseInt(btn.dataset.val, 10);
    const press = (e) => { e.preventDefault(); state[axis] = val; btn.classList.add("on"); };
    const release = (e) => { e.preventDefault(); state[axis] = 0; btn.classList.remove("on"); };
    btn.addEventListener("touchstart", press, { passive: false });
    btn.addEventListener("touchend", release, { passive: false });
    btn.addEventListener("mousedown", press);
    btn.addEventListener("mouseup", release);
  });

  // --- Botones de accion (garra / deposito) --------------------
  document.getElementById("btn-grip").addEventListener("click", () => { state.grip = 1; vibrate(25); });
  document.getElementById("btn-deposit").addEventListener("click", () => { state.deposit = 1; vibrate([20, 30, 20]); });

  // --- Control por voz (18-sept, rama control_voz) -------------------------
  // Reconocimiento 100% local en el navegador (Vosk-browser, WASM) - nada
  // de audio sale del celular ni depende de internet. Vocabulario cerrado
  // (gramatica) en vez de reconocimiento abierto, mucho mas robusto al
  // ruido real de un venue que "entender cualquier frase". Mantener
  // apretado para hablar, soltar corta - evita que capte ruido de fondo
  // como comando por error.
  //
  // OJO: getUserMedia (microfono) exige HTTPS o localhost - el dashboard
  // se sirve hoy por HTTP plano en la red local (192.168.x.x), asi que el
  // navegador va a rechazar el permiso hasta que el server tenga HTTPS
  // (ver orangepi/voice_models/README.md).
  const VOICE_COMMANDS = ["abrir garra", "cerrar garra", "depositar", "posicion inicial", "traslado", "parar", "alto"];
  const voiceBtn = document.getElementById("btn-voice");
  const voiceLabel = document.getElementById("btn-voice-label");
  let voiceModel = null;
  let voiceRecognizer = null;
  let voiceAudioCtx = null;
  let voiceStream = null;
  let voiceSource = null;
  let voiceProcessor = null;

  function handleVoiceCommand(text) {
    switch (text) {
      case "abrir garra":
      case "cerrar garra":
        state.grip = 1;
        vibrate(25);
        break;
      case "depositar":
        state.deposit = 1;
        vibrate([20, 30, 20]);
        break;
      case "traslado":
      case "posicion inicial":
        state.preset_goto = text; // solo hace algo si existe un preset guardado con ese nombre
        vibrate(15);
        break;
      case "parar":
      case "alto":
        // corta cualquier movimiento sostenido (joysticks + botones de eje) al toque
        state.lx = 0; state.ly = 0; state.arm_x_dir = 0; state.arm_y_dir = 0;
        state.base_dir = 0; state.wrist_dir = 0; state.gripper_rotate_dir = 0;
        vibrate([15, 15, 15, 15, 15]);
        break;
    }
  }

  async function loadVoiceModel() {
    if (typeof Vosk === "undefined") {
      voiceLabel.textContent = "VOZ NO DISPONIBLE";
      return;
    }
    try {
      voiceModel = await Vosk.createModel("voice/model.tar.gz");
      voiceBtn.disabled = false;
      voiceLabel.textContent = "VOZ";
      console.log("[voz] modelo cargado, boton habilitado");
    } catch (e) {
      voiceLabel.textContent = "VOZ NO DISPONIBLE";
      console.error("No se pudo cargar el modelo de voz:", e);
    }
  }

  async function startVoiceListening() {
    console.log("[voz] press detectado - voiceModel:", !!voiceModel, "disabled:", voiceBtn.disabled);
    if (!voiceModel || voiceBtn.disabled) return;
    voiceBtn.classList.add("listening");
    voiceLabel.textContent = "ESCUCHANDO…";

    const grammar = JSON.stringify(VOICE_COMMANDS.concat(["[unk]"]));
    voiceRecognizer = new voiceModel.KaldiRecognizer(48000, grammar);
    voiceRecognizer.on("result", (message) => {
      const text = (message.result.text || "").trim();
      console.log("[voz] reconocido:", JSON.stringify(text), VOICE_COMMANDS.includes(text) ? "-> match" : "-> sin match");
      if (VOICE_COMMANDS.includes(text)) handleVoiceCommand(text);
    });
    voiceRecognizer.on("partialresult", (message) => {
      if (message.result.partial) console.log("[voz] parcial:", message.result.partial);
    });

    try {
      voiceStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
      });
    } catch (e) {
      voiceLabel.textContent = "SIN PERMISO DE MIC";
      voiceBtn.classList.remove("listening");
      console.error("getUserMedia fallo (necesita HTTPS o localhost):", e);
      return;
    }
    voiceAudioCtx = new AudioContext();
    voiceSource = voiceAudioCtx.createMediaStreamSource(voiceStream);
    voiceProcessor = voiceAudioCtx.createScriptProcessor(4096, 1, 1);
    voiceProcessor.onaudioprocess = (event) => voiceRecognizer.acceptWaveform(event.inputBuffer);
    voiceSource.connect(voiceProcessor);
    voiceProcessor.connect(voiceAudioCtx.destination);
  }

  function stopVoiceListening() {
    voiceBtn.classList.remove("listening");
    voiceLabel.textContent = "VOZ";
    if (voiceProcessor) { voiceProcessor.disconnect(); voiceProcessor = null; }
    if (voiceSource) { voiceSource.disconnect(); voiceSource = null; }
    if (voiceStream) { voiceStream.getTracks().forEach((t) => t.stop()); voiceStream = null; }
    if (voiceAudioCtx) { voiceAudioCtx.close(); voiceAudioCtx = null; }
    if (voiceRecognizer) { voiceRecognizer.remove(); voiceRecognizer = null; }
  }

  voiceBtn.addEventListener("touchstart", (e) => { e.preventDefault(); startVoiceListening(); });
  voiceBtn.addEventListener("touchend", (e) => { e.preventDefault(); stopVoiceListening(); });
  voiceBtn.addEventListener("mousedown", startVoiceListening);
  voiceBtn.addEventListener("mouseup", stopVoiceListening);

  loadVoiceModel();

  // --- Selector de velocidad ----------------------------------------------
  document.querySelectorAll("#speed-select button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#speed-select button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.speed = parseInt(btn.dataset.speed, 10);
    });
  });

  // --- Pantalla completa ---------------------------------------------------
  // El navegador solo permite entrar a fullscreen desde un gesto directo
  // del usuario (no se puede activar solo al cargar la pagina) - por eso
  // es un boton, no algo automatico. La otra forma de tener pantalla
  // completa de verdad (sin ni la barra de direccion) es agregar esta
  // pagina a la pantalla de inicio del celular (usa manifest.json).
  const fsBtn = document.getElementById("btn-fullscreen");
  fsBtn.addEventListener("click", () => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
    } else {
      document.exitFullscreen().catch(() => {});
    }
  });
  document.addEventListener("fullscreenchange", () => {
    fsBtn.classList.toggle("on", !!document.fullscreenElement);
  });

  // --- Posiciones guardadas (presets): boton discreto, protegido por PIN --
  // No es seguridad real (el PIN va en el propio JS) - es solo un freno
  // para no guardar/borrar/mover el brazo a una posicion por un toque
  // accidental en plena pista. Una vez destrabado con el PIN correcto,
  // queda destrabado el resto de la sesion (hasta recargar la pagina).
  //
  // Un preset es solo el angulo final del brazo bajo un nombre elegido por
  // el operador - al pedirlo, el brazo se mueve solo hasta ahi (el backend
  // calcula el camino, ver presets.py), no hace falta grabar el trayecto.
  const REC_PIN = "2026";
  const recWidget = document.getElementById("rec-widget");
  const recLockBtn = document.getElementById("btn-rec-lock");
  const saveBtn = document.getElementById("btn-preset-save");
  const presetListEl = document.getElementById("preset-list");
  const angleReadoutEl = document.getElementById("angle-readout");
  let recArmed = false;

  // Nombre corto a mostrar -> nombre que devuelve /servos (ver servos.py:get_all_angles)
  const ANGLE_LABELS = {
    base: "BASE", hombro: "HOMBRO", codo: "CODO", inclinacion_garra: "ÁNGULO GARRA",
    gripper: "GARRA",
  };

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  async function fetchPresets() {
    try {
      const res = await fetch("/presets");
      const data = await res.json();
      renderPresets(data.names || []);
    } catch (e) {
      // sin conexion momentanea - se reintenta en el proximo save/delete/unlock
    }
  }

  function renderPresets(names) {
    if (!names.length) {
      presetListEl.innerHTML = `<span class="preset-empty">SIN POSICIONES GUARDADAS</span>`;
      return;
    }
    presetListEl.innerHTML = names.map((n) => `
      <div class="preset-item">
        <button class="preset-goto" data-name="${escapeHtml(n)}"><svg><use href="#ic-play"/></svg><span>${escapeHtml(n)}</span></button>
        <button class="preset-del" data-name="${escapeHtml(n)}">&times;</button>
      </div>`).join("");
    presetListEl.querySelectorAll(".preset-goto").forEach((btn) => {
      btn.addEventListener("click", () => { state.preset_goto = btn.dataset.name; });
    });
    presetListEl.querySelectorAll(".preset-del").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (window.confirm(`Borrar posición "${btn.dataset.name}"?`)) {
          state.preset_delete = btn.dataset.name;
          setTimeout(fetchPresets, 400);
        }
      });
    });
  }

  // El mismo PIN destraba posiciones y WiFi (los dos son menus de "taller",
  // no de mision). Abrir uno cierra el otro para no taparse.
  const wifiWidget = document.getElementById("wifi-widget");

  function unlockMenus() {
    if (recArmed) return true;
    const pin = window.prompt("PIN:");
    if (pin !== REC_PIN) return false;
    recArmed = true;
    recWidget.classList.add("armed");
    wifiWidget.classList.add("armed");
    fetchPresets();
    return true;
  }

  function toggleMenu(widget, other) {
    const wasArmed = recArmed;
    if (!unlockMenus()) return false;
    other.classList.remove("panel-open");
    if (wasArmed) widget.classList.toggle("panel-open");
    else widget.classList.add("panel-open");
    return widget.classList.contains("panel-open");
  }

  recLockBtn.addEventListener("click", () => { toggleMenu(recWidget, wifiWidget); });

  // --- WiFi ----------------------------------------------------------------
  // Todo lo hace la Pi con /usr/local/bin/percy-wifi (ver orangepi/wifi/).
  // Cambiar de red corta ESTA conexion (el celular esta en la red vieja):
  // por eso cada accion avisa a que red conectarse despues. Si una conexion
  // falla, la Pi vuelve a la red anterior o crea la red propia sola.
  const AP_IP = "10.42.0.1";
  const wifiStatusEl = document.getElementById("wifi-status");
  const wifiScanListEl = document.getElementById("wifi-scan-list");
  const wifiSavedListEl = document.getElementById("wifi-saved-list");
  let wifiNets = [];
  let wifiSaved = [];

  async function wifiGet(path) {
    const res = await fetch(path);
    return res.json();
  }

  async function wifiPost(path, body) {
    const res = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
    });
    return res.json();
  }

  function wifiNotice(msg) {
    wifiStatusEl.className = "";
    wifiStatusEl.innerHTML = escapeHtml(msg);
  }

  // --- IP del rover en la barra superior (24-sept) ------------------------
  // En la red de la casa la IP la da el router y cambia -> se lee de la Pi
  // cada 10s. En la red propia es siempre 10.42.0.1. Se recuerda la ultima
  // IP de cada red en el celular (localStorage), para tenerla a mano al
  // volver a esa red. localStorage puede fallar (modo privado): no es grave.
  const roverIpEl = document.getElementById("rover-ip");
  const wifiKnownEl = document.getElementById("wifi-known");

  function loadKnownIps() {
    try { return JSON.parse(localStorage.getItem("percy-ips") || "{}"); } catch (e) { return {}; }
  }

  function saveKnownIp(ssid, ip) {
    try {
      const known = loadKnownIps();
      if (known[ssid] === ip) return;
      known[ssid] = ip;
      localStorage.setItem("percy-ips", JSON.stringify(known));
    } catch (e) {
      // sin almacenamiento en este navegador
    }
  }

  function renderKnownIps() {
    const rows = Object.entries(loadKnownIps()).map(([ssid, ip]) =>
      `${escapeHtml(ssid)} &rarr; <b>${escapeHtml(ip)}</b>`);
    rows.push(`RED PROPIA &rarr; <b>${AP_IP}</b> (fija)`);
    wifiKnownEl.innerHTML = `ÚLTIMA IP POR RED:<br>${rows.join("<br>")}`;
  }

  function applyRoverIp(s) {
    if (s && s.mode === "ap") {
      roverIpEl.textContent = AP_IP;
      roverIpEl.className = "ap";
    } else if (s && s.mode === "client" && s.ip) {
      roverIpEl.textContent = s.ip;
      roverIpEl.className = "";
      saveKnownIp(s.ssid, s.ip);
    } else {
      roverIpEl.textContent = location.hostname || "--";
      roverIpEl.className = "";
    }
  }

  async function pollRoverIp() {
    try {
      applyRoverIp(await wifiGet("/wifi/status"));
    } catch (e) {
      // sin conexion momentanea - se reintenta solo
    }
  }
  pollRoverIp();
  setInterval(pollRoverIp, 10000);

  async function refreshWifiStatus() {
    try {
      const s = await wifiGet("/wifi/status");
      applyRoverIp(s);
      renderKnownIps();
      if (s && s.ok === false) { wifiNotice(s.msg); return; }
      wifiStatusEl.className = s.mode;
      if (s.mode === "client") {
        wifiStatusEl.innerHTML = `CONECTADO A <b>${escapeHtml(s.ssid)}</b><br><span class="dim">${escapeHtml(s.ip)}:8000</span>`;
      } else if (s.mode === "ap") {
        wifiStatusEl.innerHTML = `RED PROPIA <b>${escapeHtml(s.ssid)}</b><br><span class="dim">${AP_IP}:8000</span>`;
      } else {
        wifiStatusEl.innerHTML = "SIN RED &mdash; EN ~1 MIN SE CREA LA RED PROPIA";
      }
    } catch (e) {
      // se perdio la conexion (normal justo despues de cambiar de red)
    }
  }

  async function refreshWifiSaved() {
    try {
      const list = await wifiGet("/wifi/saved");
      wifiSaved = Array.isArray(list) ? list : [];
    } catch (e) {
      return;
    }
    if (!wifiSaved.length) {
      wifiSavedListEl.innerHTML = `<span class="wifi-empty">NINGUNA</span>`;
      return;
    }
    wifiSavedListEl.innerHTML = wifiSaved.map((n, i) => `
      <div class="wifi-item">
        <button class="wifi-net" data-i="${i}"><span>${escapeHtml(n.ssid)}</span><b>CONECTAR</b></button>
        <button class="wifi-del" data-i="${i}">&times;</button>
      </div>`).join("");
    wifiSavedListEl.querySelectorAll(".wifi-net").forEach((btn) => {
      btn.addEventListener("click", () => connectWifi(wifiSaved[+btn.dataset.i].ssid, true, true));
    });
    wifiSavedListEl.querySelectorAll(".wifi-del").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const ssid = wifiSaved[+btn.dataset.i].ssid;
        if (!window.confirm(`Olvidar la red "${ssid}"? (si es la red actual, la conexión se corta)`)) return;
        const r = await wifiPost("/wifi/forget", { ssid });
        wifiNotice(r.msg || "");
        refreshWifiSaved();
      });
    });
  }

  function renderWifiScan() {
    if (!wifiNets.length) {
      wifiScanListEl.innerHTML = `<span class="wifi-empty">NO SE VEN REDES (CON LA RED PROPIA ACTIVA A VECES NO SE PUEDE BUSCAR)</span>`;
      return;
    }
    wifiScanListEl.innerHTML = wifiNets.map((n, i) => `
      <div class="wifi-item${n.active ? " active" : ""}">
        <button class="wifi-net" data-i="${i}"><span>${escapeHtml(n.ssid)}</span><b>${n.signal}%${n.secure ? " ·WPA" : ""}${n.saved ? " ·G" : ""}</b></button>
      </div>`).join("");
    wifiScanListEl.querySelectorAll(".wifi-net").forEach((btn) => {
      btn.addEventListener("click", () => {
        const n = wifiNets[+btn.dataset.i];
        if (n.active) return;
        connectWifi(n.ssid, n.secure, n.saved);
      });
    });
  }

  async function connectWifi(ssid, secure, saved) {
    let password = "";
    if (secure && !saved) {
      const pw = window.prompt(`Clave de "${ssid}" (se guarda en el rover):`);
      if (pw === null) return;
      if (pw.length < 8 || pw.length > 63) { window.alert("La clave debe tener entre 8 y 63 caracteres."); return; }
      password = pw;
    }
    if (!window.confirm(
      `El rover se cambia a "${ssid}" y esta conexión se corta.\n\n` +
      `Conecta el celular a "${ssid}" y abre la IP nueva del rover.\n` +
      `Si no logra conectarse, vuelve a la red anterior o crea su red propia (${AP_IP}).\n\n¿Continuar?`)) return;
    const r = await wifiPost("/wifi/connect", { ssid, password });
    wifiNotice(r.msg || "");
  }

  document.getElementById("wifi-scan").addEventListener("click", async () => {
    wifiScanListEl.innerHTML = `<span class="wifi-empty">BUSCANDO&hellip;</span>`;
    try {
      const list = await wifiGet("/wifi/scan");
      wifiNets = Array.isArray(list) ? list : [];
    } catch (e) {
      wifiNets = [];
    }
    renderWifiScan();
  });

  document.getElementById("wifi-disconnect").addEventListener("click", async () => {
    if (!window.confirm("El rover se desconecta de la red actual y en ~1 minuto crea su red propia. ¿Continuar?")) return;
    const r = await wifiPost("/wifi/disconnect");
    wifiNotice(r.msg || "");
  });

  document.getElementById("wifi-ap-on").addEventListener("click", async () => {
    const ssid = window.prompt("Nombre de la red propia (vacío = dejar el actual):", "");
    if (ssid === null) return;
    let password = "";
    if (ssid.trim()) {
      const pw = window.prompt(`Clave para "${ssid.trim()}" (8 a 63 caracteres):`);
      if (pw === null) return;
      if (pw.length < 8 || pw.length > 63) { window.alert("La clave debe tener entre 8 y 63 caracteres."); return; }
      password = pw;
    }
    if (!window.confirm(
      `El rover crea su red propia y esta conexión se corta.\n\n` +
      `Conecta el celular a esa red y abre http://${AP_IP}:8000\n\n¿Continuar?`)) return;
    const r = await wifiPost("/wifi/ap", { ssid: ssid.trim(), password });
    wifiNotice(r.msg || "");
  });

  document.getElementById("wifi-ap-off").addEventListener("click", async () => {
    if (!window.confirm(
      "Se apaga la red propia y el rover busca tus redes guardadas.\n" +
      "Si no encuentra ninguna, en ~1 minuto vuelve a crear la red propia. ¿Continuar?")) return;
    const r = await wifiPost("/wifi/ap/off");
    wifiNotice(r.msg || "");
  });

  document.getElementById("btn-wifi").addEventListener("click", () => {
    if (toggleMenu(wifiWidget, recWidget)) {
      refreshWifiStatus();
      refreshWifiSaved();
    }
  });

  setInterval(() => {
    if (wifiWidget.classList.contains("panel-open")) refreshWifiStatus();
  }, 4000);

  saveBtn.addEventListener("click", () => {
    const name = window.prompt("Nombre de la posición actual (ej. \"bajar garra\"):");
    if (!name || !name.trim()) return;
    state.preset_save = name.trim();
    setTimeout(fetchPresets, 400);
  });

  // Parpadeo verde mientras el brazo viaja solo hacia un preset, y lectura
  // en vivo de los angulos - solo se consulta si el panel ya esta
  // destrabado, no vale la pena antes de eso.
  setInterval(async () => {
    if (!recArmed) return;
    try {
      const res = await fetch("/status");
      const data = await res.json();
      recWidget.classList.toggle("moving", !!data.preset_moving);
    } catch (e) {
      // sin conexion momentanea - se reintenta solo
    }
    try {
      const res = await fetch("/servos");
      const data = await res.json();
      angleReadoutEl.innerHTML = Object.entries(ANGLE_LABELS).map(([key, label]) => {
        const val = data.angles && data.angles[key];
        return `<span>${label}<b>${val != null ? val.toFixed(0) : "--"}&deg;</b></span>`;
      }).join("");
    } catch (e) {
      // sin conexion momentanea - se reintenta solo
    }
  }, 500);
})();
