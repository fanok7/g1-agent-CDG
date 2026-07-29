"""
dashboard.py — Superviseur central du G1
Remplace launch.py — seul programme à ouvrir /dev/ttyUSB0
Accessible sur http://192.168.0.128:8888

Démarrage auto : crontab unitree
@reboot sleep 25 && cd /home/unitree/g1_agent_interim && /usr/bin/python3.8 dashboard.py >> /home/unitree/dashboard.log 2>&1
"""
import subprocess, threading, collections, os, sys, time, json, socket as sock_module
import serial
from flask import Flask, request, jsonify, render_template_string
from flask_sock import Sock

sys.path.insert(0, '/home/unitree/g1_agent_interim')

app  = Flask(__name__)
sock = Sock(app)

BASE_DIR = '/home/unitree/g1_agent_interim'
PORT_SERIE = '/dev/ttyUSB0'
BAUD = 115200
SOCKET_HOST = '127.0.0.1'
SOCKET_PORT = 9876

SCRIPTS = {
    "main":     ["/usr/bin/python3.8", f"{BASE_DIR}/main.py"],
    "vision":   ["/usr/bin/python3.8", f"{BASE_DIR}/vision/vision_server.py"],
    "damping":  ["/usr/bin/python3.8", f"{BASE_DIR}/robot/mode_damping.py"],
    "seating":  ["/usr/bin/python3.8", f"{BASE_DIR}/robot/mode_seating.py"],
    "standing": ["/usr/bin/python3.8", f"{BASE_DIR}/robot/mode_standing.py"],
    "regular":  ["/usr/bin/python3.8", f"{BASE_DIR}/robot/mode_regular.py"],
}

_processus   = {}
_lock        = threading.Lock()
_log_buffer  = collections.deque(maxlen=500)
_ws_clients  = set()
_ws_lock     = threading.Lock()
_ser         = None
_ser_lock    = threading.Lock()

# Stats session
_stats = {
    "conversations": 0,
    "outils": 0,
    "personnes": set(),
    "alertes_feu": 0,
    "alertes_chute": 0,
    "demarrage": time.time(),
}
# Historique conversations
_conversations = collections.deque(maxlen=200)
# Historique capteurs
_capteurs_hist = collections.deque(maxlen=50)


def broadcast(line: str):
    global _ws_clients
    _log_buffer.append(line)
    # Mise à jour stats depuis les logs
    if "[Toi]" in line and "Parle..." not in line:
        _stats["conversations"] += 1
    if "[TOOL] Appel" in line:
        _stats["outils"] += 1
    if "[FALL]" in line:
        _stats["alertes_chute"] += 1
    if "[FIRE]" in line:
        _stats["alertes_feu"] += 1
    if "[FACE]" in line and "reconnue" in line:
        try:
            nom = line.split(":")[-1].strip().split("→")[0].strip()
            if nom:
                _stats["personnes"].add(nom)
        except Exception:
            pass
    # Conversations
    if line.startswith("[Toi]") or line.startswith("[G1]"):
        _conversations.append({"ts": time.time(), "line": line})
    with _ws_lock:
        dead = set()
        for client in _ws_clients:
            try:
                client.send(line)
            except Exception:
                dead.add(client)
        _ws_clients -= dead


def log(msg: str):
    print(msg, flush=True)
    broadcast(msg)


def init_serie():
    global _ser
    try:
        _ser = serial.Serial(PORT_SERIE, BAUD, timeout=0.1)
        log(f"[Dashboard] ESP32 connecté sur {PORT_SERIE}")
        time.sleep(2)
    except Exception as e:
        _ser = None
        log(f"[Dashboard] ESP32 non disponible : {e}")


def send_emotion(emotion: str):
    if _ser is None:
        return
    try:
        with _ser_lock:
            payload = json.dumps({"cmd": "emotion", "value": emotion}) + "\n"
            _ser.write(payload.encode())
    except Exception as e:
        log(f"[Dashboard] Erreur envoi emotion : {e}")


def repondre_serie(payload: dict):
    if _ser is None:
        return
    try:
        with _ser_lock:
            _ser.write((json.dumps(payload) + "\n").encode())
    except Exception as e:
        log(f"[Dashboard] Erreur reponse serie : {e}")


