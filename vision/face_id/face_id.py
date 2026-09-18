#!/usr/bin/env python3
"""
face_id.py — Reconnaissance faciale (subprocess, GPU Jetson)
=============================================================
⚠️ Tourne sur la Jetson THOR (192.168.123.166), PAS sur l'Orin NX : la caméra
UGREEN a été déplacée là-bas en même temps que la RealSense, et la Thor a un
vrai GPU CUDA fonctionnel pour InsightFace (onnxruntime-gpu compilé depuis les
sources, cf. build sur la Thor — le wheel PyPI générique ne supporte pas son
architecture). Lancé à la main sur la Thor (pas supervisé par main.py).

Au lieu d'écrire /tmp/face_id_state.json et /tmp/latest_ugreen.jpg localement
(ce qui ne servirait à rien, rien ne les lit sur la Thor), ce script POST
chaque résultat vers vision_server.py qui tourne sur l'Orin NX :
    /push_face_state    (JSON)       → /tmp/face_id_state.json sur l'Orin NX
    /push_ugreen_frame  (JPEG brut)  → /tmp/latest_ugreen.jpg  sur l'Orin NX
Ça remplace l'ancienne écriture locale — mêmes fichiers, même contrat pour les
tools (identifier_personne, ce_que_je_vois) et pour YOLO UGREEN (vision_server).

Format JSON poussé :
    {"faces": [{"name": "alice", "score": 0.91}, ...], "ts": 1234567890.0}
    faces vide = personne devant la caméra (ou inconnu uniquement)

Pas de stream vidéo local ici : l'annotation visages + le flux MJPEG sont
faits côté Orin NX à partir de /push_face_state (bbox incluses) et de la
frame /push_ugreen_frame — voir vision_server.py, route /stream/face.
"""

import os
import sys
import glob
import json
import time
import threading

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    import urllib.request
    _HAS_REQUESTS = False

TARGET = os.environ.get("FACE_ID_TARGET", "http://192.168.123.164:8080")
PUSH_STATE_URL = TARGET.rstrip('/') + '/push_face_state'
PUSH_FRAME_URL = TARGET.rstrip('/') + '/push_ugreen_frame'
PUSH_YOLO_URL  = TARGET.rstrip('/') + '/push_yolo_ugreen'


def _push_json(url: str, data: dict) -> None:
    try:
        if _HAS_REQUESTS:
            requests.post(url, json=data, timeout=2)
        else:
            body = json.dumps(data).encode()
            req = urllib.request.Request(url, data=body, method='POST',
                                          headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=2)
    except Exception as e:
        print(f"[face_id] Push état échoué : {e}", flush=True)


def _push_bytes(url: str, data: bytes) -> None:
    try:
        if _HAS_REQUESTS:
            requests.post(url, data=data,
                           headers={'Content-Type': 'image/jpeg'}, timeout=2)
        else:
            req = urllib.request.Request(url, data=data, method='POST',
                                          headers={'Content-Type': 'image/jpeg'})
            urllib.request.urlopen(req, timeout=2)
    except Exception as e:
        print(f"[face_id] Push frame échoué : {e}", flush=True)

# Chemins IPC partagés avec l'agent. Ce script tourne sous miniconda et est
# lancé par chemin absolu : la racine du dépôt n'est pas sur sys.path.
# ipc.py n'importe que la stdlib, il est donc sûr sous n'importe quel interpréteur.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import ipc
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
WIDTH, HEIGHT = 1280, 960
SKIP          = 1       # traiter 1 frame sur SKIP
THRESHOLD     = 0.4
PAUSE_FILE    = ipc.VISION_PAUSE_FLAG  # créé par le jeu RPS → libérer /dev/video0

# ── InsightFace (GPU si disponible, sinon CPU) ────────────────────────────────
import onnxruntime as _ort
_avail = _ort.get_available_providers()
_providers = ['CUDAExecutionProvider', 'CPUExecutionProvider'] if 'CUDAExecutionProvider' in _avail \
             else ['CPUExecutionProvider']
print(f"[face_id] Chargement InsightFace ({_providers[0]})...", flush=True)
face_app = FaceAnalysis(name="buffalo_m", providers=_providers)
face_app.prepare(ctx_id=0, det_size=(1280, 960))
print("[face_id] InsightFace prêt", flush=True)

