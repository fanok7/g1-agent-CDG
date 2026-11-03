# Vision déportée sur la Jetson Thor

Ce fichier documente le découpage vision entre les deux Jetson du robot :
l'**Orin NX** (celui qui fait tourner `main.py`, l'agent, le SDK Unitree) et la
**Jetson Thor** (192.168.123.166), une seconde machine branchée en Ethernet sur
le même réseau robot, utilisée uniquement pour sa puissance GPU.

## Pourquoi

L'Orin NX n'a pas de `onnxruntime` GPU fonctionnel : aucun wheel précompilé ne
supporte son architecture pour les modèles utilisés ici (InsightFace, YOLO en
ONNX), et `face_id.py` y tournait donc entièrement sur CPU — jusqu'à **610%
CPU sur 8 cœurs**, saturant la machine qui doit aussi gérer l'audio temps réel,
les bras, la tablette.

La Thor a un vrai GPU CUDA (architecture Blackwell, très récente — JetPack
R38.2.1). Son `onnxruntime-gpu` a dû être **compilé depuis les sources** (aucun
wheel précompilé ne couvre encore cette architecture), mais une fois fait, il
tourne correctement.

**Répartition retenue** : la Thor capture les caméras et infère les modèles
(GPU, puissance) ; l'Orin NX se contente de recevoir les résultats et de faire
tourner l'agent (logs, logique, décisions).

## Schéma

```
┌─ Jetson THOR (192.168.123.166) ──────────────┐        ┌─ Jetson Orin NX (192.168.123.164) ─────────────┐
│  Caméras branchées ICI physiquement :         │        │  main.py (agent, asyncio)                       │
│   • UGREEN 2K (reconnaissance faciale + YOLO) │        │   ├─ hardware.init() (bras, audio, DDS eth0)    │
│   • (HBVCAM : plus branchée)                  │        │   ├─ thor_remote.ensure_thor_vision_running()   │
│   • (RealSense : restée sur l'Orin, inutilisée)│        │   │     └─ SSH (clé) → redémarre le service     │
│                                                │        │   │        systemd sur la Thor, à chaque lancement│
│  g1-face-id.service (systemd --user)          │        │   └─ _supervise(vision_server.py)                │
│   vision/face_id/face_id.py                   │        │        vision/vision_server.py (Flask :8080)   │
│   • InsightFace (buffalo_m) → CUDAExecutionProvider     │        │         reçoit tout, ne calcule RIEN            │
│   • YOLO (yolo26n.onnx)     → CUDAExecutionProvider     │        │                                                  │
│   • capture UGREEN, partagée entre les deux   │        │                                                  │
│     (voir CAMERA_IDS — nommée "ugreen" dans   │        │                                                  │
│     les routes/IPC pour des raisons historiques,│        │                                                  │
│     l'UGREEN est bien la caméra ouverte)       │        │                                                  │
│                                                │  POST  │  ┌────────────────────────────────────────┐      │
│   ──────────────────────────────────────────► │ ─────► │  │ /push_face_state    → face_id_state.json│      │
│   ──────────────────────────────────────────► │ ─────► │  │ /push_ugreen_frame  → latest_ugreen.jpg │      │
│   ──────────────────────────────────────────► │ ─────► │  │ /push_yolo_ugreen   → fusion YOLO       │      │
│                                                │        │  └────────────────────────────────────────┘      │
│  g1-realsense-push.service : DÉSACTIVÉ        │        │  /push_realsense : plus personne ne l'alimente  │
│   (la D435I est restée sur l'Orin, inutilisée │        │   → latest_realsense.jpg jamais écrit, donc     │
│    — pyrealsense2 ne voit rien sur la Thor)   │        │     scanner_billet sans source                  │
└────────────────────────────────────────────────┘        │  ── Consommateurs de ces fichiers ──             │
                                                            │   • tools/face_id_tool.py  (identifier_personne)│
                                                            │   • tools/vision_tool.py   (ce_que_je_vois)     │
                                                            │   • tools/qr_tool.py       (scanner_billet)     │
                                                            │   • agent/events.py        (face_greeting_loop) │
                                                            │        → salue le visiteur reconnu, à l'oral,   │
                                                            │          en injectant dans la conversation LLM │
                                                            └──────────────────────────────────────────────┘
```

## Composants

| Composant | Où | Rôle |
|-----------|-----|------|
| `vision/face_id/face_id.py` | **Thor** | InsightFace (visages) + YOLO (objets), sur la même frame UGREEN 2K capturée une seule fois (voir CAMERA_IDS, où `UGREEN` est en tête). POST les résultats. |
| `vision/realsense_remote_push.py` | ~~Thor~~ | **Hors service** : la D435I est restée branchée sur l'Orin NX et n'est plus utilisée. Service `g1-realsense-push` désactivé (il bouclait sur un crash, `pyrealsense2` ne voyant aucun device sur la Thor). |
| `vision/vision_server.py` | **Orin NX** | Ne fait plus AUCUNE inférence. Reçoit les POST, fusionne UGREEN+RealSense, écrit `/tmp/vision_state.json`, sert le stream MJPEG et `/detections`. |
| `robot/thor_remote.py` | **Orin NX** | Appelé une fois au démarrage de `main.py` : SSH vers la Thor, redémarre les deux services systemd pour un état propre à chaque session. Best-effort (Thor injoignable ⇒ log, pas de crash). |

## Endpoints HTTP (`vision_server.py`, port 8080)

