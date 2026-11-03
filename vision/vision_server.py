#!/usr/bin/env python3
"""
vision/vision_server.py — Réception des détections YOLO + visages, calculées
à distance sur la Jetson Thor (GPU), fusion et exposition HTTP locale.

⚠️ Aucune inférence ne tourne plus ici. La RealSense D435i ET la caméra UGREEN
(face_id) sont branchées sur la Jetson Thor (192.168.123.166), pas sur ce
Jetson (Orin NX, pas de GPU CUDA utilisable pour onnxruntime) :
  - vision/realsense_remote_push.py (Thor) POST chaque frame RealSense → /push_realsense
    (ignoré en mode iinterim — scanner_billet hors périmètre métier, voir REALSENSE_ACTIVE)
  - vision/face_id/face_id.py (Thor, InsightFace + YOLO sur onnxruntime-gpu)
    POST la frame UGREEN brute → /push_ugreen_frame, et l'état visages +
    détections YOLO (fusionnés sur un seul endpoint) → /push_ugreen_state
Ce script ne fait que recevoir ces push, fusionner UGREEN+RealSense et écrire
vision_state.json — plus de thread YOLO local (_yolo_ugreen_loop retiré).

IPC fichiers :
  /tmp/latest_ugreen.jpg    → frame UGREEN écrite par /push_ugreen_frame (POST distant depuis la Thor)
  /tmp/latest_realsense.jpg → frame RealSense écrite par /push_realsense (POST distant depuis la Thor, mode cdg/daneel uniquement)
  /tmp/face_id_state.json   → état visages écrit par /push_ugreen_state (POST distant depuis la Thor)
  /tmp/vision_state.json    → {"objects":[{label,conf,x,y,cam}], "ts":…}
  /tmp/vision_pause         → RPS game : stoppe la capture RealSense

HTTP (port 8080) :
  /stream            → MJPEG UGREEN annoté (relu depuis face_id)
  /stream/realsense  → MJPEG RealSense brut (hors service : voir THOR.md)
  /detections        → JSON détections courantes
  /push_realsense    → POST (body = JPEG brut) — reçoit les frames RealSense depuis la Thor
  /push_ugreen_frame → POST (body = JPEG brut) — reçoit les frames UGREEN depuis la Thor
  /push_ugreen_state → POST (body = JSON {"faces": [...]} ou {"objects": [...]}) — reçoit l'état visages et/ou objets YOLO depuis la Thor
"""

import json
import logging
import os
import sys
import threading
import time

# Chemins IPC partagés avec l'agent : ce sous-processus est lancé par
# chemin absolu, la racine du dépôt n'est donc pas sur sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ipc

import cv2
import numpy as np
from flask import Flask, Response, request

try:
    import pyrealsense2 as rs
    USE_REALSENSE = True
    print("[VISION] RealSense D435i disponible")
except ImportError:
    USE_REALSENSE = False
    print("[VISION] pyrealsense2 absent — YOLO RealSense désactivé")

# RealSense : capture couleur uniquement (pas de YOLO RS — réservée au scan QR).
# YOLO RS désactivé : le D435i décrochait en USB et spammait des erreurs.
USE_REALSENSE = True

# ── Config ────────────────────────────────────────────────────────────────────
UGREEN_FRAME    = ipc.UGREEN_FRAME      # fourni par face_id.py (Thor) via /push_ugreen_frame
RS_FRAME        = ipc.REALSENSE_FRAME   # fourni par realsense_remote_push.py (Thor) via /push_realsense
STATE_FILE      = ipc.VISION_STATE
QR_STATE_FILE   = ipc.QR_STATE
QR_INTERVAL     = 1.0    # secondes entre deux tentatives de scan
QR_COOLDOWN     = 30.0   # secondes avant de re-signaler le même code
UV_BIN          = '/home/unitree/.local/bin/uv'
QR_REPO         = '/home/unitree/QR_scan_tool'
HTTP_PORT       = 8080
WIDTH, HEIGHT   = 640, 480
MAX_OBJECTS     = 5
QUALITY         = 75
STREAM_FPS      = 12                    # plafond d'envoi du flux MJPEG (voir _gen_ugreen)

