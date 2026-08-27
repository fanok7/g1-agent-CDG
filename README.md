# G1 Agent Aéroportuaire 

Model IA agent vocal d'accueil aéroportuaire, et plus spécifiquement pour Charles de Gaulle aéroport, tournant sur un robot humanoïde **Unitree G1 EDU** (ordinateur Jetson Orin NX).

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

- **[TABLETTE.md](TABLETTE.md)** — écran tactile du robot : liaison USB (`adb reverse`),
  serveur SSE, interface, **dépannage et pistes d'évolution.** À lire avant toute
  intervention sur la tablette.
- **[DASHBOARD.md](DASHBOARD.md)** — dashboard de télémétrie Streamlit : lancement,
  lecture, export Excel. **Mode d'emploi complet.**
- **[STATISTIQUES.md](STATISTIQUES.md)** — la télémétrie côté données (ce qui est
  mesuré, anonymat, fichiers de données).
- **[ASSISTANCE.md](ASSISTANCE.md)** — appel d'assistance humaine (bouton 🆘 + vocal,
  salon Daily, alerte n8n/ntfy, sourdine de l'IA, audio par le robot). Contient la
  procédure de **changement de compte Daily**.

## Dashboard de télémétrie — démarrage rapide

Streamlit est installé sur le robot. On le lance sur le robot, on le consulte depuis
le PC. **Détails : [DASHBOARD.md](DASHBOARD.md).**

```bash
# sur le robot (SSH) — headless auto
cd /home/unitree/g1_agent_interim && bash scripts/lancer_dashboard.sh
```

Puis, dans le navigateur du PC : **`http://192.168.123.164:8501`**

> **La règle :** le serveur écoute sur `0.0.0.0`, donc les deux adresses du robot
> fonctionnent — il faut prendre **celle du réseau sur lequel ton PC est branché** :
>
> | Comment ton PC est relié au robot | Adresse à utiliser |
> |---|---|
> | Lien direct eth0 (le cas habituel — c'est par là que tu fais SSH) | **`192.168.123.164`** |
> | Même Wi-Fi que le robot | l'IP wlan0, `ip -4 -o addr show wlan0` (aujourd'hui `192.168.0.128`) |
>
> Le `Network URL` imprimé par Streamlit annonce l'adresse Wi-Fi : ce n'est **pas**
> forcément la bonne pour toi. En cas de doute : `ss -tnp | grep :22` sur le robot
> montre l'IP source de ta session SSH — utilise l'adresse du robot sur ce réseau-là.

Le dashboard va chercher les données via `G1_ROBOT_URL`, dont le défaut
(`http://192.168.123.164:8000`) convient tant qu'il tourne **sur le robot** ou sur un PC
relié en eth0. Il n'y a à le surcharger que si le robot n'est joignable que par le Wi-Fi :

```bash
G1_ROBOT_URL="http://<IP-wlan0-du-robot>:8000" bash scripts/lancer_dashboard.sh
```

**Arrêt :** `pkill -f "streamlit run"`.

**Les données brutes sans passer par le dashboard** — `main.py` sert le journal de
télémétrie complet en téléchargement :

```text
http://192.168.123.164:8000/api/export-logs   →  analytics_events_<date>.jsonl
```

C'est la source qu'utilise le dashboard lui-même. Une ligne JSON par événement
(question, tool, note, appel, alerte, santé) — anonyme, voir [STATISTIQUES.md](STATISTIQUES.md).