| Route | Émetteur | Contenu | Écrit dans |
|-------|----------|---------|------------|
| `POST /push_face_state` | face_id.py (Thor) | JSON `{"faces":[...], "ts":...}` | `/tmp/face_id_state.json` |
| `POST /push_ugreen_frame` | face_id.py (Thor) | JPEG brut | `/tmp/latest_ugreen.jpg` |
| `POST /push_yolo_ugreen` | face_id.py (Thor) | JSON `[{"label","conf","x","y","cam"}, ...]` | fusionné dans `/tmp/vision_state.json` |
| `POST /push_realsense` | realsense_remote_push.py (Thor) | JPEG brut | `/tmp/latest_realsense.jpg` |
| `GET /detections` | — | JSON détections courantes | — |
| `GET /stream` | — | MJPEG : **objets YOLO + visages fusionnés** sur la même frame UGREEN | — |
| `GET /stream/face` | — | Alias de `/stream` (même caméra, mêmes annotations depuis la fusion) | — |
| `GET /stream/realsense` | — | MJPEG RealSense brut — **plus alimenté** | — |

Ces fichiers `/tmp/*` sont exactement ceux que lisaient déjà les tools avant ce
changement (voir § IPC de [CLAUDE.md](CLAUDE.md)) — leur contrat n'a pas changé,
seule la source d'écriture est maintenant distante.

## Persistance côté Thor

Les deux scripts tournent en **services systemd `--user`** (pas de sudo requis
pour les gérer) :

```bash
systemctl --user status  g1-face-id.service g1-realsense-push.service
systemctl --user restart g1-face-id.service g1-realsense-push.service
systemctl --user status  g1-face-id.service --no-pager -l -n 60   # logs
```

- `Restart=always` : redémarre seul en cas de crash (driver GPU, caméra qui
  décroche…).
- `loginctl enable-linger unitree` a été activé : les services démarrent au
  boot de la Thor même sans session utilisateur active, et survivent à une
  déconnexion SSH (contrairement à un simple `nohup` lancé depuis une session
  SSH, qui peut mourir avec elle).
- Fichiers unités : `~/.config/systemd/user/g1-face-id.service` et
  `g1-realsense-push.service` **sur la Thor**.

`main.py` les redémarre en plus à chaque lancement (`thor_remote.py`) pour un
état propre en début de session — mais ils survivraient de toute façon sans
cet appel, la persistance systemd est indépendante.

## Connexion Orin NX → Thor

SSH par **clé** (pas de mot de passe en dur dans le code) :
- Utilisateur `unitree` des deux côtés.
- Clé publique de l'Orin NX (`~/.ssh/id_ed25519.pub`) ajoutée à
  `~/.ssh/authorized_keys` sur la Thor.
- `robot/thor_remote.py` utilise `BatchMode=yes` : si l'auth par clé échoue,
  ça ne bloque jamais sur un prompt de mot de passe — timeout propre à la place.

## Environnement Python sur la Thor

Venv dédié : `~/g1_vision_venv` (Python 3.12, JetPack R38.2.1 / Ubuntu 24.04).

Dépendances clés :
- `onnxruntime-gpu` — **compilé depuis les sources** (`~/onnxruntime`, tag
  `v1.30.0`), le wheel PyPI générique ne supporte pas l'architecture GPU de la
  Thor (`cudaErrorNoKernelImageForDevice` sinon). Compilé avec
  `CMAKE_CUDA_ARCHITECTURES=native` pour cibler exactement ce GPU.
- `insightface`, `pyrealsense2`, `opencv-python-headless`, `flask`, `requests`
  — wheels précompilés standards, aucun souci.
- `torch` (CPU-only) + `ultralytics` sont installés mais **ultralytics n'est
  pas utilisé pour l'inférence YOLO** : sa détection GPU dépend de
  `torch.cuda.is_available()`, toujours faux avec un torch CPU-only (un torch
  CUDA demanderait un nouveau build source aussi long que celui
  d'onnxruntime). L'inférence YOLO passe par `onnxruntime` directement
  (`_yolo_sess` dans `face_id.py`), qui lui tourne bien sur
  `CUDAExecutionProvider`.

CUDA système : `/usr/local/cuda-13.0` + `libcudnn9-cuda-13` installés via les
dépôts NVIDIA JetPack (`apt`).

## Modules déjà GPU sur la Thor

| Modèle | Format | Provider |
|--------|--------|----------|
| InsightFace `buffalo_m` | `.onnx` (via insightface) | `CUDAExecutionProvider` |
| YOLO `yolo26n` | `.onnx` (export "end-to-end", NMS intégré) | `CUDAExecutionProvider` |

## Diagnostic rapide

| Symptôme | Vérifier |
|----------|----------|
| `/tmp/face_id_state.json` ne se met plus à jour | `ssh unitree@192.168.123.166 systemctl --user status g1-face-id.service` |
| `/tmp/latest_realsense.jpg` périmé | `ssh unitree@192.168.123.166 systemctl --user status g1-realsense-push.service` |
| `[THOR] Injoignable (timeout SSH)` au démarrage de `main.py` | Thor éteinte / débranchée / Ethernet coupé — l'agent continue sans vision (dégradé), rien ne bloque |
| Détections YOLO jamais reçues mais visages oui | Vérifier les logs `g1-face-id.service` côté Thor : `[face_id] YOLO erreur : ...` ou `YOLO non chargé` |
| `vision_server` crashe au lancement avec « Address already in use » | Un ancien `vision_server.py` orphelin tient encore le port 8080 côté Orin NX — `pkill -f vision_server.py` |
