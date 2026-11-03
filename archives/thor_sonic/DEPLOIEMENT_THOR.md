# Déploiement embarqué sur la Thor — SONIC + gestes d'accueil

Contexte autonome pour installer et lancer, **directement sur la Jetson Thor du robot**,
le contrôleur SONIC avec la bibliothèque de gestes d'accueil, et pour que l'agent d'accueil
déclenche un geste par son nom. Pas de serveur, pas de PC externe : tout tourne sur la Thor.

État au 2026-10-28 : tout ce qui suit est **validé en simulation MuJoCo sur PC**
(RTX, TensorRT 10.13, CUDA 13.1). **Rien n'a encore tourné sur la Thor ni sur le vrai robot.**

---

## 1. Architecture

```
            Jetson Thor (embarquée)
 ┌──────────────────────────────────────────────────────────┐
 │ Agent d'accueil  ── from geste import jouer              │
 │        │            jouer("warm_welcome", attendre=True) │
 │        ▼                                                 │
 │  /tmp/sonic_cmd (tube)      /tmp/sonic.log (journal)     │
 │        │                           ▲                     │
 │        ▼                           │                     │
 │  g1_deploy_onnx_ref  (SONIC : encodeur + décodeur + planner, TensorRT)
 └────────┬─────────────────────────────────────────────────┘
          │ Ethernet 192.168.123.x — DDS rt/lowcmd / rt/lowstate (bas niveau, 29 DDL)
          ▼
      Robot G1 en MODE DEBUG (contrôleur usine éteint)
```

- **SONIC pilote tout le corps.** Le contrôleur de marche Unitree est éteint (mode debug),
  donc son estimateur buggé n'intervient plus. `arm_sdk` n'est **pas** utilisé dans cette voie.
- **Les gestes sont des mouvements de référence** que SONIC suit corps entier, équilibre compris.
- **La marche** = mode Planner de SONIC (touche `Entrée` ; manette Unitree en réel).
- **L'agent** appelle `geste.jouer(nom)` en local. Le script envoie au contrôleur les touches
  N / P / T par le tube, en lisant dans le journal l'ordre des gestes et le geste courant.

## 2. Ce qu'il faut sur la Thor

| Élément | Source (PC de dev) | Taille |
|---|---|---|
| Dépôt SONIC | `git clone https://github.com/NVlabs/GR00T-WholeBodyControl.git` + `git lfs pull` (PC : commit `b042411`) | — |
| Poids SONIC v1.1 | `gear_sonic_deploy/policy/sonic_v1_1/` : **seulement** `model_encoder.onnx`, `model_decoder.onnx`, `observation_config.yaml` | ~380 Mo |
| Planner | `gear_sonic_deploy/planner/target_vel/V2/planner_sonic.onnx` | — |
| Gestes d'accueil | `gear_sonic_deploy/reference/accueil/` (15 dossiers) | 31 Mo |
| Scripts | `g1-stack/scripts/` : `sonic_start.sh`, `sonic_cmd.py`, `geste.py`, `env.sh` | — |

⚠️ **Ne pas copier les `.trt`** (`*_model_*.trt`, `planner_planner_sonic.trt`) : ce sont des
moteurs TensorRT compilés pour la carte du PC. Sur la Thor, le binaire les régénère depuis les
`.onnx` au premier lancement (plusieurs minutes). Sans ça, avertissement
« engine plan file across different models of devices » et risque d'inférence fausse.