# Lu directement (pas d'import de config.py ici, trop lourd pour ce sous-
# processus) — même variable d'env que main.py/config.py, héritée par ce
# sous-processus (subprocess.Popen sans env= : hérite l'environnement du
# parent). La RealSense ne sert qu'au scan de billet (scanner_billet), hors
# périmètre métier d'iinterim (agence d'intérim) : on ignore ce flux dans ce
# mode plutôt que de le traiter pour rien.
ROBOT_MODE      = os.environ.get("ROBOT_MODE", "cdg").strip().lower()
REALSENSE_ACTIVE = ROBOT_MODE != "iinterim"
# ─────────────────────────────────────────────────────────────────────────────

# ── Authentification des routes /push_* ───────────────────────────────────────
# Ces routes acceptent n'importe quel POST sans validation : sur le réseau eth0
# partagé avec la Thor (192.168.123.x), n'importe qui pourrait injecter une
# fausse frame ou un faux visage "reconnu" (identifier_personne mentirait au
# LLM). Contrairement à dashboard.py/tablet_server.py, on NE génère PAS de
# token automatique ici : la Thor (face_id.py, realsense_remote_push.py) est
# un AUTRE processus sur une AUTRE machine, un token généré à la volée sur
# l'Orin NX ne serait jamais connu de la Thor. Si G1_ADMIN_TOKEN est défini
# (même valeur des deux côtés, dans le .env de chaque machine), il est exigé ;
# sinon, comportement historique (routes ouvertes) + avertissement au démarrage.
ADMIN_TOKEN = os.environ.get("G1_ADMIN_TOKEN", "").strip()
if not ADMIN_TOKEN:
    print("[VISION] ⚠ G1_ADMIN_TOKEN non défini — routes /push_* ouvertes sans "
          "authentification. Définir G1_ADMIN_TOKEN (même valeur sur l'Orin NX "
          "et la Thor) pour les protéger.")


def _authorized() -> bool:
    if not ADMIN_TOKEN:
        return True
    return request.headers.get("X-Admin-Token") == ADMIN_TOKEN

# Les push depuis la Thor arrivent en continu (plusieurs par seconde) — le
# log d'accès Werkzeug par défaut ("POST /push_... 204") noierait le reste
# des logs de main.py sans rien apporter (pas d'erreur à surveiller ici, les
# routes renvoient déjà des codes clairs en cas de problème).
logging.getLogger('werkzeug').setLevel(logging.WARNING)

app   = Flask(__name__)

_lock           = threading.Lock()
_dets_ugreen    = []
_dets_realsense = []
_faces          = []     # derniers visages reçus (bbox + nom), pour l'annotation
_ann_realsense  = None   # frame RealSense annotée (pour HTTP stream)

# Les flux objets et visages viennent de la MÊME caméra (UGREEN sur la Thor,
# poussée dans UGREEN_FRAME) : ils sont fusionnés en un seul stream annoté par
# _gen_ugreen(). On ne garde donc que les données (_dets_ugreen, _faces) et on
# dessine à la demande, dans le générateur, au lieu de ré-annoter une frame à
# chaque push — ce qui décodait le JPEG ~24 fois par seconde même sans client.


# ── Écriture atomique ─────────────────────────────────────────────────────────

def _write_state(dets):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"objects": dets, "ts": time.time()}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[VISION] Erreur écriture état : {e}")


# ── Fusion des deux caméras ───────────────────────────────────────────────────

def _merge_and_write():
    """Fusionne UGREEN + RealSense, trie par confiance, écrit le fichier JSON."""
    with _lock:
        all_dets = list(_dets_ugreen) + list(_dets_realsense)
    all_dets.sort(key=lambda d: d["conf"], reverse=True)
    _write_state(all_dets[:MAX_OBJECTS])


# ── Réception YOLO UGREEN distante (calcul déporté sur la Thor) ─────────────
# L'inférence YOLO ne tourne plus ici : face_id.py (Thor) fait déjà tourner
# InsightFace sur la même frame UGREEN, avec un vrai GPU CUDA — on y a ajouté
# l'inférence YOLO en plus (onnxruntime-gpu, même modèle yolo26n exporté en
# .onnx) pour éviter une 2e capture caméra et réutiliser le GPU déjà en place.
# Voir vision/face_id/face_id.py § YOLO. Ce Jetson ne fait plus que recevoir
# le JSON de détections via /push_ugreen_state ci-dessous (payload {"objects": [...]}).


