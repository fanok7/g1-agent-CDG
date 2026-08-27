#!/usr/bin/env python3
"""
face_id.py — Reconnaissance faciale (subprocess, GPU Jetson)
=============================================================
Tourne en arrière-plan lancé par main.py.
Écrit les visages détectés dans /tmp/face_id_state.json toutes les SKIP frames.

Format JSON :
    {"faces": [{"name": "alice", "score": 0.91}, ...], "ts": 1234567890.0}
    faces vide = personne devant la caméra (ou inconnu uniquement)

Stream vidéo optionnel : http://192.168.123.164:8081
"""

import os
import glob
import json
import time
import threading
import cv2
import numpy as np
from insightface.app import FaceAnalysis

# ── Config ────────────────────────────────────────────────────────────────────
PHOTOS_DIR    = os.path.join(os.path.dirname(__file__), "photos")
# Identifiants cherchés dans le nom v4l2 ET les attributs USB (fabricant/produit/série).
# Le nom v4l2 seul ne suffit pas : la HBVCAM s'annonce "USB Camera: USB Camera" (générique).
CAMERA_IDS    = ("UGREEN", "HBVCAM", "HB202400001")
CAMERA_NAME   = CAMERA_IDS[0]   # conservé pour les logs
DEVICE_ID     = 0         # fallback ultime si aucun nœud de capture trouvé
WIDTH, HEIGHT = 640, 480
SKIP          = 5       # traiter 1 frame sur SKIP
THRESHOLD     = 0.4
STATE_FILE    = "/tmp/face_id_state.json"
FRAME_FILE    = "/tmp/latest_ugreen.jpg"   # lu par vision_server.py pour YOLO UGREEN
PAUSE_FILE    = "/tmp/vision_pause"   # créé par le jeu RPS → libérer /dev/video0
STREAM_PORT   = 8081     # mettre à 0 pour désactiver le stream Flask

# ── InsightFace (GPU si disponible, sinon CPU) ────────────────────────────────
import onnxruntime as _ort
_avail = _ort.get_available_providers()
_providers = ['CUDAExecutionProvider', 'CPUExecutionProvider'] if 'CUDAExecutionProvider' in _avail \
             else ['CPUExecutionProvider']
print(f"[face_id] Chargement InsightFace ({_providers[0]})...", flush=True)
face_app = FaceAnalysis(name="buffalo_sc", providers=_providers)
face_app.prepare(ctx_id=0, det_size=(640, 480))
print("[face_id] InsightFace prêt", flush=True)

# ── Base de visages ───────────────────────────────────────────────────────────
known_names: list = []
known_embeddings: list = []