# ── YOLO (déporté ici pour réutiliser la même frame + le même GPU qu'InsightFace,
# plutôt qu'une 2e ouverture caméra sur l'Orin NX) ────────────────────────────
# onnxruntime brut, pas ultralytics : la lib ultralytics conditionne l'usage du
# CUDAExecutionProvider à torch.cuda.is_available(), et seul un torch CPU-only
# a un wheel aarch64 précompilé ici (un torch CUDA demanderait un build source
# aussi long que celui d'onnxruntime) — inutile, l'inférence elle-même passe
# entièrement par onnxruntime, torch n'est pas nécessaire.
YOLO_MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "yolo26n.onnx")
YOLO_CONFIDENCE = 0.50
YOLO_INTERVAL   = 2.0
YOLO_INPUT_SIZE = 640
# Sortie du modèle déjà post-NMS ([1, 300, 6] = x1,y1,x2,y2,conf,cls) — export
# "end-to-end" ultralytics, donc pas de NMS manuel à refaire ici.
_COCO_NAMES = (
    "personne,vélo,voiture,moto,avion,bus,train,camion,bateau,feu de circulation,"
    "bouche d'incendie,panneau stop,parcmètre,banc,oiseau,chat,chien,cheval,mouton,vache,"
    "éléphant,ours,zèbre,girafe,sac à dos,parapluie,sac à main,cravate,valise,frisbee,"
    "skis,snowboard,ballon de sport,cerf-volant,batte de baseball,gant de baseball,skateboard,"
    "planche de surf,raquette de tennis,bouteille,verre à vin,tasse,fourchette,couteau,cuillère,"
    "bol,banane,pomme,sandwich,orange,brocoli,carotte,hot-dog,pizza,donut,gâteau,chaise,canapé,"
    "plante en pot,lit,table,toilettes,tv,ordinateur portable,souris,télécommande,clavier,"
    "téléphone portable,micro-ondes,four,grille-pain,évier,réfrigérateur,livre,horloge,vase,"
    "ciseaux,ours en peluche,sèche-cheveux,brosse à dents"
).split(",")
try:
    _yolo_sess = _ort.InferenceSession(
        YOLO_MODEL_PATH,
        providers=['CUDAExecutionProvider', 'CPUExecutionProvider']
                  if 'CUDAExecutionProvider' in _avail else ['CPUExecutionProvider'],
    )
    print(f"[face_id] YOLO chargé ({_yolo_sess.get_providers()[0]})", flush=True)
except Exception as e:
    _yolo_sess = None
    print(f"[face_id] YOLO non chargé ({e}) — détection d'objets désactivée", flush=True)

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


def _write_state(state: dict) -> None:
    _push_json(PUSH_STATE_URL, state)


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

        # Partage de frame vers vision_server.py (Orin NX) pour YOLO UGREEN —
        # POST réseau au lieu de l'ancienne écriture locale (caméra + calcul
        # tournent maintenant sur la Thor, plus rien à écrire localement).
        try:
            ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                _push_bytes(PUSH_FRAME_URL, buf.tobytes())
        except Exception as e:
            print(f"[face_id] Push frame UGREEN échoué : {e}", flush=True)


def _yolo_letterbox(frame: np.ndarray, size: int = YOLO_INPUT_SIZE):
    """Redimensionne en conservant le ratio + padding gris — retourne l'image
    prête pour le modèle et (scale, pad_x, pad_y) pour retrouver les
    coordonnées d'origine sur les détections."""
    h, w = frame.shape[:2]
    scale = min(size / h, size / w)
    nh, nw = int(round(h * scale)), int(round(w * scale))
    resized = cv2.resize(frame, (nw, nh))
    pad_x, pad_y = (size - nw) // 2, (size - nh) // 2
    padded = np.full((size, size, 3), 114, dtype=np.uint8)
    padded[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return padded, scale, pad_x, pad_y


def _yolo_loop() -> None:
    """Toutes les YOLO_INTERVAL secondes, infère sur la dernière frame UGREEN
    partagée avec InsightFace (pas de 2e capture caméra) et pousse le résultat."""
    if _yolo_sess is None:
        return
    input_name = _yolo_sess.get_inputs()[0].name
    while True:
        time.sleep(YOLO_INTERVAL)
        if os.path.exists(PAUSE_FILE):
            continue
        with _raw_frame_lock:
            frame = _raw_frame
        if frame is None:
            continue
        try:
            padded, scale, pad_x, pad_y = _yolo_letterbox(frame)
            blob = padded[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
            blob = np.expand_dims(blob, 0)
            out = _yolo_sess.run(None, {input_name: blob})[0][0]  # (300, 6)

            dets = []
            for x1, y1, x2, y2, conf, cls in out:
                if conf < YOLO_CONFIDENCE:
                    continue
                # Retour aux coordonnées de la frame d'origine (retire le padding/scale)
                ox1, oy1 = (x1 - pad_x) / scale, (y1 - pad_y) / scale
                ox2, oy2 = (x2 - pad_x) / scale, (y2 - pad_y) / scale
                cls = int(cls)
                label = _COCO_NAMES[cls] if 0 <= cls < len(_COCO_NAMES) else str(cls)
                dets.append({
                    "label": label,
                    "conf":  round(float(conf), 2),
                    "x":     int((ox1 + ox2) / 2),
                    "y":     int((oy1 + oy2) / 2),
                    "cam":   "ugreen",
                })
            _push_json(PUSH_YOLO_URL, dets)
        except Exception as e:
            print(f"[face_id] YOLO erreur : {e}", flush=True)


def detection_loop() -> None:
    # Délai de démarrage : après un crash + redémarrage par _supervise (3s),
    # le kernel peut mettre 2-5s à libérer le device USB de l'instance précédente.
    time.sleep(3)
    cap_ref      = [open_camera()]
    last_results = []

    threading.Thread(target=_capture_loop, args=(cap_ref,), daemon=True).start()
    threading.Thread(target=_yolo_loop, daemon=True).start()

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
        found = []

        for face in faces:
            x1, y1, x2, y2 = [int(v) for v in face.bbox]
            name, score = identify(face.normed_embedding)
            entry = {"name": name, "score": round(score, 3), "bbox": [x1, y1, x2, y2]}
            if name == "Inconnu":
                entry["embedding"] = face.normed_embedding.tolist()
            found.append(entry)

        state = {"faces": found, "ts": time.time()}
        _write_state(state)


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    detection_loop()
