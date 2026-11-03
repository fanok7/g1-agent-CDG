# Passation — installation SONIC (GR00T-WholeBodyControl)

Session du 2026-09-25. Complète `gr00t_handoff.md` (installation Isaac-GR00T).
Objectif : chemin **Deployment / Sim2Sim MuJoCo**, sans robot réel.

## Ce qui est installé

| Élément | Emplacement | Version |
|---|---|---|
| Repo | `~/projects/GR00T-WholeBodyControl` | clone du 2026-09-25 |
| TensorRT | `~/TensorRT-10.13.3.9` | 10.13.3.9 (wheels CUDA 13 + en-têtes du TAR) — **seule build avec les noyaux SM 120** |
| Binaire de déploiement | `gear_sonic_deploy/target/release/g1_deploy_onnx_ref` | build Release |
| Checkpoint SONIC v1.1 | `gear_sonic_deploy/policy/sonic_v1_1/` | encoder + decoder ONNX + observation_config |
| Planner cinématique | `gear_sonic_deploy/planner/target_vel/V2/planner_sonic.onnx` | — |
| Données d'exemple | `sample_data/` | 6 PKL (1 séquence de marche) |
| CUDA | `/usr/local/cuda` | 13.1.115 |
| Compilateur | GNU 15.2.0 | — |

L'environnement `uv` d'`Isaac-GR00T` n'a **pas été touché** : la pile de déploiement est
100 % C++/ONNX, aucun paquet Python partagé.

## Exports nécessaires

```bash
export PATH="$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$HOME/.local/share/gr00t-ffmpeg-env/lib:$LD_LIBRARY_PATH"
export CUDA_HOME=/usr/local/cuda
export TensorRT_ROOT=$HOME/TensorRT-10.13.0.35
```

Ajouté à `~/.bashrc` pour `TensorRT_ROOT`. Regroupés dans `~/projects/g1-stack/scripts/env.sh`.

## Commandes de relance

```bash
# Rebuild (si modification du C++)
cd ~/projects/GR00T-WholeBodyControl/gear_sonic_deploy
source scripts/setup_env.sh
timeout 3600 just build

# Sim2Sim — terminal 1 (simulateur MuJoCo)
cd ~/projects/GR00T-WholeBodyControl
source .venv_sim/bin/activate
python gear_sonic/scripts/run_sim_loop.py

# Sim2Sim — terminal 2 (déploiement SONIC v1.1)
bash ~/projects/g1-stack/scripts/sim2sim.sh
```

## ⚠️ L'étape `9` est obligatoire — sans elle, tout part en vrille

Séquence de démarrage complète, dans cet ordre :

1. `]` dans le terminal du contrôleur — démarre la politique
2. **Clic dans la fenêtre 3D MuJoCo, puis `9`** — pose le robot au sol
3. `T` dans le terminal — joue le mouvement

Sauter l'étape 2 ne produit **aucun message d'erreur** : le robot se met simplement à courir
et à exécuter n'importe quoi. Mesure sur la macarena livrée avec le dépôt :

| | amplitude mesurée / référence |
|---|---|
| sans le `9` | **1,42** — le robot fait plus que la référence |
| avec le `9` | **0,69** — suivi correct, amorti par la politique RL |

La fenêtre MuJoCo est une fenêtre graphique **hors tmux** : le `9` exige son focus et ne
peut pas être envoyé par `tmux send-keys`. Toute automatisation du démarrage doit donc
laisser cette étape à l'opérateur — et ne jamais lancer ces processus dans une session tmux
détachée, invisible pour lui.

Conséquence : plusieurs conclusions tirées d'essais automatisés sans cette étape sont
**non fiables** et à reprendre — notamment le suivi partiel observé en pilotage par
cinématique inverse (section « Saisie scriptée par ZMQ ») et le comportement lors du
premier essai de geste de préhension.

## Problèmes rencontrés et correctifs