def load_photos(photos_dir: str) -> None:
    os.makedirs(photos_dir, exist_ok=True)
    files = [f for f in os.listdir(photos_dir)
             if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
    if not files:
        print(f"[face_id] Aucune photo dans {photos_dir}", flush=True)
        return
    for fname in files:
        name = os.path.splitext(fname)[0]
        img  = cv2.imread(os.path.join(photos_dir, fname))
        if img is None:
            continue
        faces = face_app.get(img)
        if not faces:
            print(f"[face_id] Aucun visage dans {fname}", flush=True)
            continue
        face = max(faces, key=lambda f: (f.bbox[2]-f.bbox[0]) * (f.bbox[3]-f.bbox[1]))
        known_names.append(name)
        known_embeddings.append(face.normed_embedding)
        print(f"[face_id] Visage chargé : {name}", flush=True)
    print(f"[face_id] Base : {len(known_names)} personne(s) — {known_names}", flush=True)


load_photos(PHOTOS_DIR)


def identify(embedding: np.ndarray) -> tuple:
    if not known_embeddings:
        return "Inconnu", 0.0
    scores   = [float(np.dot(embedding, k)) for k in known_embeddings]
    best_idx = int(np.argmax(scores))
    best     = scores[best_idx]
    if best >= THRESHOLD:
        return known_names[best_idx], best
    return "Inconnu", best


# ── Caméra ────────────────────────────────────────────────────────────────────
def _video_name(idx: int) -> str:
    """Nom carte v4l2 du /dev/videoN (via /sys), '' si absent."""
    try:
        with open(f"/sys/class/video4linux/video{idx}/name") as f:
            return f.read().strip()
    except OSError:
        return ""


def _usb_ident(idx: int) -> str:
    """Fabricant + produit + n° de série USB du /dev/videoN, '' si introuvable.
    Nécessaire car certaines caméras (HBVCAM) n'exposent qu'un nom v4l2 générique."""
    try:
        usb_dir = os.path.join(
            os.path.realpath(f"/sys/class/video4linux/video{idx}/device"), "..")
    except OSError:
        return ""
    parts = []
    for attr in ("manufacturer", "product", "serial"):
        try:
            with open(os.path.join(usb_dir, attr)) as f:
                parts.append(f.read().strip())
        except OSError:
            pass
    return " ".join(parts)


def _is_capture_node(idx: int) -> bool:
    """True si /dev/videoN est le nœud de capture UVC (index 0) et pas le nœud
    metadata (index 1). Permissif si l'attribut est illisible."""
    try:
        with open(f"/sys/class/video4linux/video{idx}/index") as f:
            return int(f.read().strip()) == 0
    except (OSError, ValueError):
        return True


def find_camera_by_name(name_substrs=CAMERA_IDS, fallback: int = DEVICE_ID) -> int:
    """Retourne l'index du nœud de capture dont le nom v4l2 OU les attributs USB
    contiennent un des name_substrs. À défaut, premier nœud de capture disponible.
    Pas de test d'ouverture ici : ouvrir video6 avant video7 corrompt l'USB sur
    Jetson Tegra et empêche video7 de s'ouvrir. open_camera() gère les retries."""
    if isinstance(name_substrs, str):
        name_substrs = (name_substrs,)
    wanted = [s.lower() for s in name_substrs]

    capture_nodes, matches = [], []
    for path in sorted(glob.glob("/sys/class/video4linux/video*"),
                       key=lambda p: int(os.path.basename(p).replace("video", ""))):
        idx = int(os.path.basename(path).replace("video", ""))
        if not _is_capture_node(idx):
            continue
        capture_nodes.append(idx)
        haystack = (_video_name(idx) + " " + _usb_ident(idx)).lower()
        if any(s in haystack for s in wanted):
            matches.append(idx)

    if matches:
        best = matches[0]
        print(f"[face_id] Caméra reconnue → /dev/video{best} "
              f"({_video_name(best)} | {_usb_ident(best)}) — candidats {matches}", flush=True)
        return best
    if capture_nodes:
        best = capture_nodes[0]
        print(f"[face_id] Aucun identifiant {list(name_substrs)} trouvé — "
              f"1er nœud de capture /dev/video{best} "
              f"({_video_name(best)} | {_usb_ident(best)})", flush=True)
        return best
    print(f"[face_id] Aucun nœud de capture — fallback /dev/video{fallback}", flush=True)
    return fallback


def open_camera(device_id: int = None) -> cv2.VideoCapture:
    if device_id is None:
        device_id = find_camera_by_name()
    for attempt in range(6):
        cap = cv2.VideoCapture(device_id)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        if cap.isOpened():
            print(f"[face_id] Caméra /dev/video{device_id} ouverte "
                  f"(tentative {attempt+1})", flush=True)
            return cap
        cap.release()
        print(f"[face_id] /dev/video{device_id} indisponible "
              f"(tentative {attempt+1}/6) — retry 2s", flush=True)
        time.sleep(2)
    raise RuntimeError(f"Impossible d'ouvrir /dev/video{device_id} après 6 tentatives")


# ── État partagé entre boucle détection et Flask ─────────────────────────────
_lock         = threading.Lock()
_latest_frame = None
_latest_state: dict = {"faces": [], "ts": 0.0}


def _write_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)   # écriture atomique


# ── Boucle de détection ───────────────────────────────────────────────────────
_raw_frame      = None
_raw_frame_lock = threading.Lock()


def _capture_loop(cap_ref: list) -> None:
    """Thread rapide : lit les frames sans s'arrêter, toujours la plus fraîche.
    Libère la caméra quand /tmp/vision_pause existe (jeu RPS)."""
    global _raw_frame
    while True:
        if os.path.exists(PAUSE_FILE):
            if cap_ref[0] is not None and cap_ref[0].isOpened():
                cap_ref[0].release()
                print("[face_id] Pause — /dev/video0 libérée pour le jeu RPS", flush=True)
            time.sleep(0.2)
            continue
        if cap_ref[0] is None or not cap_ref[0].isOpened():
            try:
                cap_ref[0] = open_camera()
                print("[face_id] Reprise après pause", flush=True)
            except RuntimeError as e:
                print(f"[face_id] {e} — nouvel essai dans 1s", flush=True)
                time.sleep(1)
                continue
        ret, frame = cap_ref[0].read()
        if not ret:
            print("[face_id] Reconnexion caméra...", flush=True)
            cap_ref[0].release()
            time.sleep(2)
            continue
        with _raw_frame_lock:
            _raw_frame = frame

        # Partage de frame pour vision_server.py (YOLO UGREEN sans conflit /dev/video0).
        # On encode en mémoire avec cv2.imencode (le codec est choisi par l'extension
        # '.jpg' passée ici) puis on écrit les octets bruts dans un .tmp avant un
        # os.replace atomique. NE PAS utiliser cv2.imwrite('....tmp', ...) : imwrite
        # déduit le format de l'extension du fichier, et '.tmp' n'est pas reconnu →
        # échec SILENCIEUX → /tmp/latest_ugreen.jpg jamais créé → YOLO UGREEN aveugle.
        try:
            ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                tmp = FRAME_FILE + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(buf.tobytes())
                os.replace(tmp, FRAME_FILE)
        except Exception as e:
            print(f"[face_id] Écriture frame UGREEN échouée : {e}", flush=True)