> ℹ️ L'ancien dashboard `/admin` (Chart.js, servi par la tablette) **a été supprimé**,
> ainsi que le rapport HTML autonome. Le dashboard Streamlit les remplace tous les deux.
>
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
DAILY_API_KEY=          # appel d'assistance (salon Daily.co) — aujourd'hui dans ~/.env
ASSIST_AUDIO=robot      # 'robot' (défaut) ou 'tablette' — voir ASSISTANCE.md
```

> `config.py` charge le `.env` du repo **puis** `~/.env` : les deux emplacements
> fonctionnent, mais en cas de doublon **c'est celui du repo qui gagne** (`load_dotenv`
> n'écrase pas une variable déjà chargée). `DAILY_API_KEY` vit aujourd'hui dans `~/.env`.

### Mode d'accueil

Le personnage et les tools mis en avant dans le prompt dépendent du mode choisi **au
lancement**, via `--mode` :

| Mode | Flag | Usage |
|------|------|-------|
| **I-Interim** | `--mode iinterim` | Accueil agence intérim — agenda (RDV déjà pris), prise de RDV en self-service (QR Google Calendar), formations/badges CORSUR, Gmail, transport IDF |
| **CDG** | `--mode cdg` | Accueil Terminal 2F CDG — vols temps réel, Google Maps, transport IDF |

Sans `--mode`, `main.py` retombe sur `_DEFAULT_MODE` codé en bas de `config.py`
(aujourd'hui `"cdg"`) — modifiable directement si tu préfères ne jamais avoir à passer le
flag sur ce robot.

---

## Lancement

### Le robot (mode en service aujourd'hui — manuel)

```bash
conda deactivate                       # si un env conda est actif — main.py veut le 3.8 SYSTÈME
cd /home/unitree/unitree_sdk2_python && python3.8 /home/unitree/g1_agent_interim/main.py --mode cdg
```

`--mode cdg` ou `--mode iinterim` choisit le personnage actif (voir § Mode d'accueil
ci-dessus) — omissible, `main.py` retombe alors sur `_DEFAULT_MODE` dans `config.py`.

> **Le répertoire de travail doit être `/home/unitree/unitree_sdk2_python`.** Le SDK Unitree initialise la couche DDS avec des chemins relatifs à ce dossier et lie l'interface `eth0` vers le robot ; lancé ailleurs, la connexion au robot échoue silencieusement.

`main.py` fait tout le reste : init hardware (micro, HP, bras, LED), serveur de la
tablette sur le port 8000, démarrage supervisé des sous-processus vision, connexion
OpenAI, boucles asyncio. Un sous-processus qui crashe est relancé automatiquement, et la
WebSocket OpenAI se reconnecte seule (backoff 2→30 s).

Au démarrage, la console affiche le lien de l'écran :
`[TABLETTE] Ouvre ce lien pour voir l'écran : http://<ip>:8000`.

**Arrêt :** `Ctrl+C`. Si un `main.py` fantôme tient encore le port 8000 :
`pkill -f g1_agent_interim/main.py`.

### Vérifier que la tablette suit

```bash
adb devices                 # 0123456789ABCDEF   device
adb reverse --list          # UsbFfs tcp:8000 tcp:8000
curl -s localhost:8000/ping # {"ok":true}
```

Détails, pannes et évolutions : **[TABLETTE.md](TABLETTE.md)**.

### Les deux autres modes de démarrage

Ils tiennent tous les deux le **port 8000** : n'en activer **qu'un seul à la fois**, sinon
`Address already in use`.

**Launcher** — affiche une page « Démarrer » sur la tablette tant que `main.py` ne tourne
pas, et le lance d'un tap (`launcher_server.py`). Le service est **déjà installé sur le
robot**, simplement désactivé :

```bash
sudo systemctl enable --now g1-launcher    # activer
sudo systemctl disable --now g1-launcher   # revenir au mode manuel

# sur une machine neuve, installer d'abord l'unité :
sudo cp scripts/g1-launcher.service /etc/systemd/system/ && sudo systemctl daemon-reload
```

**Auto-start** — `main.py` démarré au boot par systemd et relancé s'il plante (installe
aussi la synchro NTP, indispensable au TLS, et désactive le launcher) :

```bash
bash scripts/installer_autostart.sh     # une seule fois, demande le sudo

systemctl status g1-main                # état
journalctl -u g1-main -f                # logs
sudo systemctl restart g1-main          # redémarrer
sudo systemctl disable --now g1-main    # revenir au mode manuel
```

### Services annexes

| Service | Rôle | État |
|---------|------|------|
| `adb-reverse-tablet` | Maintient le tunnel USB vers la tablette (port 8000) | actif au boot |
| `g1-dashboard` | Superviseur ESP32 `dashboard.py` (Flask, port 8888) | actif au boot |
| `g1-launcher` | Page « Démarrer » (port 8000) | installé, désactivé |
| `g1-main` | `main.py` au boot (port 8000) | fichier fourni, **non installé** |

```bash
for s in adb-reverse-tablet g1-dashboard g1-launcher g1-main; do
  printf "%-22s %s / %s\n" "$s" "$(systemctl is-enabled $s 2>&1)" "$(systemctl is-active $s 2>&1)"
done
```