Copie depuis le PC (remplacer `<thor>` par l'utilisateur@IP de la Thor) :

```bash
D=~/projects/GR00T-WholeBodyControl/gear_sonic_deploy
rsync -av --exclude='*.trt' $D/policy/sonic_v1_1 $D/planner/target_vel <thor>:GR00T-WholeBodyControl/gear_sonic_deploy/staging/
rsync -av $D/reference/accueil <thor>:GR00T-WholeBodyControl/gear_sonic_deploy/reference/
rsync -av ~/projects/g1-stack/scripts/{sonic_start.sh,sonic_cmd.py,geste.py,env.sh} <thor>:g1-stack/scripts/
```

Puis, sur la Thor, placer `staging/sonic_v1_1` dans `policy/` et `staging/target_vel` dans `planner/`.

## 3. Installation sur la Thor

### 3.1 Point bloquant à vérifier en premier : TensorRT

La doc NVIDIA n'officialise que **x86 → TensorRT 10.13** et **Orin (JetPack 6) → TensorRT 10.7**,
et prévient : *« une autre version produit des inférences incorrectes — le planner sort un
mouvement faux, ce qui peut rendre le robot dangereux »*. **La Thor n'est pas dans le tableau.**

- La Thor tourne sous JetPack 7 (CUDA 13). Vérifier la version de TensorRT :
  `dpkg -l | grep -i tensorrt` ou `python3 -c "import tensorrt; print(tensorrt.__version__)"`.
- Si c'est une 10.13.x : même branche que le PC où tout est validé → bon signe, mais **à valider
  en simulation sur la Thor** (§5) avant tout essai réel.
- Sinon : ne pas aller sur le robot. Valider d'abord en sim et comparer au PC (le robot doit
  suivre les gestes sans partir en vrille, voir piège de la touche `9`).

### 3.2 Build

```bash
cd ~/GR00T-WholeBodyControl/gear_sonic_deploy
./scripts/install_deps.sh
export TensorRT_ROOT=<dossier TensorRT de la Thor>   # cf. 3.1
source scripts/setup_env.sh        # sur Jetson : règle aussi jetson_clocks
just build                         # → target/release/g1_deploy_onnx_ref
```

### 3.3 Adapter `env.sh`

`g1-stack/scripts/env.sh` est écrit pour le PC (`TensorRT_ROOT=$HOME/TensorRT-10.13.3.9`,
chemins `~/projects/...`). Sur la Thor, mettre à jour : `TensorRT_ROOT`, `SONIC_REPO`
(chemin du dépôt), `G1_STACK`. Les lignes ffmpeg/GR00T ne servent pas ici.

## 4. Brancher l'agent d'accueil

L'agent tourne sur la même Thor : il importe `geste.py` (Python 3 standard, aucune dépendance).

```python
import sys; sys.path.insert(0, "/chemin/vers/g1-stack/scripts")
from geste import jouer, gestes, nom_court

jouer("warm_welcome", attendre=True)   # rend la main à la fin du geste
[nom_court(c) for c in gestes()]       # gestes disponibles
```

- `attendre=True` : indispensable pour enchaîner ; un nouveau geste **coupe** celui en cours.
- Erreurs : `ValueError` (nom inconnu, avec la liste), `RuntimeError` (contrôleur absent ou
  pas sur le bon geste), `TimeoutError` (geste non terminé après 60 s).
- Le script dépend du format du journal (`✓ Loaded …`, `Reference motion name: …`,
  `Motion index: k : … completed.`). Si une mise à jour du dépôt NVIDIA change ces messages,
  adapter les regex de `geste.py`.
- **Un seul pilote à la fois** : pendant que l'agent joue un geste, ne rien envoyer au clavier.

### Gestes disponibles (ordre du contrôleur)

| Nom | Usage suggéré |
|---|---|
| `warm_welcome`, `casual_greeting` | arrivée d'un visiteur |
| `wave` | salut **en marchant** (~10 s, le robot se déplace) |
| `beckon`, `come_here` | inviter à approcher |
| `present_left` | indiquer une direction / présenter |
| `neutral_item_give` | tendre un objet (badge, flyer) |
| `thinking` (~27 s), `nod_in_thought` | pendant que l'agent réfléchit |
| `don_t_know_no_hands` | l'agent n'a pas la réponse |
| `m_kissing_hands_thanks_stadium` | remercier |
| `clap_enthusiastic`, `crowd_cheer`, `show_bicep` | célébrer, ludique |
| `bye_bye_salute` | départ |

Retirés volontairement (risque de chute) : révérence (`polite_bow`), high five (les 8 clips
enchaînent un trot). Ils sont dans `g1-stack/datasets/accueil/retires/` sur le PC.

## 5. Ordre de mise en service (ne sauter aucune étape)