def lancer(nom):
    if nom not in SCRIPTS:
        return {"ok": False, "error": "script inconnu"}
    proc_existant = _processus.get(nom)
    if proc_existant and proc_existant.poll() is None:
        return {"ok": False, "error": "deja lance"}
    proc = subprocess.Popen(
        SCRIPTS[nom],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=BASE_DIR
    )
    _processus[nom] = proc
    threading.Thread(target=_pipe_logs, args=(proc, nom), daemon=True).start()
    log(f"[Dashboard] Lance {nom} (PID {proc.pid})")
    return {"ok": True, "pid": proc.pid, "script": nom}


def arreter(nom):
    proc = _processus.get(nom)
    if proc and proc.poll() is None:
        proc.terminate()
        log(f"[Dashboard] Arrete {nom}")
        return {"ok": True, "script": nom}
    return {"ok": False, "error": "pas en cours"}


def statut():
    result = {}
    for nom in SCRIPTS:
        proc = _processus.get(nom)
        if proc and proc.poll() is None:
            result[nom] = True
        else:
            try:
                out = subprocess.run(
                    ['pgrep', '-f', SCRIPTS[nom][-1]],
                    capture_output=True
                )
                result[nom] = out.returncode == 0
            except Exception:
                result[nom] = False
    return result


def _pipe_logs(proc, nom):
    for raw in proc.stdout:
        line = raw.decode(errors='replace').rstrip()
        broadcast(f"[{nom}] {line}")
    broadcast(f"[Dashboard] {nom} terminé (code {proc.returncode})")


_RETRY_INTERVAL_S = 30


def _ecoute_serie():
    global _ser
    next_retry = 0.0
    while True:
        if _ser is None:
            # Retente une connexion toutes les 30s tant que l'ESP32 n'est pas
            # branché (au démarrage OU après une perte) — avant ce correctif,
            # une seule tentative échouée laissait _ser à None pour toujours,
            # sans jamais retenter même si l'ESP32 était rebranché plus tard.
            now = time.time()
            if now >= next_retry:
                init_serie()
                next_retry = time.time() + _RETRY_INTERVAL_S
            time.sleep(0.5)
            continue
        if not _ser.in_waiting:
            time.sleep(0.05)
            continue
        try:
            with _ser_lock:
                ligne = _ser.readline().decode(errors='ignore').strip()
            if not ligne:
                continue
            data = json.loads(ligne)
            event = data.get("event")
            if event == "run":
                rep = lancer(data.get("script"))
                repondre_serie(rep)
            elif event == "stop":
                rep = arreter(data.get("script"))
                repondre_serie(rep)
            elif event == "status":
                repondre_serie({"ok": True, "statuts": statut()})
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        except (serial.SerialException, OSError) as e:
            log(f"[Dashboard] Perte ESP32 : {e}")
            _ser = None
            next_retry = time.time() + _RETRY_INTERVAL_S
        except Exception as e:
            log(f"[Dashboard] Erreur serie : {e}")


def _serveur_socket():
    srv = sock_module.socket(sock_module.AF_INET, sock_module.SOCK_STREAM)
    srv.setsockopt(sock_module.SOL_SOCKET, sock_module.SO_REUSEADDR, 1)
    srv.bind((SOCKET_HOST, SOCKET_PORT))
    srv.listen(5)
    log(f"[Dashboard] Socket emotions sur {SOCKET_HOST}:{SOCKET_PORT}")
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_gerer_emotion, args=(conn,), daemon=True).start()


def _gerer_emotion(conn):
    with conn:
        try:
            data = conn.recv(1024).decode(errors='ignore').strip()
            if not data:
                return
            payload = json.loads(data)
            emotion = payload.get("emotion")
            if emotion:
                send_emotion(emotion)
                conn.sendall(b'{"ok": true}')
        except Exception:
            pass