| Problème | Cause | Correctif |
|---|---|---|
| `bash: Xmin: Aucun fichier ou dossier` | Placeholder `<Xmin>` laissé dans les commandes ; bash a interprété `<` et `>` comme des redirections. `install_deps.sh` n'a jamais tourné | Utiliser une vraie durée : `timeout 1800 ./scripts/install_deps.sh` |
| `error: No justfile found` | Le fichier est `.justfile` (caché) et `just` était lancé depuis un autre dossier | Lancer `just` depuis `gear_sonic_deploy/` |
| `Fail to find TensorRT, please set TensorRT_ROOT` | TensorRT pas encore installé — `setup_env.sh` affiche « TensorRT environment configured » même sans install réelle (message trompeur) | Télécharger TensorRT 10.13 TAR depuis NVIDIA Developer, extraire, `export TensorRT_ROOT=$HOME/TensorRT-10.13.0.35` |
| `msgpack not found. Install libmsgpack-dev` | **Écart doc** : sur Ubuntu 26.04, `libmsgpack-dev` est un paquet de transition vide qui n'installe que `libmsgpack-c-dev` (le C, `msgpack.h`). Le CMakeLists cherche `msgpack.hpp` (C++) | `sudo apt-get install -y libmsgpack-cxx-dev` |
| `[ChannelFactory] create domain error` au lancement du sim | **Faux positif** : `simulator_factory.py:19` et `base_sim.py:564` initialisent tous deux le domaine DDS. Le second appel échoue forcément (domaine déjà créé dans le process), l'erreur est attrapée et affichée | Aucun — message bénin. Vérifier que le process tourne (~50 % CPU) plutôt que se fier au message |
| `selected interface "lo" is not multicast-capable` | L'interface loopback d'Ubuntu n'a pas le flag MULTICAST, requis par le SDK Unitree (config `INTERFACE: "lo"` dans `wbc_configs/g1_29dof_sonic_model12.yaml`) | `sudo ip link set lo multicast on` (non persistant — à refaire après redémarrage) |
| `LD_LIBRARY_PATH : variable sans liaison` | `set -u` dans un script qui référence une variable non définie | Utiliser `${LD_LIBRARY_PATH:-}` |
| `CMAKE_PREFIX_PATH : variable sans liaison` (dans `setup_env.sh:120`) | Le `set -u` du script appelant se propage au `setup_env.sh` du repo, qui n'est pas écrit pour `nounset` | Ne pas utiliser `set -u` dans un script qui source `setup_env.sh` |
| `Error Code 9: Target GPU SM 120 is not supported by this TensorRT release` puis `core dumped` | **Le piège majeur sur RTX 5090.** TensorRT 10.13.**0** est une build CUDA 12.x qui ne contient pas les noyaux SM 120 (Blackwell). Le compute 12.0 n'est supporté qu'à partir de CUDA Toolkit 12.8+, et seule la 10.13.**3** existe en build CUDA 13 | Récupérer les bibliothèques 10.13.3.9 via les wheels NVIDIA (voir ci-dessous) — **sans re-télécharger le TAR de 10 Go ni recompiler** |

### Correctif TensorRT SM 120 (sans TAR, sans rebuild)

Les wheels NVIDIA fournissent les mêmes bibliothèques que le TAR, sans login et pour ~1,3 Go :

```bash
uv venv /tmp/trt_dl --python 3.10
uv pip install --python /tmp/trt_dl/bin/python --no-deps \
    --index-url https://pypi.nvidia.com "tensorrt-cu13-libs==10.13.3.9"
mkdir -p ~/TensorRT-10.13.3.9/lib
cp -a /tmp/trt_dl/lib/python3.10/site-packages/tensorrt_libs/. ~/TensorRT-10.13.3.9/lib/
rm -f ~/TensorRT-10.13.3.9/lib/libnvinfer_builder_resource_win.so.*   # 1,1 Go inutile (Windows)
rm -rf /tmp/trt_dl
```

Les wheels ne fournissent que les bibliothèques. Pour en faire un `TensorRT_ROOT` complet
(CMake cherche `include/NvInfer.h` et un `lib/libnvinfer.so` non versionné) :