# ── Réception RealSense distante (caméra branchée sur la Thor) ──────────────
# La D435i n'est plus sur ce Jetson : plus de pipeline pyrealsense2 local ici.
# vision/realsense_remote_push.py tourne sur la Thor et POST chaque frame JPEG
# sur /push_realsense (voir route Flask plus bas) — on se contente de l'écrire
# telle quelle dans RS_FRAME, atomiquement, comme le faisait l'ancienne capture
# locale (même fichier lu par qr_tool.py et le stream MJPEG).
_last_rs_push = 0.0
RS_PUSH_STALE_SEC = 5.0   # au-delà, on considère la Thor déconnectée (logs)


def _rs_watchdog_loop():
    """Log une transition (pas en boucle) quand le flux RealSense poussé par
    la Thor devient périmé, puis quand il revient — seul moyen de le
    remarquer sans examiner manuellement le timestamp de RS_FRAME."""
    was_stale = False
    while True:
        time.sleep(2.0)
        stale = _last_rs_push > 0 and (time.time() - _last_rs_push) > RS_PUSH_STALE_SEC
        if stale and not was_stale:
            print(f"[RS-PUSH] Plus de frame RealSense depuis {RS_PUSH_STALE_SEC:.0f}s "
                  f"— Thor déconnectée ?", flush=True)
        elif was_stale and not stale:
            print("[RS-PUSH] Flux RealSense revenu", flush=True)
        was_stale = stale


# ── Thread QR passif (RealSense) ─────────────────────────────────────────────

_DECODE_SCRIPT = (
    "import sys; sys.path.insert(0, '{repo}'); from scanner import scan_image; "
    "r = scan_image(sys.argv[1]); print(r if r else '__NONE__')"
)

def _qr_scan_loop():
    """Scan QR/PDF417/Aztec en continu sur la frame RealSense partagée.
    Utilise uv+zxingcpp pour supporter l'Aztec (billets Air France/KLM)."""
    import subprocess
    print("[QR] Thread passif démarré (QR+PDF417+Aztec via zxingcpp)", flush=True)
    _last_raw   = None
    _last_alert = 0.0
    _last_mtime = None

    while True:
        time.sleep(QR_INTERVAL)
        if not os.path.exists(RS_FRAME):
            continue
        try:
            mtime = os.path.getmtime(RS_FRAME)
        except OSError:
            continue
        if mtime == _last_mtime:
            continue
        _last_mtime = mtime
        try:
            out = subprocess.check_output(
                [UV_BIN, "run", "--project", QR_REPO, "python", "-c",
                 _DECODE_SCRIPT.format(repo=QR_REPO), RS_FRAME],
                stderr=subprocess.DEVNULL, timeout=4,
            ).decode().strip()
            raw = None if out == "__NONE__" else out
        except Exception:
            continue

        if not raw:
            continue

        now = time.time()
        if raw == _last_raw and (now - _last_alert) < QR_COOLDOWN:
            continue
        _last_raw   = raw
        _last_alert = now

        # Parse BCBP
        info = {"raw": raw, "ts": now}
        if raw.startswith("M"):
            try:
                nom_brut  = raw[2:22].strip()
                pnr       = raw[23:30].strip()
                depart    = raw[30:33].strip()
                arrivee   = raw[33:36].strip()
                compagnie = raw[36:39].strip()
                vol       = raw[39:44].strip()
                if "/" in nom_brut:
                    n, p = nom_brut.split("/", 1)
                    nom = "{} {}".format(p.strip().title(), n.strip().title())
                else:
                    nom = nom_brut.title()
                info = {"passager": nom, "pnr": pnr,
                        "vol": "{}{}".format(compagnie.strip(), vol.strip()),
                        "de": depart, "vers": arrivee, "raw": raw, "ts": now}
            except Exception:
                pass

        tmp = QR_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(info, f, ensure_ascii=False)
        os.replace(tmp, QR_STATE_FILE)
        print("[QR] Billet détecté : {} {}".format(
            info.get("passager", "?"), info.get("vol", "")), flush=True)


