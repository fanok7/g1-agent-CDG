# Contexte de travail — SONIC sur la Thor (session directe)

Pour travailler **en SSH directement sur la Jetson Thor** (`unitree@192.168.123.166`,
alias `thor`). Objectif de la session : finir l'install et **valider SONIC en simulation**
sur la Thor. **Ne pas toucher au vrai robot** tant que la sim n'est pas OK.

Réfs complètes : `~/g1-stack/docs/DEPLOIEMENT_THOR.md` (sur la Thor), `sonic_handoff.md`
(sur l'Orin, pièges du setup PC).

---

## État vérifié au 2026-10-28

| Élément | État |
|---|---|
| Archi Thor | aarch64, L4T R38.2 (JetPack 7), **CUDA 13.0**, GPU **SM 110** |
| **TensorRT** | ✅ **installé** `10.13.3.9-1+cuda13.0` (dépôt Jetson officiel) — `trtexec` dans `/usr/src/tensorrt/bin/` |
| Repo SONIC | ✅ `~/GR00T-WholeBodyControl` (5,6 Go, commit `b042411`, LFS pull fait) |
| Poids SONIC v1.1 | ✅ `gear_sonic_deploy/policy/sonic_v1_1/` (encoder+decoder+observation_config) |
| Planner | ✅ `gear_sonic_deploy/planner/target_vel/V2/planner_sonic.onnx` |
| Gestes | ✅ `gear_sonic_deploy/reference/accueil/` (15 gestes) |
| Fichiers `.trt` | ❌ absents **volontairement** → régénérés au 1er lancement sur la Thor |
| Scripts | ✅ `~/g1-stack/scripts/` : `sonic_start.sh sonic_cmd.py geste.py env.sh` |
| Outils de build (`just`, `cargo`) | ❌ manquants → installés par `install_deps.sh` |
| Binaire `g1_deploy_onnx_ref` | ❌ pas encore buildé |

---

## À FAIRE, dans l'ordre

### 0. Corriger git (empêche plein de choses)
`~/.gitconfig` est un **dossier vide appartenant à root** (accident Docker du 9 sept).
Vide → suppression sans risque (`rmdir` refuse si non-vide) :
```bash
sudo rmdir ~/.gitconfig
git config --global user.name "unitree" && git config --global user.email "unitree@thor"
```

### 1. Internet sur la Thor
`install_deps.sh` télécharge des paquets → la Thor doit avoir une route internet.
Vérifier : `ping -c1 github.com`. Sinon, router via l'interface qui a le net
(cf. config réseau du robot/Orin).

### 2. Dépendances de build (demande sudo)
```bash
cd ~/GR00T-WholeBodyControl/gear_sonic_deploy
timeout 1800 ./scripts/install_deps.sh      # vraie durée, PAS <Xmin> (bash avale < >)
```
Pièges connus (vus sur PC Ubuntu récent) :
- `libmsgpack-dev` est un paquet vide → si erreur `msgpack.hpp` manquant :
  `sudo apt-get install -y libmsgpack-cxx-dev`
- `.justfile` est caché → lancer `just` **depuis `gear_sonic_deploy/`**.

### 3. Adapter `env.sh` pour la Thor
`~/g1-stack/scripts/env.sh` est écrit pour le PC (`TensorRT_ROOT=$HOME/TensorRT-10.13.3.9`,
chemins `~/projects/...`). Sur la Thor, TensorRT est en **chemins système** (`/usr/...`),
pas dans un dossier maison. À corriger :
- `TensorRT_ROOT` : pointer sur l'install système. En.-têtes dans `/usr/include/aarch64-linux-gnu/`,
  libs dans `/usr/lib/aarch64-linux-gnu/`. Si le CMake exige un `TensorRT_ROOT` type dossier,
  créer un lien ou pointer `/usr` (vérifier que `NvInfer.h` et `libnvinfer.so` sont trouvés).
- `SONIC_REPO=~/GR00T-WholeBodyControl`, `G1_STACK=~/g1-stack`.
- Lignes ffmpeg/GR00T : inutiles ici.

### 4. Build
```bash
cd ~/GR00T-WholeBodyControl/gear_sonic_deploy
source scripts/setup_env.sh        # sur Jetson : règle aussi jetson_clocks
export TensorRT_ROOT=<voir étape 3>
timeout 3600 just build            # → target/release/g1_deploy_onnx_ref
```
Si `Fail to find TensorRT` : `setup_env.sh` affiche « configured » même sans trouver →
re-vérifier `TensorRT_ROOT` et la présence de `NvInfer.h`.

### 5. Validation SIM (étape A) — AUCUN robot
Prérequis à CHAQUE reboot : `sudo ip link set lo multicast on` (sinon DDS ne s'init pas).
```bash
# Terminal 1 — simulateur (si .venv_sim installé sur la Thor ; sinon l'installer d'abord)
cd ~/GR00T-WholeBodyControl && .venv_sim/bin/python gear_sonic/scripts/run_sim_loop.py
# Terminal 2 — contrôleur SONIC
bash ~/g1-stack/scripts/sonic_start.sh reference/accueil/
# Terminal 3 — commande
python3 ~/g1-stack/scripts/sonic_cmd.py        # 'start' = touche ]
```
**OBLIGATOIRE ET SILENCIEUX** : cliquer dans la fenêtre 3D MuJoCo puis taper **`9`**
(pose le robot au sol). Sans ça, pas d'erreur mais le robot part en vrille.
Le `9` ne passe PAS par tmux → fenêtre graphique, focus requis, opérateur humain.

Puis jouer un geste :
```bash
python3 ~/g1-stack/scripts/geste.py warm_welcome --attendre
```
Attendu dans `/tmp/sonic.log` : `Found 15 motion folders`, `Init Done`,
`Motion index: … warm_welcome…_completed.`, et **aucun** `Robot has fallen`.
Le 1er lancement régénère les `.trt` depuis les `.onnx` (plusieurs minutes, normal).

---

## Garde-fous

- **Ne jamais aller sur le robot avant que l'étape 5 (sim) soit propre sur la Thor.**
  TensorRT est la bonne version, mais Thor = SM 110 hors tableau officiel → valider en sim.
- Ordre de mise en service réel ensuite (voir DEPLOIEMENT_THOR.md §5) : **B** robot
  **suspendu** au portique, E-stop en main → **C** au sol surveillé → **D** agent.
- `--motor-kp-scale/kd-scale 4,10=1.5` dans `sonic_start.sh` : origine non documentée,
  **à vérifier/retirer avant le 1er essai réel**.
- Ressources : SONIC partage le GPU de la Thor avec la vision (face_id/RealSense) et l'agent.
  Surveiller `Loop timing` dans le journal : la boucle doit tenir **50 Hz**.
- Les **doigts ne bougent pas** (gestes = 29 DDL du corps seulement).
- Genou droit : offset ~0,28° non corrigé → SONIC le voit comme une petite erreur de suivi.

## Commandes utiles

```bash
ssh thor                                   # depuis l'Orin
tmux capture-pane -S -3000 | grep STREAMING   # retrouver un message noyé
nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader   # (Tegra: peut être partiel)
```