```bash
cp -a ~/TensorRT-10.13.0.35/include ~/TensorRT-10.13.3.9/
cd ~/TensorRT-10.13.3.9/lib
for f in libnvinfer libnvinfer_plugin libnvonnxparser; do ln -sf "$f.so.10" "$f.so"; done
```

Les en-têtes 10.13.0 avec les bibliothèques 10.13.3 : même version mineure, même `SONAME`
(`libnvinfer.so.10`), ABI compatible.

Puis `export TensorRT_ROOT=$HOME/TensorRT-10.13.3.9` (déjà dans `scripts/env.sh`) et rebuild.

⚠️ **Ne pas tenter de contourner par `LD_LIBRARY_PATH` seul** : `deploy.sh` re-source
`setup_env.sh` en interne, ce qui replace `TensorRT_ROOT/lib` en tête du chemin et écrase la
priorité. Le moteur `.trt` généré par une version se refuse alors à charger sur l'autre :
`Error Code 6: The engine plan file is not compatible with this version of TensorRT`.
La seule approche robuste est de faire pointer `TensorRT_ROOT` sur la bonne install.

Après ce changement, l'ancienne `~/TensorRT-10.13.0.35` (~10 Go) n'est plus utilisée que
comme source des en-têtes déjà recopiées — elle peut être supprimée.

Moteurs TensorRT générés une fois puis réutilisés :
`gear_sonic_deploy/policy/sonic_v1_1/{encoder_model_encoder.trt, policy_model_decoder.trt}`
(48 Mo + 143 Mo). Les supprimer force une reconstruction.

### `install_inference.sh` échoue — nom de paquet incorrect

```
× Failed to download and build `isaac-gr00t @ git+https://github.com/NVIDIA/Isaac-GR00T.git`
╰─▶ Package metadata name `gr00t` does not match given name `isaac-gr00t`
```

**Écart doc / upstream** : l'extra `gear_sonic[inference]` déclare la dépendance sous le nom
`Isaac-GR00T`, alors que le dépôt NVIDIA se publie sous le nom `gr00t`. Le script est donc
cassé tel quel.

Contournement — construire `.venv_inference` à la main, en épinglant le **commit exact du
clone local** plutôt que la branche `main`, pour rester sur la version stabilisée :

```bash
cd ~/projects/GR00T-WholeBodyControl
uv venv .venv_inference --python 3.10
uv pip install --python .venv_inference/bin/python \
    pyzmq msgpack msgpack-numpy pin tyro opencv-python scipy
uv pip install --python .venv_inference/bin/python \
    "gr00t @ git+https://github.com/NVIDIA/Isaac-GR00T.git@23ace64f17aa5015259b8609d371eb61a357c776"