def detection_loop() -> None:
    global _latest_frame, _latest_state

    # Délai de démarrage : après un crash + redémarrage par _supervise (3s),
    # le kernel peut mettre 2-5s à libérer le device USB de l'instance précédente.
    time.sleep(3)
    cap_ref      = [open_camera()]
    last_results = []

    threading.Thread(target=_capture_loop, args=(cap_ref,), daemon=True).start()

    _interval = SKIP / 30.0   # ~0.5 s entre deux inférences à 30 fps

    while True:
        if os.path.exists(PAUSE_FILE):
            # Caméra libérée pour le jeu RPS — ne pas analyser la frame figée
            time.sleep(0.3)
            continue

        with _raw_frame_lock:
            frame = _raw_frame
        if frame is None:
            time.sleep(0.01)
            continue

        faces = face_app.get(frame)
        time.sleep(_interval)
        last_results = []
        found = []

        for face in faces:
            x1, y1, x2, y2 = [int(v) for v in face.bbox]
            name, score = identify(face.normed_embedding)
            last_results.append((x1, y1, x2, y2, name, score))
            entry = {"name": name, "score": round(score, 3), "bbox": [x1, y1, x2, y2]}
            if name == "Inconnu":
                entry["embedding"] = face.normed_embedding.tolist()
            found.append(entry)

        state = {"faces": found, "ts": time.time()}
        _write_state(state)
        with _lock:
            _latest_state = state

        # Annoter la frame la plus fraîche pour le stream
        with _raw_frame_lock:
            frame = _raw_frame.copy()
        for (x1, y1, x2, y2, name, score) in last_results:
            color = (0, 255, 0) if name != "Inconnu" else (0, 0, 255)
            label = f"{name} ({score:.2f})" if name != "Inconnu" else "Inconnu"
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.rectangle(frame, (x1, y1 - 28), (x2, y1), color, -1)
            cv2.putText(frame, label, (x1 + 5, y1 - 7),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        with _lock:
            _latest_frame = frame


# ── Stream Flask optionnel ────────────────────────────────────────────────────
def start_flask_stream(port: int) -> None:
    from flask import Flask, Response

    app_flask = Flask(__name__)

    def generate():
        while True:
            with _lock:
                frame = _latest_frame
            if frame is None:
                time.sleep(0.05)
                continue
            _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
                   + buf.tobytes() + b'\r\n')

    @app_flask.route('/')
    def index():
        with _lock:
            state = _latest_state
        count = len(known_names)
        rows  = "".join(f"<tr><td>&#x1F464; {n}</td></tr>" for n in known_names) \
                if known_names else "<tr><td style='color:#f66'>Aucune photo</td></tr>"
        faces_now  = state.get("faces", [])
        faces_html = ", ".join(
            f"<b style='color:#0f0'>{f['name']}</b>" if f['name'] != "Inconnu"
            else "<b style='color:#f00'>Inconnu</b>"
            for f in faces_now
        ) or "personne"
        return f'''<!DOCTYPE html><html><head><title>G1 Face ID</title><meta charset="utf-8">
<style>body{{background:#111;display:flex;justify-content:center;align-items:center;
min-height:100vh;margin:0;flex-direction:column;gap:12px;padding:20px;}}
img{{border:2px solid #444;max-width:100%;}}h1{{color:white;font-family:monospace;margin:0;}}
p{{color:#aaa;font-family:monospace;margin:0;}}
table{{font-family:monospace;border-collapse:collapse;}}td{{color:#ccc;padding:3px 16px;}}
</style></head><body>
<h1>Unitree G1 — Face ID</h1>
<p>{count} personne(s) dans la base &nbsp;|&nbsp; Devant caméra : {faces_html}</p>
<table>{rows}</table><img src="/stream"></body></html>'''

    @app_flask.route('/stream')
    def stream():
        return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

    print(f"[face_id] Stream sur http://192.168.123.164:{port}", flush=True)
    app_flask.run(host='0.0.0.0', port=port, threaded=True)


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    t = threading.Thread(target=detection_loop, daemon=True)
    t.start()

    if STREAM_PORT:
        start_flask_stream(STREAM_PORT)
    else:
        t.join()