def _surveille_capteurs():
    fichiers = {
        "face_id":  "/tmp/face_id_state.json",
        "vision":   "/tmp/vision_state.json",
        "chute":    "/tmp/fall_state.json",
        "feu":      "/tmp/fire_state.json",
        "qr":       "/tmp/qr_state.json",
        "agent":    "/tmp/agent_responding",
    }

    def usb_present(vid_pid):
        try:
            out = subprocess.run(["lsusb"], capture_output=True, text=True)
            return vid_pid.lower() in out.stdout.lower()
        except Exception:
            return False

    def video_actif(device):
        return os.path.exists(device)

    def audio_actif(card_name):
        try:
            out = subprocess.run(["arecord", "-l"], capture_output=True, text=True)
            return card_name.lower() in out.stdout.lower()
        except Exception:
            return False

    while True:
        snapshot = {}
        for nom, path in fichiers.items():
            if os.path.exists(path):
                try:
                    if nom == "agent":
                        snapshot[nom] = True
                    else:
                        with open(path) as f:
                            snapshot[nom] = json.load(f)
                except Exception:
                    snapshot[nom] = None
            else:
                snapshot[nom] = None

        snapshot["hw"] = {
            "micro":     audio_actif("USB MIC/INPUT Adapter"),
            "ugreen":    video_actif("/dev/video6"),
            "realsense": video_actif("/dev/video0"),
            "esp32":     usb_present("10c4:ea60"),
        }

        _capteurs_hist.append({"ts": time.time(), "data": snapshot})
        time.sleep(3)