uv pip install --python .venv_inference/bin/python --no-deps -e gear_sonic
```

Vérification : `from gr00t.policy.server_client import PolicyClient` et
`from gear_sonic.camera.composed_camera import ComposedCameraClientSensor` doivent passer.
L'env d'`Isaac-GR00T` n'est pas touché — seul le code source est récupéré depuis GitHub.

### Caméra en simulation : native, pas besoin de script

`run_sim_loop.py` sait déjà rendre et publier la vue égocentrique, inutile d'écrire un
publieur ZMQ :

```bash
python gear_sonic/scripts/run_sim_loop.py --enable-offscreen --enable-image-publish
```

Il rend la caméra `head_camera` du modèle G1
(`decoupled_wbc/control/robot_model/model_data/g1/g1_29dof_old.xml:240`) en 640×480 et la
publie sur le port **5555** sous la clé **`ego_view`** — exactement ce que
`run_vla_inference.py` consomme (`camera_msg["images"]["ego_view"]`).

Protocole : ZMQ PUB, msgpack, `{"timestamps": {"ego_view": float}, "images": {"ego_view": jpeg}}`.

### Saisie scriptée par ZMQ : ce qui marche, ce qui bloque

Objectif : produire des démonstrations de manipulation sans casque VR, en pilotant SONIC
par trajectoires articulaires (protocole ZMQ v1), pour alimenter un fine-tuning VLA.

**Acquis** (`scripts/reach_object.py`) :

- Position des objets lue dans la scène MuJoCo, exprimée **dans le repère du bassin** —
  le modèle Pinocchio a une base fixe à l'origine alors que le bassin est à 0,793 m dans
  le monde. Sans ce changement de repère, l'IK échoue de ~48 cm.
- IK par moindres carrés amortis : converge en moins de 15 itérations, erreur < 4 mm, à
  condition **d'inclure les 3 articulations de la taille** dans la chaîne. Le bras seul
  n'atteint pas une table à 55 cm (erreur résiduelle 23 cm).
- Posture obtenue vérifiée visuellement : penché au-dessus de la table, sans contorsion.
- Publication acceptée par le contrôleur, format validé par ses propres logs :
  `Received ZMQ message - topic: 'pose', protocol_version: 1`, `29 joints`.
- Mode streaming activé par `ENTER` (le message `ZMQ STREAMING MODE: ENABLED` est noyé
  dans les logs de boucle — chercher avec `tmux capture-pane -S -3000 | grep STREAMING`).

**Limite rencontrée.** Le robot bouge et se rapproche (écart articulaire 1,54 → 1,16 rad
au plus près) mais ne converge pas sur la posture demandée.

Explication : **SONIC est un contrôleur de suivi de mouvement, pas un asservissement de
position.** Entraîné par RL sur des mouvements humains, il produit face à une posture
statique de saisie — hors distribution — un mouvement plausible et équilibré, non la
reproduction exacte des angles. Le protocole v1 passe de surcroît par l'encodeur, qui
compresse la référence en token latent : l'information de position exacte se perd.

**Pistes investiguées ensuite — résultats :**

**1. Cibles VR 3 points → fermée.** L'interface ZMQ du contrôleur ne lit que neuf champs :
`body_pos`, `body_quat`, `body_quat_w`, `frame_index`, `joint_pos`, `joint_vel`,
`last_smpl_global_frames`, `smpl_joints`, `smpl_pose`
(`gear_sonic_deploy/src/g1/g1_deploy_onnx_ref/include/input_interface/zmq_endpoint_interface.hpp`).
Aucun champ VR 3 points : ces cibles sont **dérivées en interne** des poses SMPL streamées.
Commander une position de main passe donc obligatoirement par une pose humaine SMPL
(protocole v2/v3), pas par des cibles cartésiennes.

**2. Protocole v4 → viable mais circulaire.** Spécification (même fichier, l. 82-88) :

```
REQUIRED: token_state (motion token array)
OPTIONAL: frame_index, left_hand_joints, right_hand_joints, body_quat_w
```

→ **C'est ce canal qui pilote les mains** (question laissée ouverte par le protocole v1,
limité aux 29 articulations du corps).
Mais les tokens 64D appartiennent à l'espace latent appris de SONIC : impossible de les
écrire à la main. Les produire suppose de passer une motion dans `model_encoder.onnx` — or
si l'on dispose déjà d'une motion, le protocole v1 l'encode lui-même. La v4 n'a donc
d'intérêt réel que pour transporter la sortie d'un VLA.

**3. Mouvement dynamique in-distribution → la voie à suivre.**
BONES-SEED contient **14 643 mouvements « Interactions »** (pick-and-place, portage,
manipulation d'outils), sur lesquels SONIC a précisément été entraîné — ils sont donc
in-distribution par construction. Le dataset fournit `g1.tar.gz` (déjà reciblé G1, donc
streamable tel quel en protocole v1) et `metadata/seed_metadata_v004.csv` avec des
descriptions en langage naturel, pour y chercher les gestes de préhension.

⚠️ Dépôt **gated** : `GatedRepoError 403`. Accepter la licence une fois sur
https://huggingface.co/datasets/bones-studio/seed en étant connecté ; l'authentification
`hf auth login` existante suffit ensuite.

**Conséquence méthodologique.** L'échec de l'approche IK n'était pas un problème de
transport (les trames passaient, le contrôleur les décodait) mais de **contenu** : une
posture statique issue d'une IK est hors distribution. L'approche correcte inverse le
problème — prendre un vrai mouvement de saisie du dataset et **placer l'objet là où la main
passe**, en ajustant les coordonnées dans `OBJECTS` de `scripts/build_scene.py`.

### Pipeline de conversion des motions — validé

Chaîne officielle, testée de bout en bout sur `sample_data/` :

```
CSV Bones-SEED  →  convert_soma_csv_to_motion_lib.py  →  PKL motion_lib
                →  export_motion_to_deploy.py         →  dossier reference/<nom>/
