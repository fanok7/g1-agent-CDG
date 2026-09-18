#!/usr/bin/env python3
"""
vision/vision_server.py — Réception des détections YOLO + visages, calculées
à distance sur la Jetson Thor (GPU), fusion et exposition HTTP locale.

⚠️ Aucune inférence ne tourne plus ici. La RealSense D435i ET la caméra UGREEN
(face_id) sont branchées sur la Jetson Thor (192.168.123.166), pas sur ce
Jetson (Orin NX, pas de GPU CUDA utilisable pour onnxruntime) :
  - vision/realsense_remote_push.py (Thor) POST chaque frame RealSense → /push_realsense
  - vision/face_id/face_id.py (Thor, InsightFace + YOLO sur onnxruntime-gpu)
    POST l'état visages, la frame UGREEN et les détections YOLO UGREEN →
    /push_face_state, /push_ugreen_frame, /push_yolo_ugreen
Ce script ne fait que recevoir ces push, fusionner UGREEN+RealSense et écrire
vision_state.json — plus de thread YOLO local (_yolo_ugreen_loop retiré).

IPC fichiers :
  /tmp/latest_ugreen.jpg    → frame UGREEN écrite par /push_ugreen_frame (POST distant depuis la Thor)
  /tmp/latest_realsense.jpg → frame RealSense écrite par /push_realsense (POST distant depuis la Thor)
  /tmp/face_id_state.json   → état visages écrit par /push_face_state (POST distant depuis la Thor)
  /tmp/vision_state.json    → {"objects":[{label,conf,x,y,cam}], "ts":…}
  /tmp/vision_pause         → RPS game : stoppe la capture RealSense

HTTP (port 8080) :
  /stream            → MJPEG UGREEN annoté (relu depuis face_id)
  /stream/realsense  → MJPEG RealSense annoté
  /detections        → JSON détections courantes
  /push_realsense    → POST (body = JPEG brut) — reçoit les frames RealSense depuis la Thor
  /push_ugreen_frame → POST (body = JPEG brut) — reçoit les frames UGREEN depuis la Thor
  /push_face_state   → POST (body = JSON) — reçoit l'état face_id depuis la Thor
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
UGREEN_FRAME    = ipc.UGREEN_FRAME      # fourni par face_id.py
RS_FRAME        = ipc.REALSENSE_FRAME   # fourni par _capture_rs_loop (partagé avec qr_tool)
PAUSE_FILE      = ipc.VISION_PAUSE_FLAG
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
# ─────────────────────────────────────────────────────────────────────────────

# Les push depuis la Thor arrivent en continu (plusieurs par seconde) — le
# log d'accès Werkzeug par défaut ("POST /push_... 204") noierait le reste
# des logs de main.py sans rien apporter (pas d'erreur à surveiller ici, les
# routes renvoient déjà des codes clairs en cas de problème).
logging.getLogger('werkzeug').setLevel(logging.WARNING)

app   = Flask(__name__)

_lock           = threading.Lock()
_dets_ugreen    = []
_dets_realsense = []
_ann_ugreen     = None   # frame UGREEN annotée objets (pour HTTP stream)
_ann_realsense  = None   # frame RealSense annotée (pour HTTP stream)
_ann_face       = None   # frame UGREEN annotée visages (pour HTTP stream /stream/face)


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
# le JSON de détections via /push_yolo_ugreen ci-dessous.


# ── Réception RealSense distante (caméra branchée sur la Thor) ──────────────
# La D435i n'est plus sur ce Jetson : plus de pipeline pyrealsense2 local ici.
# vision/realsense_remote_push.py tourne sur la Thor et POST chaque frame JPEG
# sur /push_realsense (voir route Flask plus bas) — on se contente de l'écrire
# telle quelle dans RS_FRAME, atomiquement, comme le faisait l'ancienne capture
# locale (même fichier lu par qr_tool.py et le stream MJPEG).
_last_rs_push = 0.0
RS_PUSH_STALE_SEC = 5.0   # au-delà, on considère la Thor déconnectée (logs)


def _yolo_rs_loop():
    """YOLO RealSense désactivé — RealSense réservée au scan QR."""
    return


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

def _gen_ugreen():
    while True:
        with _lock:
            frame = _ann_ugreen
        if frame is None:
            # Fallback : servir la dernière frame UGREEN brute
            raw = cv2.imread(UGREEN_FRAME) if os.path.exists(UGREEN_FRAME) else None
            frame = raw
        if frame is None:
            time.sleep(0.05)
            continue
        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
               + buf.tobytes() + b'\r\n')


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
    rs_link = '<a href="/stream/realsense" style="color:#aaa">RealSense</a>' if USE_REALSENSE else ''
    return f'''<html><head><title>G1 Vision</title>
    <style>body{{background:#111;display:flex;align-items:center;justify-content:center;
    height:100vh;margin:0;flex-direction:column;gap:12px;}}
    h1{{color:white;font-family:monospace;}}img{{border:2px solid #444;max-width:48%;}}
    .row{{display:flex;gap:12px;}}</style></head>
    <body><h1>Unitree G1 — YOLO dual-cam</h1>
    <div class="row"><img src="/stream"><img src="/stream/realsense"><img src="/stream/face"></div>
    </body></html>'''

@app.route('/stream')
def stream_ugreen():
    return Response(_gen_ugreen(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/stream/realsense')
def stream_realsense():
    return Response(_gen_realsense(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

def _gen_face():
    while True:
        with _lock:
            frame = _ann_face
        if frame is None:
            time.sleep(0.05)
            continue
        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
               + buf.tobytes() + b'\r\n')

@app.route('/stream/face')
def stream_face():
    return Response(_gen_face(),
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


@app.route('/push_yolo_ugreen', methods=['POST'])
def push_yolo_ugreen():
    """Reçoit les détections YOLO UGREEN (JSON) depuis face_id.py (Thor) —
    remplace l'ancien calcul local. Dessine aussi l'annotation pour /stream."""
    global _dets_ugreen, _ann_ugreen
    try:
        dets = request.get_json(force=True)
    except Exception:
        return ('invalid json', 400)
    if not isinstance(dets, list):
        return ('invalid json', 400)

    with _lock:
        _dets_ugreen = dets

    try:
        frame = cv2.imread(UGREEN_FRAME) if os.path.exists(UGREEN_FRAME) else None
        if frame is not None:
            for d in dets:
                x, y = d.get("x", 0), d.get("y", 0)
                label = f"{d.get('label','?')} ({d.get('conf',0):.2f})"
                cv2.circle(frame, (x, y), 6, (0, 255, 0), -1)
                cv2.putText(frame, label, (x + 8, y), cv2.FONT_HERSHEY_SIMPLEX,
                            0.6, (0, 255, 0), 2)
            with _lock:
                _ann_ugreen = frame
    except Exception as e:
        print(f"[YOLO-UGREEN-PUSH] Annotation échouée : {e}", flush=True)

    _merge_and_write()
    return ('', 204)


@app.route('/push_face_state', methods=['POST'])
def push_face_state():
    """Reçoit l'état JSON face_id (visages détectés, avec bbox) depuis face_id.py
    (Thor). Dessine aussi l'annotation pour /stream/face — remplace l'ancien
    stream Flask local à face_id.py (port 8081 sur la Thor, retiré)."""
    global _ann_face
    try:
        data = request.get_json(force=True)
    except Exception:
        return ('invalid json', 400)
    if data is None:
        return ('invalid json', 400)
    try:
        tmp = ipc.FACE_ID_STATE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, ipc.FACE_ID_STATE)
    except Exception as e:
        print(f"[FACE-PUSH] Erreur écriture : {e}", flush=True)
        return ('write failed', 500)

    try:
        frame = cv2.imread(UGREEN_FRAME) if os.path.exists(UGREEN_FRAME) else None
        if frame is not None:
            for face in data.get("faces", []):
                bbox = face.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                x1, y1, x2, y2 = bbox
                name = face.get("name", "Inconnu")
                score = face.get("score", 0.0)
                color = (0, 255, 0) if name != "Inconnu" else (0, 0, 255)
                label = f"{name} ({score:.2f})" if name != "Inconnu" else "Inconnu"
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.rectangle(frame, (x1, y1 - 28), (x2, y1), color, -1)
                cv2.putText(frame, label, (x1 + 5, y1 - 7),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            with _lock:
                _ann_face = frame
    except Exception as e:
        print(f"[FACE-PUSH] Annotation échouée : {e}", flush=True)

    return ('', 204)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    threading.Thread(target=_yolo_rs_loop,     daemon=True).start()
    threading.Thread(target=_qr_scan_loop,     daemon=True).start()
    print(f"[VISION] Stream UGREEN     → http://192.168.123.164:{HTTP_PORT}/stream")
    print(f"[VISION] Stream RealSense  → http://192.168.123.164:{HTTP_PORT}/stream/realsense")
    print(f"[VISION] Stream Face ID    → http://192.168.123.164:{HTTP_PORT}/stream/face")
    print(f"[VISION] Détections JSON   → http://192.168.123.164:{HTTP_PORT}/detections")
    print(f"[VISION] RealSense reçue via POST sur /push_realsense (depuis la Thor, "
          f"vision/realsense_remote_push.py doit y tourner)")
    app.run(host='0.0.0.0', port=HTTP_PORT, threaded=True)
