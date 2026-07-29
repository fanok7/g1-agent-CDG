# G1 Agent Aéroportuaire 

Model IA agent vocal d'accueil aéroportuaire, et plus spécifiquement pour Charles de Gaulle aéroport, tournant sur un robot humainoïde **Unitree G1 EDU** (ordinateur Jetson Orin NX).

Seul le LLM est distant, l'audio du micro part en streaming vers l'**API OpenAI Realtime**, qui renvoie soit de l'audio à jouer, soit un appel de tool soit les deux. **Tout le reste — capture micro, lecture haut-parleur, vision, gestes, exécution des tools — tourne en local sur le Jetson.**

```text
┌─ Cloud OpenAI ──────────────┐         ┌─ Jetson Orin NX (local) ──────────────┐
│ gpt-realtime-mini           │◀──WSS──▶│ main.py (asyncio)                     │
│  • écoute l'audio micro     │ audio + │  • capture micro / joue le HP         │
│  • décide les tool calls    │ events  │  • exécute les tools                  │
│  • génère l'audio réponse   │         │  • supervise vision + librespot       │
└─────────────────────────────┘         └───────────────────────────────────────┘
``` 
## Vidéo de démonstration 1
[![Demo G1 Agent à l'aéroport](https://img.youtube.com/vi/fDE_c2kgEHU/maxresdefault.jpg)](https://www.youtube.com/watch?v=fDE_c2kgEHU)

## Vidéo de démonstration 2
[![Demo](https://img.youtube.com/vi/pPBh3XzLi3g/hqdefault.jpg)](https://www.youtube.com/watch?v=pPBh3XzLi3g)

## Documentation par fonctionnalité

- **[DASHBOARD.md](DASHBOARD.md)** — dashboard de télémétrie Streamlit : lancement,
  lecture, export Excel. **Mode d'emploi complet.**
- **[STATISTIQUES.md](STATISTIQUES.md)** — la télémétrie côté données (ce qui est
  mesuré, anonymat, fichiers de données).
- **[ASSISTANCE.md](ASSISTANCE.md)** — appel d'assistance humaine (bouton 🆘 + vocal,
  Daily.co, audio par le robot, écran d'avertissement).

## Dashboard de télémétrie — démarrage rapide

Streamlit est installé sur le robot. On le lance sur le robot, on le consulte depuis
le PC. **Détails : [DASHBOARD.md](DASHBOARD.md).**

```bash
# sur le robot (SSH) — headless auto
cd /home/unitree/g1_agent_interim && bash scripts/lancer_dashboard.sh
```
Puis, dans le navigateur du PC : **http://192.168.123.164:8501**

> ⚠️ `dashboard_stats.py` ≠ `dashboard.py` : ce dernier est le **superviseur ESP32**
> (Flask, port 8888, lancé au boot), à ne pas confondre.

---

## Installation

L'agent s'appuie sur **deux interpréteurs Python séparés** sur le Jetson, chacun avec ses dépendances — ils ne sont pas interchangeables :

| Interpréteur | Chemin | Rôle | Pourquoi séparé |
|--------------|--------|------|-----------------|
| **Python 3.8 système** | `/usr/bin/python3.8` | `main.py`, `vision_server.py`, détection chute/feu | Seule version compatible avec le SDK Unitree (DDS) et le runtime TensorRT |
| **Python miniconda** | `/home/unitree/miniconda3/bin/python3` | `face_id.py` (reconnaissance faciale) | InsightFace requiert CUDA/onnxruntime-gpu, indisponible en 3.8 système |

`main.py` lance et supervise `face_id.py` en sous-processus avec le bon interpréteur — tu n'as jamais à jongler entre les deux à la main.

### 1. Dépendances Python de l'agent (3.8 système)

```bash
python3.8 -m pip install -r requirements.txt
```

Couvre le cœur (audio, WebSocket, tools) **et** la vision embarquée (`ultralytics`, `pyrealsense2`, `opencv`). Le SDK Unitree (`unitree_sdk2py`) est déjà installé sur le robot dans `/home/unitree/unitree_sdk2_python` — **ne pas le réinstaller.**

### 2. Reconnaissance faciale (miniconda)

InsightFace tourne dans l'environnement miniconda. Les modèles (`buffalo_sc`) se téléchargent au premier lancement. Vérifier que l'import passe :

```bash
/home/unitree/miniconda3/bin/python3 -c "import insightface; print('OK')"
```

### 3. Modèle YOLO (TensorRT)

`vision_server.py` charge `/home/unitree/yolo26n.engine`. **Un engine TensorRT n'est pas portable** : il est compilé pour un GPU précis et doit être généré *sur le Jetson*. S'il manque, l'exporter depuis les poids `.pt` :

```bash
python3.8 -c "from ultralytics import YOLO; YOLO('yolo26n.pt').export(format='engine')"
mv yolo26n.engine /home/unitree/yolo26n.engine
```

Même logique pour les modules chute/feu (désactivés par défaut) : voir `vision/fall_detection/scripts/export_tensorrt.py`.

### 4. Musique Spotify (optionnel)

La lecture Spotify passe par **librespot** (client Spotify Connect natif), installé via cargo dans `~/.cargo/bin/librespot`. Auth OAuth une seule fois :

```bash
python3.8 scripts/spotify_setup.py
```

### 5. Tokens OAuth Google (optionnel)

Agenda et Gmail nécessitent chacun un token OAuth généré une fois. Les scripts ouvrent un serveur local sur le port 8080 — depuis un poste distant, faire suivre le port : `ssh -L 8080:localhost:8080 unitree@192.168.123.164`.

```bash
python3.8 scripts/calendar_setup.py   # Google Agenda (lecture + création de RDV)
python3.8 scripts/gmail_setup.py      # Gmail (lecture + envoi)
```

---

## Configuration

### Clés API — fichier `.env`

Placé à la racine du repo (ou dans `~/.env`). Seule `OPENAI_API_KEY` est indispensable au démarrage ; les autres n'activent que les tools correspondants.

```text
OPENAI_API_KEY=         # obligatoire — LLM Realtime
SERPER_API_KEY=         # recherche_web
GOOGLE_MAPS_API_KEY=    # lieux / itinéraires
AIRLABS_API_KEY=        # vols temps réel
SPOTIFY_CLIENT_ID=      # musique
SPOTIFY_CLIENT_SECRET=
```

### Mode d'accueil

Le personnage et les tools exposés dépendent du prompt actif, à basculer en bas de `config.py` :

| Mode | Variable | Usage |
|------|----------|-------|
| **I-Interim** | `SYSTEM_PROMPT_IINTERIM` | Accueil agence intérim — prise de RDV, agenda, Gmail, transport IDF |
| **CDG** | `SYSTEM_PROMPT_CDG` | Accueil Terminal 2F CDG — vols temps réel, Google Maps, transport IDF |

---

## Lancement

```bash
cd /home/unitree/unitree_sdk2_python && python3.8 /home/unitree/g1_agent_interim/main.py
```

> **Le répertoire de travail doit être `/home/unitree/unitree_sdk2_python`.** Le SDK Unitree initialise la couche DDS avec des chemins relatifs à ce dossier et lie l'interface `eth0` vers le robot ; lancé ailleurs, la connexion au robot échoue silencieusement.

`main.py` fait tout le reste : init hardware (micro, HP, bras), démarrage supervisé des sous-processus vision, connexion OpenAI, boucles asyncio. Un sous-processus qui crashe est relancé automatiquement.

Pour piloter en plus la bouche/émotions via l'ESP32 (couche optionnelle) :

```bash
cd /home/unitree/unitree_sdk2_python && python3.8 /home/unitree/g1_agent_interim/launch.py
```