DASHBOARD_HTML = """<!DOCTYPE html><html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>G1 Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'SF Mono',monospace;background:#080808;color:#ffffff;height:100vh;display:grid;grid-template-rows:48px 40px 1fr;overflow:hidden}
header{background:#0f0f0f;border-bottom:1px solid #1c1c1c;display:flex;align-items:center;padding:0 16px;gap:10px}
.dot{width:8px;height:8px;border-radius:50%;background:#e74c3c;flex-shrink:0;transition:background .3s}
.dot.on{background:#2ecc71}
h1{font-size:13px;color:#e8e8e8;font-family:system-ui;font-weight:500}
.meta{margin-left:auto;font-size:11px;color:#888;font-family:system-ui}
.tabs{background:#0a0a0a;border-bottom:1px solid #161616;display:flex}
.tab{font-size:12px;font-family:system-ui;color:#888;padding:0 20px;height:40px;display:flex;align-items:center;cursor:pointer;border-bottom:2px solid transparent;gap:6px;transition:color .2s}
.tab:hover{color:#888}
.tab.active{color:#e8e8e8;border-bottom-color:#3498db}
.badge-tab{font-size:10px;background:#1a1a1a;padding:1px 6px;border-radius:10px;color:#888}
.tab.active .badge-tab{background:#1a2a3a;color:#3498db}
.panel{display:none;height:100%;overflow:hidden}
.panel.active{display:grid}

/* Taches */
#p-taches{grid-template-columns:260px 1fr}
.sidebar{background:#0a0a0a;border-right:1px solid #161616;overflow-y:auto;display:flex;flex-direction:column}
.sec{border-bottom:1px solid #111;padding:12px 14px}
.sec-title{font-size:10px;color:#555;font-family:system-ui;text-transform:uppercase;letter-spacing:.12em;margin-bottom:10px}
.prog-row{display:flex;align-items:center;gap:6px;margin-bottom:8px}
.prog-name{font-size:12px;font-family:system-ui;color:#ffffff;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{font-size:10px;padding:2px 7px;border-radius:3px;font-family:system-ui;flex-shrink:0}
.badge.on{background:#0a1f0a;color:#2ecc71;border:1px solid #1a4a1a}
.badge.off{background:#111;color:#333;border:1px solid #1a1a1a}
.btn{font-size:10px;padding:3px 8px;border-radius:3px;border:1px solid #222;background:#111;color:#cccccc;cursor:pointer;font-family:system-ui;flex-shrink:0}
.btn:hover{border-color:#555;color:#ffffff}
.btn.danger{border-color:#2a0a0a;color:#8b2020}
.btn.danger:hover{border-color:#4a1a1a;color:#e74c3c}
.btn.warn{border-color:#2a1a00;color:#7a5000}
.btn.warn:hover{border-color:#4a3000;color:#e67e22}
.btn.primary{border-color:#0a2040;color:#1a5f9a}
.btn.primary:hover{border-color:#1a4060;color:#3498db}
.logs-area{overflow-y:auto;padding:10px 14px;display:flex;flex-direction:column;gap:1px;background:#040404}
.log{font-size:11px;line-height:1.65;white-space:pre-wrap;word-break:break-all}
.log.g1{color:#2980b9}.log.toi{color:#27ae60}.log.err{color:#c0392b}
.log.tool{color:#d68910}.log.sys{color:#7d3c98}.log.dash{color:#148f77}.log.other{color:#cccccc}
.log-filter{display:flex;gap:6px;padding:8px 14px;background:#060606;border-bottom:1px solid #111;flex-wrap:wrap}
.filter-btn{font-size:10px;padding:2px 8px;border-radius:10px;border:1px solid #2a2a2a;background:#0a0a0a;color:#aaaaaa;cursor:pointer;font-family:system-ui}
.filter-btn.active{border-color:#555;color:#ffffff;background:#1a1a1a}

/* Stats sidebar */
.stat-card{padding:10px 14px;border-bottom:1px solid #111}
.stat-label{font-size:10px;color:#666;font-family:system-ui;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}
.stat-val{font-size:22px;color:#e8e8e8;font-weight:500;line-height:1}
.stat-sub{font-size:10px;color:#888;font-family:system-ui;margin-top:2px}

/* Robot */
#p-robot{grid-template-columns:260px 1fr}
.mode-grid{padding:16px;overflow-y:auto;display:grid;grid-template-columns:1fr 1fr;gap:10px;align-content:start}
.mode-card{background:#0d0d0d;border:1px solid #181818;border-radius:6px;padding:14px}
.mode-card h3{font-size:12px;font-family:system-ui;color:#ffffff;font-weight:500;margin-bottom:4px}
.mode-card p{font-size:11px;color:#888;font-family:system-ui;margin-bottom:10px;line-height:1.5}
.mode-card.warn{border-color:#2a1a00}
.mode-card h3.warn{color:#e67e22}

/* Capteurs */
#p-capteurs{grid-template-columns:1fr}
.capteurs-grid{padding:16px;overflow-y:auto;display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;align-content:start}
.capteur-card{background:#0d0d0d;border:1px solid #181818;border-radius:6px;padding:12px}
.capteur-card.alerte{border-color:#4a1a1a;background:#0f0808}
.capteur-title{font-size:11px;font-family:system-ui;color:#cccccc;margin-bottom:6px;display:flex;align-items:center;gap:6px}
.capteur-val{font-size:12px;color:#ffffff;font-family:system-ui;line-height:1.5}
.capteur-val.ok{color:#2ecc71}.capteur-val.off{color:#333}.capteur-val.alert{color:#e74c3c}
.capteur-ts{font-size:10px;color:#888;font-family:system-ui;margin-top:4px}

/* Conversations */
#p-conv{grid-template-columns:1fr}
.conv-area{padding:16px;overflow-y:auto;display:flex;flex-direction:column;gap:8px}
.conv-msg{display:flex;gap:10px;align-items:flex-start}
.conv-msg.toi .bubble{background:#0d1f0d;border:1px solid #1a3a1a;color:#2ecc71}
.conv-msg.g1 .bubble{background:#0a1525;border:1px solid #1a3050;color:#3498db}
.bubble{font-size:12px;font-family:system-ui;padding:8px 12px;border-radius:6px;max-width:80%;line-height:1.5}
.conv-who{font-size:10px;color:#888;font-family:system-ui;padding-top:6px;flex-shrink:0;width:36px;text-align:right}
.conv-ts{font-size:10px;color:#666;font-family:system-ui;padding-top:6px}
</style>
</head>
<body>
<header>
  <div class="dot" id="ws-dot"></div>
  <h1>G1 Robot — Dashboard</h1>
  <span class="meta" id="uptime">—</span>
</header>

<div class="tabs">
  <div class="tab active" onclick="tab('taches')" id="t-taches">Tâches <span class="badge-tab" id="tb-err">0 err</span></div>
  <div class="tab" onclick="tab('robot')" id="t-robot">Robot</div>
  <div class="tab" onclick="tab('capteurs')" id="t-capteurs">Capteurs <span class="badge-tab" id="tb-alert">0</span></div>
  <div class="tab" onclick="tab('conv')" id="t-conv">Conversations <span class="badge-tab" id="tb-conv">0</span></div>
</div>

<!-- TACHES -->
<div class="panel active" id="p-taches">
  <div class="sidebar">
    <div class="stat-card">
      <div class="stat-label">Conversations</div>
      <div class="stat-val" id="st-conv">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Outils appelés</div>
      <div class="stat-val" id="st-outils">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Personnes reconnues</div>
      <div class="stat-val" id="st-pers">0</div>
      <div class="stat-sub" id="st-pers-names">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Alertes</div>
      <div class="stat-val" id="st-alertes">0</div>
      <div class="stat-sub" id="st-alertes-det">feu: 0 — chute: 0</div>
    </div>
    <div class="sec" style="margin-top:auto">
      <div class="sec-title">Programmes</div>
      <div class="prog-row">
        <span class="prog-name">main.py</span>
        <span class="badge off" id="s-main">—</span>
        <button class="btn primary" onclick="run('main')">Lancer</button>
        <button class="btn danger" onclick="stop('main')">Stop</button>
      </div>
      <div class="prog-row">
        <span class="prog-name">vision</span>
        <span class="badge off" id="s-vision">—</span>
        <button class="btn primary" onclick="run('vision')">Lancer</button>
        <button class="btn danger" onclick="stop('vision')">Stop</button>
      </div>
    </div>
    <div class="sec">
      <div class="sec-title">Actions</div>
      <div class="prog-row">
        <button class="btn" style="width:100%" onclick="clearLogs()">Vider les logs</button>
      </div>
    </div>
  </div>
  <div style="display:grid;grid-template-rows:36px 1fr;overflow:hidden">
    <div class="log-filter">
      <button class="filter-btn active" onclick="setFilter('all',this)">Tout</button>
      <button class="filter-btn" onclick="setFilter('g1',this)">[G1]</button>
      <button class="filter-btn" onclick="setFilter('toi',this)">[Toi]</button>
      <button class="filter-btn" onclick="setFilter('tool',this)">[TOOL]</button>
      <button class="filter-btn" onclick="setFilter('err',this)">Erreurs</button>
      <button class="filter-btn" onclick="setFilter('dash',this)">[Dashboard]</button>
    </div>
    <div class="logs-area" id="logs"></div>
  </div>
</div>

<!-- ROBOT -->
<div class="panel" id="p-robot">
  <div class="sidebar">
    <div class="sec">
      <div class="sec-title">État</div>
      <div class="prog-row">
        <span class="prog-name">Moteurs</span>
        <span class="badge" id="r-moteurs">—</span>
      </div>
      <div class="prog-row">
        <span class="prog-name">Dernier mode</span>
        <span style="font-size:11px;color:#cccccc;font-family:system-ui" id="r-mode">—</span>
      </div>
    </div>
    <div class="sec">
      <div class="sec-title">Bouche OLED</div>
      <div class="prog-row">
        <button class="btn" onclick="emotion('content')" style="flex:1">Sourire</button>
        <button class="btn" onclick="emotion('parle')" style="flex:1">Parle</button>
        <button class="btn" onclick="emotion('surpris')" style="flex:1">Surpris</button>
      </div>
    </div>
  </div>
  <div class="mode-grid">
    <div class="mode-card">
      <h3>Damping</h3>
      <p>Moteurs en amortissement. Position de sécurité. FSM 1.</p>
      <button class="btn primary" onclick="run('damping')">Activer</button>
    </div>
    <div class="mode-card">
      <h3>Seating</h3>
      <p>Le robot s'assoit. FSM 3.</p>
      <button class="btn primary" onclick="run('seating')">Activer</button>
    </div>
    <div class="mode-card">
      <h3>Locked Standing</h3>
      <p>Damp → FSM 4 → Start. Depuis Seating ou Damp. ~10s.</p>
      <button class="btn primary" onclick="run('standing')">Activer</button>
    </div>
    <div class="mode-card warn">
      <h3 class="warn">Regular ⚠️</h3>
      <p>FSM 501 — 3 DoF Waist. Depuis Locked Standing uniquement.</p>
      <button class="btn warn" onclick="run('regular')">Activer</button>
    </div>
  </div>
</div>

<!-- CAPTEURS -->
<div class="panel" id="p-capteurs">
  <div class="capteurs-grid" id="capteurs-grid">
    <div class="capteur-card"><div class="capteur-title">Chargement...</div></div>
  </div>
</div>

<!-- CONVERSATIONS -->
<div class="panel" id="p-conv">
  <div class="conv-area" id="conv-area"></div>
</div>

<script>
let currentFilter = 'all';
let errCount = 0;
let convCount = 0;
let alertCount = 0;

function tab(name) {
  ['taches','robot','capteurs','conv'].forEach(n => {
    document.getElementById('p-'+n).classList.toggle('active', n===name);
    document.getElementById('t-'+n).classList.toggle('active', n===name);
  });
}

function colorClass(line) {
  if (line.includes('[G1]') && !line.includes('[main]') && !line.includes('[vision]')) return 'g1';
  if (line.includes('[Toi]')) return 'toi';
  if (line.includes('Error')||line.includes('Traceback')||line.includes('ERREUR')) return 'err';
  if (line.includes('[TOOL]')) return 'tool';
  if (line.includes('[Dashboard]')||line.includes('[Launch]')) return 'dash';
  if (line.includes('[HARDWARE]')||line.includes('[AGENT]')||line.includes('[VISION]')||
      line.includes('[face_id]')||line.includes('[Mode]')||line.includes('[GESTE]')) return 'sys';
  return 'other';
}

function matchFilter(cls) {
  if (currentFilter === 'all') return true;
  if (currentFilter === 'err') return cls === 'err';
  return cls === currentFilter;
}

function setFilter(f, btn) {
  currentFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('#logs .log').forEach(el => {
    el.style.display = matchFilter(el.dataset.cls) ? '' : 'none';
  });
}

function addLog(line) {
  const logs = document.getElementById('logs');
  const cls = colorClass(line);
  if (cls === 'err') {
    errCount++;
    document.getElementById('tb-err').textContent = errCount + ' err';
  }
  const div = document.createElement('div');
  div.className = 'log ' + cls;
  div.dataset.cls = cls;
  div.textContent = line;
  div.style.display = matchFilter(cls) ? '' : 'none';
  logs.appendChild(div);
  if (logs.children.length > 1000) logs.removeChild(logs.firstChild);
  logs.scrollTop = logs.scrollHeight;

  // Conversations
  if (line.startsWith('[Toi]') && !line.includes('Parle...')) {
    addConv('toi', line.replace('[Toi] ',''));
  }
  if (line.startsWith('[G1]') && !line.includes('Parle...')&&!line.includes('Écoute...')) {
    addConv('g1', line.replace('[G1] ',''));
  }
}

function addConv(who, text) {
  const area = document.getElementById('conv-area');
  const now = new Date().toLocaleTimeString();
  const div = document.createElement('div');
  div.className = 'conv-msg ' + who;
  div.innerHTML = `<span class="conv-who">${who==='toi'?'Toi':'G1'}</span>
    <div class="bubble">${text}</div>
    <span class="conv-ts">${now}</span>`;
  area.appendChild(div);
  area.scrollTop = area.scrollHeight;
  convCount++;
  document.getElementById('tb-conv').textContent = convCount;
}

function clearLogs() {
  document.getElementById('logs').innerHTML = '';
  errCount = 0;
  document.getElementById('tb-err').textContent = '0 err';
}

async function run(s) {
  const r = await fetch('/api/run?script=' + s);
  const d = await r.json();
  addLog('[Dashboard] run ' + s + ' → ' + (d.ok ? 'OK PID ' + d.pid : d.error));
  if (['damping','seating','standing','regular'].includes(s)) {
    document.getElementById('r-mode').textContent = s;
  }
}

async function stop(s) {
  const r = await fetch('/api/stop?script=' + s);
  const d = await r.json();
  addLog('[Dashboard] stop ' + s + ' → ' + (d.ok ? 'OK' : d.error));
}

async function emotion(e) {
  await fetch('/api/emotion?value=' + e);
  addLog('[Dashboard] emotion → ' + e);
}

async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    for (const [name, running] of Object.entries(d)) {
      const el = document.getElementById('s-' + name);
      if (!el) continue;
      el.textContent = running ? 'actif' : 'arrêté';
      el.className = 'badge ' + (running ? 'on' : 'off');
    }
  } catch(e) {}
}

async function refreshStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    document.getElementById('st-conv').textContent = d.conversations;
    document.getElementById('st-outils').textContent = d.outils;
    document.getElementById('st-pers').textContent = d.personnes;
    document.getElementById('st-pers-names').textContent = d.noms || '—';
    const al = d.alertes_feu + d.alertes_chute;
    document.getElementById('st-alertes').textContent = al;
    document.getElementById('st-alertes-det').textContent = `feu: ${d.alertes_feu} — chute: ${d.alertes_chute}`;
    if (al > alertCount) {
      alertCount = al;
      document.getElementById('tb-alert').textContent = al;
    }
    if (d.uptime) document.getElementById('uptime').textContent = d.uptime;
  } catch(e) {}
}

async function refreshCapteurs() {
  try {
    const r = await fetch('/api/capteurs');
    const d = await r.json();
    const grid = document.getElementById('capteurs-grid');
    grid.innerHTML = '';

    // Section hardware
    const hw = d.hw || {};
    const hwDiv = document.createElement('div');
    hwDiv.style.cssText = 'grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin-bottom:14px';
    const hwTitle = document.createElement('div');
    hwTitle.style.cssText = 'grid-column:1/-1;font-size:10px;color:#555;font-family:system-ui;text-transform:uppercase;letter-spacing:.12em;margin-bottom:2px';
    hwTitle.textContent = 'Hardware';
    hwDiv.appendChild(hwTitle);
    const hwItems = [
      {key:'micro',     label:'Micro',            icon:'🎤', model:'GPT-4o Realtime API', device:'KTMicro USB — 31b2:0111',    val:hw.micro},
      {key:'ugreen',    label:'Caméra UGREEN 2K', icon:'📷', model:'YOLO v8 + InsightFace', device:'/dev/video6-7 — 0c45:636f', val:hw.ugreen},
      {key:'realsense', label:'RealSense D435i',  icon:'📡', model:'QR/PDF417/Aztec scan',  device:'/dev/video0-5 — 8086:0b3a', val:hw.realsense},
      {key:'esp32',     label:'ESP32 Bouche OLED',icon:'🤖', model:'U8g2 SH1106 1.3"',      device:'CP210x — 10c4:ea60 /dev/ttyUSB0', val:hw.esp32},
    ];
    hwItems.forEach(item => {
      const card = document.createElement('div');
      card.className = 'capteur-card' + (item.val ? '' : ' alerte');
      card.innerHTML = `
        <div class="capteur-title">${item.icon} ${item.label}</div>
        <div class="capteur-val ${item.val ? 'ok' : 'alert'}">${item.val ? '● Branché' : '○ Débranché'}</div>
        <div class="capteur-ts">${item.model}</div>
        <div class="capteur-ts" style="color:#444;margin-top:2px">${item.device}</div>`;
      hwDiv.appendChild(card);
    });
    grid.appendChild(hwDiv);

    // Separateur
    const sep = document.createElement('div');
    sep.style.cssText = 'grid-column:1/-1;border-top:1px solid #111;margin:4px 0 10px';
    const sepTitle = document.createElement('div');
    sepTitle.style.cssText = 'font-size:10px;color:#555;font-family:system-ui;text-transform:uppercase;letter-spacing:.12em;margin-bottom:2px;margin-top:8px';
    sepTitle.textContent = 'IA & Détections';
    grid.appendChild(sep);
    grid.appendChild(sepTitle);

    // Section IA
    const aiItems = [
      {key:'agent',   label:'Agent GPT',         icon:'🧠', model:'GPT-4o Realtime API',
        detail: d.agent ? 'En train de parler' : 'En écoute'},
      {key:'face_id', label:'Reconnaissance visage', icon:'👤', model:'InsightFace buffalo_sc — UGREEN',
        detail: d.face_id?.faces ? (d.face_id.faces.map(f=>f.name).join(', ') || '—') : '—'},
      {key:'vision',  label:'Détection objets',  icon:'👁', model:'YOLO v8 — UGREEN /dev/video6',
        detail: d.vision ? 'Actif' : 'Inactif'},
      {key:'chute',   label:'Détection chute',   icon:'🆘', model:'YOLO Pose — fall_detection',
        detail: d.chute ? '⚠️ Chute détectée !' : 'RAS'},
      {key:'feu',     label:'Détection feu',     icon:'🔥', model:'YOLO fire_detection',
        detail: d.feu ? '⚠️ Feu détecté !' : 'RAS'},
      {key:'qr',      label:'QR / Billets',      icon:'📋', model:'pyzbar — RealSense /dev/video0',
        detail: d.qr ? (d.qr.passager || d.qr.raw || 'Scanné') : 'Aucun scan'},
    ];
    aiItems.forEach(item => {
      const actif = d[item.key] !== null && d[item.key] !== false && d[item.key] !== undefined;
      const alerte = ['chute','feu'].includes(item.key) && actif && d[item.key];
      const card = document.createElement('div');
      card.className = 'capteur-card' + (alerte ? ' alerte' : '');
      card.innerHTML = `
        <div class="capteur-title">${item.icon} ${item.label}</div>
        <div class="capteur-val ${alerte ? 'alert' : actif ? 'ok' : 'off'}">${item.detail}</div>
        <div class="capteur-ts">${item.model}</div>`;
      grid.appendChild(card);
    });

    // Alertes hardware
    const hwAlerts = Object.values(hw).filter(v => v === false).length;
    const aiAlerts = (d.chute ? 1 : 0) + (d.feu ? 1 : 0);
    const total = hwAlerts + aiAlerts;
    document.getElementById('tb-alert').textContent = total || '0';
  } catch(e) {}
}

// WebSocket logs
const ws = new WebSocket('ws://' + location.host + '/ws/logs');
ws.onopen  = () => document.getElementById('ws-dot').classList.add('on');
ws.onclose = () => document.getElementById('ws-dot').classList.remove('on');
ws.onmessage = e => addLog(e.data);

refreshStatus();
refreshStats();
refreshCapteurs();
setInterval(refreshStatus, 3000);
setInterval(refreshStats, 5000);
setInterval(refreshCapteurs, 3000);
</script>
</body></html>"""