**Étape A — Sim sur la Thor** (robot éteint ou débranché). Valide TensorRT et le build.
```bash
# Terminal 1 (si MuJoCo est installé sur la Thor ; sinon passer à B avec le robot suspendu)
cd ~/GR00T-WholeBodyControl && .venv_sim/bin/python gear_sonic/scripts/run_sim_loop.py
# Terminal 2
bash ~/g1-stack/scripts/sonic_start.sh reference/accueil/      # interface lo, CRC désactivé
# Terminal 3
python3 ~/g1-stack/scripts/sonic_cmd.py     # start  (= touche ])
```
Puis **cliquer dans la fenêtre MuJoCo et taper `9`** (pose le robot au sol — obligatoire et
silencieux : sans elle le robot part en vrille). Puis :
`python3 ~/g1-stack/scripts/geste.py warm_welcome --attendre`.
Attendu dans `/tmp/sonic.log` : `Found 15 motion folders`, `Init Done`, puis
`Motion index: 14 : warm_welcome_R_001__A428 completed.`, et aucun `Robot has fallen`.

**Étape B — Vrai robot suspendu au portique**, pieds dans le vide, arrêt d'urgence en main.
1. Robot en **mode debug** (contrôleur usine désactivé) — procédure Unitree de la télécommande.
2. Thor sur le réseau robot `192.168.123.x` ; repérer l'interface : `ip -br a | grep 192.168.123`.
3. Lancer avec l'interface réelle — **la vérification CRC reste alors active** :
   ```bash
   SONIC_IFACE=<interface> bash ~/g1-stack/scripts/sonic_start.sh reference/accueil/
   ```
4. `start`, puis un seul geste court (`casual_greeting`). Vérifier : mouvement propre, pas de
   saccade, pas de moteur qui chauffe (`F` dans la console = température moteurs).

**Étape C — Au sol, surveillé**, zone dégagée, E-stop en main : d'abord rester debout, puis
quelques gestes **sur place**, puis la marche (Planner) lente. `wave` (qui marche) en dernier.

**Étape D — Agent** : seulement après C, laisser l'agent appeler `jouer()`.

## 6. Pièges et points ouverts

- **`--motor-kp-scale 4,10=1.5` / `--motor-kd-scale 4,10=1.5`** dans `sonic_start.sh` :
  repris de la config sim du PC, origine non documentée. **Vérifier avant l'étape B** si c'est
  voulu pour le réel (`deploy.sh` officiel ne les met pas par défaut) ; en cas de doute, les
  retirer pour le premier essai réel.
- **Arrêt d'urgence logiciel** : touche `O` (console `stop`) ; manette Unitree : `Select`.
  Garder l'arrêt matériel du robot à portée — c'est lui qui compte.
- **État collant du contrôleur** : après un changement de mode ou un arrêt brutal, relancer
  `sonic_start.sh` à froid.
- **Genou droit** : offset physique de ~0,28° signalé, non vérifié ; SONIC devrait le voir comme
  une petite erreur de suivi.
- **Ressources Thor** : SONIC (planner ~12 ms sur Orin, moins sur Thor) partage le GPU avec la
  vision et l'agent. Surveiller dans le journal les lignes `Loop timing` : la boucle doit rester
  à 50 Hz.
- **Mains** : les gestes ne contiennent que les 29 DDL du corps ; les doigts ne bougent pas.

## 7. Ajouter un geste

Sur le PC (seul `.venv_inference` a pandas + wandb) :
```bash
cd ~/projects/GR00T-WholeBodyControl
cp <clip BONES-SEED>.csv ~/projects/g1-stack/datasets/accueil/csv/
.venv_inference/bin/python gear_sonic/data_process/convert_soma_csv_to_motion_lib.py \
    --input ~/projects/g1-stack/datasets/accueil/csv --output ~/projects/g1-stack/datasets/accueil/accueil_motionlib.pkl --fps 50
.venv_inference/bin/python gear_sonic/data_process/export_motion_to_deploy.py \
    ~/projects/g1-stack/datasets/accueil/accueil_motionlib.pkl --output-dir gear_sonic_deploy/reference/accueil --zero-root-xy
```
Vérifier en sim que le robot ne tombe pas, puis recopier `reference/accueil/` sur la Thor et
relancer `sonic_start.sh`. Clips bruts : `~/projects/g1-stack/datasets/motions/full/g1/csv/`
(142 220 mouvements BONES-SEED déjà retargetés G1).