# ── HTTP MJPEG ────────────────────────────────────────────────────────────────

def _annotate(frame, dets, faces):
    """Dessine objets YOLO (points verts) ET visages (rectangles nommés) sur la
    même frame. Les détections peuvent avoir jusqu'à ~2 s de retard sur l'image
    (cadence YOLO côté Thor) : c'est un flux de supervision, pas une preuve."""
    for d in dets:
        x, y = d.get("x", 0), d.get("y", 0)
        label = f"{d.get('label','?')} ({d.get('conf',0):.2f})"
        cv2.circle(frame, (x, y), 6, (0, 255, 0), -1)
        cv2.putText(frame, label, (x + 8, y), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (0, 255, 0), 2)
    for face in faces:
        bbox = face.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x1, y1, x2, y2 = bbox
        name = face.get("name", "Inconnu")
        color = (0, 255, 0) if name != "Inconnu" else (0, 0, 255)
        label = (f"{name} ({face.get('score', 0.0):.2f})"
                 if name != "Inconnu" else "Inconnu")
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.rectangle(frame, (x1, y1 - 28), (x2, y1), color, -1)
        cv2.putText(frame, label, (x1 + 5, y1 - 7),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    return frame


def _gen_ugreen():
    """Flux unique de la caméra UGREEN : objets + visages sur la même image.

    Cadencé à STREAM_FPS : sans ce plafond la boucle relit, réannote et réencode
    la frame aussi vite que le CPU le permet (~95 fps mesurés, ~50 Mbit/s), en
    renvoyant surtout des doublons — face_id ne pousse qu'~24 frames/s. C'est un
    flux de supervision : 12 fps suffisent, et le réseau wifi respire.
    """
    period = 1.0 / STREAM_FPS
    while True:
        t0 = time.time()
        frame = cv2.imread(UGREEN_FRAME) if os.path.exists(UGREEN_FRAME) else None
        if frame is None:
            time.sleep(0.05)
            continue
        with _lock:
            dets, faces = list(_dets_ugreen), list(_faces)
        try:
            frame = _annotate(frame, dets, faces)
        except Exception as e:
            print(f"[VISION] Annotation échouée : {e}", flush=True)
        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
               + buf.tobytes() + b'\r\n')
        time.sleep(max(0.0, period - (time.time() - t0)))