@app.route('/')
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route('/api/run')
def api_run():
    return jsonify(lancer(request.args.get('script', '')))

@app.route('/api/stop')
def api_stop():
    return jsonify(arreter(request.args.get('script', '')))

@app.route('/api/status')
def api_status():
    return jsonify(statut())

@app.route('/api/emotion')
def api_emotion():
    send_emotion(request.args.get('value', 'content'))
    return jsonify({"ok": True})

@app.route('/api/stats')
def api_stats():
    uptime_s = int(time.time() - _stats["demarrage"])
    h, m = divmod(uptime_s // 60, 60)
    uptime_str = f"{h}h{m:02d}m"
    return jsonify({
        "conversations": _stats["conversations"],
        "outils":        _stats["outils"],
        "personnes":     len(_stats["personnes"]),
        "noms":          ", ".join(sorted(_stats["personnes"])) or "—",
        "alertes_feu":   _stats["alertes_feu"],
        "alertes_chute": _stats["alertes_chute"],
        "uptime":        uptime_str,
    })

@app.route('/api/capteurs')
def api_capteurs():
    if _capteurs_hist:
        return jsonify(_capteurs_hist[-1]["data"])
    return jsonify({})

@sock.route('/ws/logs')
def ws_logs(ws):
    with _ws_lock:
        _ws_clients.add(ws)
    for line in list(_log_buffer):
        try:
            ws.send(line)
        except Exception:
            break
    while True:
        try:
            ws.receive(timeout=30)
        except Exception:
            break
    with _ws_lock:
        _ws_clients.discard(ws)


if __name__ == '__main__':
    log("[Dashboard] Démarrage...")
    init_serie()
    threading.Thread(target=_ecoute_serie,    daemon=True).start()
    threading.Thread(target=_serveur_socket,  daemon=True).start()
    threading.Thread(target=_surveille_capteurs, daemon=True).start()
    send_emotion("content")
    log("[Dashboard] Accessible sur http://0.0.0.0:8888")
    app.run(host='0.0.0.0', port=8888, debug=False, threaded=True)