```

`sample_data/robot_filtered/*.pkl` est **déjà** au format motion_lib
(`root_trans_offset`, `pose_aa`, `dof`, `root_rot`, `smpl_joints`, `fps`) : l'étape de
conversion ne sert qu'aux CSV bruts de Bones-SEED.

```bash
.venv_inference/bin/python gear_sonic/data_process/export_motion_to_deploy.py \
    sample_data/robot_filtered/210531/walk_forward_amateur_001__A001.pkl \
    --output-dir <dest> --target-fps 50 --zero-root-xy
```

Sortie vérifiée conforme aux motions du dépôt : mêmes 8 fichiers
(`joint_pos`, `joint_vel`, `body_pos`, `body_quat`, `body_lin_vel`, `body_ang_vel`,
`info.txt`, `metadata.txt`) et mêmes en-têtes. Copier le dossier produit dans
`gear_sonic_deploy/reference/<jeu>/` le rend jouable par `N` / `P` / `T`.

⚠️ **À exécuter avec `.venv_inference`, pas `.venv_sim`.** Le script tire la pile
d'entraînement, absente de l'environnement de simulation. Dépendances manquantes ajoutées
une à une : `easydict`, `joblib`, `loguru`, `hydra-core`, `lxml`, `open3d`.

### Catalogue BONES-SEED

`metadata/seed_metadata_v004.csv` (150 Mo, téléchargeable seul) décrit les 142 220
mouvements : catégorie, durée, accessoires, position du corps, jusqu'à 4 descriptions en
langage naturel, et le chemin `move_g1_path` du CSV reciblé G1.

Recherche par mots-clés de préhension : **16 956 résultats**, dont 8 479 non-miroir.
En restreignant au package `Interactions`, debout, 2,5–7,5 s : **2 625 candidats**.
Exemple le mieux adapté à une saisie sur table :
`small_heavy_one_hand_pick_up_front_medium_R_001__A509`
(« picks up small heavy object with one hand from medium height up front »).

⚠️ Les CSV ne sont **pas téléchargeables individuellement** (404) : tout est dans
`g1.tar.gz`, **23,5 Go**.

### Incompatibilité msgpack entre les deux dépôts (pont VLA)

Symptôme, à chaque inférence, le pont plante et le serveur journalise :

```
AssertionError: Video key 'ego_view' must be a numpy array. Got <class 'dict'>
```

**Cause racine.** `gear_sonic/utils/data_collection/zmq_state_subscriber.py:54` appelle
`msgpack_numpy.patch()`, qui remplace **globalement** `msgpack.packb` dans le processus du
pont. Les tableaux numpy y sont dès lors encodés au format msgpack-numpy
(`b'nd'`, `b'type'`, `b'kind'`, `b'shape'`, `b'data'`) au lieu du format propre à GR00T
(`__ndarray_class__` + `as_npy`). Le serveur n'importe pas `gear_sonic`, conserve le
`unpackb` d'origine, ne reconnaît pas ces clés et transmet le dict brut au modèle.

C'est une incompatibilité entre les deux dépôts, pas une erreur de configuration.

**Piège de diagnostic** : un client de test isolé fonctionne parfaitement, car il n'importe
pas le module d'état qui déclenche le patch. Il faut reproduire avec les imports du pont.

**Correctif** : lancer le serveur via `scripts/run_policy_server.py`, qui étend son décodeur
pour accepter les deux encodages. Aucun fichier n'est modifié.

```bash
cd ~/projects/Isaac-GR00T
~/projects/GR00T-WholeBodyControl/.venv_inference/bin/python \
    ~/projects/g1-stack/scripts/run_policy_server.py \
    --model-path ~/projects/g1-stack/checkpoints/vla/sonic-community-10000 \
    --embodiment-tag unitree_g1_sonic --device cuda:0 --port 5550
```

Le sens retour (actions serveur → pont) n'a pas besoin de correctif : le pont décode le
format GR00T via son `object_hook` explicite.

Résultat validé : chunks d'action toutes les ~199 ms, zéro erreur serveur.

### Le pont démarre en pause

`run_vla_inference.py` initialise `pause_loop = True`. Le VLA infère mais n'envoie rien au
robot tant que `p` n'a pas été envoyé depuis le publieur clavier. Le pane affiche
`Pausing....` entre les chunks — c'est l'état normal avant la reprise, pas une erreur.

### Conflit de port avec le serveur GR00T

`zmq::error_t: Address already in use` au démarrage du contrôleur : le port de debug par
défaut **5557** est occupé par le serveur de politique VLA
(`gr00t/eval/run_gr00t_server.py`, lancé depuis `Isaac-GR00T`), qui tourne en continu.

SONIC est donc décalé sur **5567** via `--zmq-out-port`. Conséquence : le visualiseur doit
cibler ce port.

```bash
python visualize_motion.py --realtime_debug_url tcp://localhost:5567
```

⚠️ `deploy.sh` ne parse qu'une liste fermée d'options et interprète toute option inconnue
comme un **nom d'interface réseau** — `--zmq-out-port` y est donc silencieusement avalé.
C'est pourquoi `sim2sim.sh` appelle directement `target/release/g1_deploy_onnx_ref` avec les
arguments que `deploy.sh` aurait construits, plus le port.

### Contrainte matérielle à retenir

RTX 5090 = Blackwell, **compute capability 12.0 (SM 120)**, avec CUDA 13.1 sur la machine.
Toute brique passant par TensorRT doit être une build **CUDA 13.x** ET supporter SM 120.
Vérification rapide de la compatibilité d'une install TensorRT :

```bash
nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader
```

Matrice officielle : https://docs.nvidia.com/deeplearning/tensorrt/latest/getting-started/support-matrix.html
(sélectionner la version + la plateforme, puis lire « Supported Compute Capabilities »).

## Écarts constatés par rapport à la documentation officielle

1. **Le Sim2Sim n'est pas un simple env Python.** La doc le présente dans le Quick Start, mais
   il dépend de tout le chemin Deployment : TensorRT 10.13 + build C++ complet (`just build`).
   Le venv `.venv_sim` ne couvre que le simulateur (terminal 1), pas le contrôleur (terminal 2).
2. **`libmsgpack-dev` est obsolète sur Ubuntu 26.04** — voir tableau ci-dessus. La page
   Troubleshooting officielle ne mentionne pas ce cas.
3. **Ubuntu 26.04 n'est pas dans la liste supportée** (20.04/22.04/24.04). Le build passe
   néanmoins avec GCC 15.2 et CUDA 13.1 (la doc annonce CUDA 12.x).
4. **ROS2 absent** — détecté et désactivé automatiquement (`HAS_ROS2` non défini). Sans impact
   sur le Sim2Sim ; nécessaire seulement pour `ROS2InputHandler`.
5. **ONNX Runtime non trouvé** par `setup_env.sh` au premier passage — sans conséquence, le
   build utilise TensorRT.

## Prérequis à refaire après chaque redémarrage

```bash
sudo ip link set lo multicast on
```

Sans ça, le SDK Unitree ne peut pas initialiser DDS et le simulateur ne communique pas
avec le contrôleur.

## Ce qui reste à faire

- [x] `bash install_scripts/install_mujoco_sim.sh` → `.venv_sim` (Python 3.10.21, MuJoCo 3.14, torch 2.14)
- [ ] Lancer le Sim2Sim et valider que le G1 tient debout et suit la motion de référence
- [ ] Tutoriel *Motion Tracking and Kinematic Planner with Keyboard Controls*
- [ ] (plus tard) Isaac Lab 2.3.0 pour le fine-tuning du BFM