def _gen_realsense():
    """Stream RealSense brut (depuis /tmp/latest_realsense.jpg partagé)."""
    while True:
        if os.path.exists(RS_FRAME):
            raw = cv2.imread(RS_FRAME)
            if raw is not None:
                _, buf = cv2.imencode('.jpg', raw, [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
                       + buf.tobytes() + b'\r\n')
                continue
        time.sleep(0.05)


@app.route('/')
def index():
    rs = ('<img src="/stream/realsense">' if USE_REALSENSE else
          '<p style="color:#666;font-family:monospace">RealSense : pas de flux '
          '(caméra restée sur l\'Orin, push désactivé)</p>')
    return f'''<html><head><title>G1 Vision</title>
    <style>body{{background:#111;display:flex;align-items:center;justify-content:center;
    height:100vh;margin:0;flex-direction:column;gap:12px;}}
    h1{{color:white;font-family:monospace;font-size:18px;}}img{{border:2px solid #444;max-width:90%;}}
    </style></head>
    <body><h1>Unitree G1 — UGREEN : objets + visages</h1>
    <img src="/stream">{rs}
    </body></html>'''

@app.route('/stream')
def stream_ugreen():
    return Response(_gen_ugreen(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/stream/realsense')
def stream_realsense():
    return Response(_gen_realsense(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/stream/face')
def stream_face():
    """Alias historique : même caméra, mêmes annotations que /stream depuis la
    fusion des deux flux. Conservé pour ne pas casser les liens existants."""
    return Response(_gen_ugreen(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/detections')
def detections():
    with _lock:
        return {"objects": list(_dets_ugreen) + list(_dets_realsense)}


@app.route('/push_realsense', methods=['POST'])
def push_realsense():
    """Reçoit une frame JPEG brute depuis vision/realsense_remote_push.py (Thor)
    et l'écrit à la place de l'ancienne capture locale — même fichier, même
    contrat pour qr_tool.py et le stream /stream/realsense."""
    if not _authorized():
        return ('unauthorized', 401)
    if not REALSENSE_ACTIVE:
        # Mode iinterim : scanner_billet hors périmètre, on ignore le flux
        # plutôt que de l'écrire pour rien (pas d'erreur côté Thor pour autant).
        return ('', 204)
    global _last_rs_push
    data = request.get_data()
    if not data:
        return ('empty body', 400)
    try:
        tmp = RS_FRAME + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, RS_FRAME)
    except Exception as e:
        print(f"[RS-PUSH] Erreur écriture : {e}", flush=True)
        return ('write failed', 500)
    _last_rs_push = time.time()
    return ('', 204)


@app.route('/push_ugreen_frame', methods=['POST'])
def push_ugreen_frame():
    """Reçoit une frame JPEG UGREEN brute depuis face_id.py (Thor) — remplace
    l'ancienne écriture locale par face_id.py tournant sur ce même Jetson."""
    if not _authorized():
        return ('unauthorized', 401)
    data = request.get_data()
    if not data:
        return ('empty body', 400)
    try:
        tmp = UGREEN_FRAME + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, UGREEN_FRAME)
    except Exception as e:
        print(f"[UGREEN-PUSH] Erreur écriture : {e}", flush=True)
        return ('write failed', 500)
    return ('', 204)


@app.route('/push_ugreen_state', methods=['POST'])
def push_ugreen_state():
    """Reçoit l'état JSON UGREEN depuis face_id.py (Thor) — visages ET objets
    YOLO, fusionnés sur un seul endpoint (remplace les anciennes routes
    /push_yolo_ugreen et /push_face_state, qui ne faisaient que dupliquer la
    même logique d'écriture pour la même caméra). Les deux boucles côté Thor
    gardent leur propre cadence (visages en continu, objets toutes les 2s) :
    un payload ne contient que la clé qu'il met à jour (`faces` ou `objects`),
    jamais les deux à la fois."""
    if not _authorized():
        return ('unauthorized', 401)
    try:
        data = request.get_json(force=True)
    except Exception:
        return ('invalid json', 400)
    if not isinstance(data, dict):
        return ('invalid json', 400)

    if 'faces' in data:
        try:
            tmp = ipc.FACE_ID_STATE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, ipc.FACE_ID_STATE)
        except Exception as e:
            print(f"[FACE-PUSH] Erreur écriture : {e}", flush=True)
            return ('write failed', 500)
        faces = data.get("faces")
        global _faces
        with _lock:
            _faces = faces if isinstance(faces, list) else []

    if 'objects' in data:
        dets = data.get('objects')
        if isinstance(dets, list):
            global _dets_ugreen
            with _lock:
                _dets_ugreen = dets
            _merge_and_write()

    return ('', 204)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if REALSENSE_ACTIVE:
        threading.Thread(target=_qr_scan_loop,     daemon=True).start()
        threading.Thread(target=_rs_watchdog_loop, daemon=True).start()
    else:
        print(f"[VISION] Mode {ROBOT_MODE} : RealSense ignorée (scanner_billet "
              f"hors périmètre), threads QR/watchdog non démarrés.", flush=True)
    print(f"[VISION] Stream UGREEN     → http://192.168.123.164:{HTTP_PORT}/stream")
    print(f"[VISION] Stream RealSense  → http://192.168.123.164:{HTTP_PORT}/stream/realsense")
    print(f"[VISION] Stream Face ID    → http://192.168.123.164:{HTTP_PORT}/stream/face"
          " (alias de /stream — flux fusionné)")
    print(f"[VISION] Détections JSON   → http://192.168.123.164:{HTTP_PORT}/detections")
    print(f"[VISION] RealSense reçue via POST sur /push_realsense (depuis la Thor, "
          f"vision/realsense_remote_push.py doit y tourner)")
    app.run(host='0.0.0.0', port=HTTP_PORT, threaded=True)
