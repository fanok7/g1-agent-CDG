# Mise à jour du contrat — serveur UnifoLM-WLA sur Thor, variante **Inspire**

À coller tel quel dans l'instance Claude qui écrit le **client WBC sur l'Orin NX**.
Ce document **remplace** les §4 et §6 de `THOR_DEPLOY_WLA.md` : le choix `UnifoLM_G1_Dex1`
était faux pour ce robot. Le reste de l'architecture ne change pas.

Rédigé le 2026-10-01 depuis la Thor, après tests. Tout ce qui est marqué **vérifié** a été mesuré
ou lu dans le code. Tout ce qui est marqué **à vérifier** ne l'a pas été.

---

## 1. Ce qui a changé et pourquoi

- Le G1 est équipé de **mains Inspire (6 DDL par main)**, pas de pinces Dex1.
- Dans le checkpoint, les données Inspire sont sous `unnorm_key = UnifoLM_WBT`, avec les
  slots `left_fig6d` / `right_fig6d` (6 doigts). Le serveur Dex1 d'origine ne lit ni ne prédit
  les doigts : son masque d'actions les exclut. Or ce masque est une **entrée** du modèle
  (conditionnement d'embodiment), il doit donc correspondre à l'entraînement.
- Nouveau serveur sur la Thor : `model_server/action_server_wbc_msgpack_unitree_inspire.py`
  (sous-classe du serveur d'origine, qui reste intact). Masque, état et normalisation suivent
  exactement le dataloader d'entraînement WBT (`configs/unitree.yaml` → `unitree_fullbody_base`).

**Validé hors ligne (vérifié).** J'ai rejoué l'épisode 0 de
`unitreerobotics/G1_WBT_Inspire_Put_Clothes_Into_Basket` à travers le serveur, avec les vraies
caméras et le vrai état, une requête toutes les 30 frames. Erreur moyenne sur le chunk, comparée
à une baseline « rester immobile » :

| | modèle | rester immobile |
|---|---|---|
| main gauche, position | 1,9 cm | 8,0 cm |
| main droite, position | 3,2 cm | 15,9 cm |
| main gauche, rotation | 3,4° | 14,8° |
| main droite, rotation | 6,4° | 21,5° |
| doigts main droite (0–1) | 0,033 | 0,107 |

Les 3 cycles prise/lâcher de la main droite sont prédits au bon moment.
**Défaut observé :** sur 3 chunks, le modèle prédit une fermeture partielle de la main **gauche**
(pouce latéral jusqu'à 0,6–0,8, auriculaire jusqu'à 0,3), alors que l'opérateur ne la bouge jamais.
**Limite :** cet épisode fait très probablement partie des données d'entraînement. Le test prouve
que la chaîne est correcte, pas que le modèle généralise.

---

## 2. Connexion

| | |
|---|---|
| URL | `ws://192.168.123.166:8600` |
| Transport | websocket, frames binaires msgpack + numpy |
| Sérialisation | copier `model_server/tools/msgpack_numpy.py` du dépôt `unifolm-wla` (60 lignes). Un ndarray est encodé `{b"__ndarray__": True, b"data": bytes, b"dtype": dtype.str, b"shape": shape}` |
| À la connexion | le serveur envoie **d'abord** un frame `metadata` (voir § 3) |
| Requête | `{"type": "get_action", "obs": {...}}` → réponse : dict d'actions |
| Autres types | `policy_reset` → `{}` ; `episode_end`, `obs_init`… → `None` ; `obs_chunk` → pas de réponse |
| Erreur serveur | le serveur envoie un frame **texte** (traceback), puis ferme la connexion. Le client doit tester `isinstance(raw, str)` |
| Pare-feu | port 8600 écoute sur `0.0.0.0` côté Thor. ufw **non vérifié** (pas de sudo). Teste `nc -zv 192.168.123.166 8600` |

**Le serveur n'est pas un service.** Il faut le lancer à la main sur la Thor, depuis `~/unifolm-wla`,
avec le venv activé :
```bash
python -m model_server.action_server_wbc_msgpack_unitree_inspire \
    --ckpt_path playground/Pretrained_models/UnifoLM-WLA-1.0-Base/checkpoints/model.safetensors \
    --dit_bf16 --host 0.0.0.0 --port 8600
```
Chargement : environ 80 s. `--unnorm_key UnifoLM_WBT` est appliqué par défaut.
**`--dit_bf16` fait passer la latence de 1,40 s à 0,46 s sans perte mesurable** (voir § 6).
`metadata["dit_bf16"]` dit si le serveur tourne avec. Il n'est pas activé par défaut, pour que
le comportement d'origine reste celui du dépôt.

---

## 3. `metadata` envoyé à la connexion (verbatim)

```
{'env': 'unifolm_wla_unitree_wbc_msgpack_inspire', 'ckpt_path': 'playground/Pretrained_models/UnifoLM-WLA-1.0-Base/checkpoints/model.safetensors', 'data_keys': ['observation.images.cam_left_high', 'observation.images.cam_left_wrist', 'observation.images.cam_right_wrist', 'observation.state.left_ee_6d', 'observation.state.right_ee_6d', 'observation.state.left_fig6d', 'observation.state.right_fig6d', 'observation.state.lower_body', 'observation.state.base_rot', 'action.left_ee_rpy', 'action.right_ee_rpy', 'action.left_fig6d', 'action.right_fig6d', 'action.lower_body', 'action.base_command', 'action.pivot', 'action.base_pose_rel'], 'action_chunk_size': 30, 'available_unnorm_keys': ['UnifoLM_G1_Dex1', 'UnifoLM_WBT'], 'default_unnorm_key': 'UnifoLM_WBT'}
```

---

## 4. Observations à envoyer (`obs`)

Chaque valeur peut avoir un axe `(T, …)` en tête : le serveur ne garde que la **dernière** frame.
Tous les états sont en `float32`.

| Clé | Forme | Contenu |
|---|---|---|
| `observation.images.cam_left_high` | (H,W,3) uint8 **BGR** | caméra tête, **œil gauche** de la stéréo (dataset : `head_stereo_left`, 480×640) |
| `observation.images.cam_left_wrist` | (H,W,3) uint8 BGR | caméra poignet gauche (dataset : `wrist_left`, 480×640) |
| `observation.images.cam_right_wrist` | (H,W,3) uint8 BGR | caméra poignet droit (dataset : `wrist_right`, 480×640) |
| `observation.state.left_ee_6d` | (9,) | pose main gauche : `xyz` (m) + rot6d |
| `observation.state.right_ee_6d` | (9,) | idem main droite |
| `observation.state.left_fig6d` | (6,) | doigts gauche, voir § 4.2 |
| `observation.state.right_fig6d` | (6,) | doigts droite |
| `observation.state.lower_body` | (15,) | rad : jambe G (hip_pitch, hip_roll, hip_yaw, knee, ankle_pitch, ankle_roll), jambe D (même ordre), taille (**yaw, roll, pitch**) |
| `observation.state.base_rot` | (6,) | IMU du bassin : gravité projetée `gx, gy, gz` (vecteur unitaire, ≈ `[0, 0, -1]` debout) + vitesse angulaire `wx, wy, wz` (rad/s). Facultatif pour le serveur, mais **présent dans toutes les données d'entraînement : à envoyer** |
| `instruction` | str | consigne en anglais. Reprendre la formulation des datasets (§ 7) |

Les images sont redimensionnées côté serveur en 320×448. Inutile de les réduire côté client,
sauf pour la bande passante.

### 4.1 Pose des mains : le point le plus risqué

- **Repère.** C'est le champ `observation.state.<side>_ee_pose_gripper_base` des datasets Unitree :
  pose de l'effecteur dans le repère **base du robot**. Plages observées sur cet épisode :
  - main gauche : x ∈ [−0,12 ; 0,55], y ∈ [−0,23 ; 0,55], z ∈ [−0,30 ; 0,35] ;
  - main droite : x ∈ [−0,03 ; 0,71], y ∈ [−0,65 ; 0,27], z ∈ [−0,27 ; 0,71].

  Le repère exact (pelvis ou torse) et le point exact de l'effecteur sont **à vérifier**.
- **Conversion de la rotation (vérifié dans le code).** Le dataset la stocke en `rpy`. On la
  convertit en matrice avec `scipy.spatial.transform.Rotation.from_euler("xyz", rpy)` (minuscules =
  extrinsèque), puis en rot6d = **deux premières colonnes, colonne par colonne**,
  `[R00, R10, R20, R01, R11, R21]`. Une rot6d nulle ou dégénérée **fait planter la requête**
  (scipy refuse une matrice de déterminant ≤ 0).
- **Comment vérifier ta cinématique directe (FK).** Le dataset contient à la fois les angles
  (`observation.state.left_arm` / `right_arm`, 7 articulations, plus la taille dans
  `observation.state.lower_body[12:15]`) et la pose `..._ee_pose_gripper_base`. Ta FK doit
  reproduire cette pose à partir de ces angles. **Fais ce test avant toute autre chose** : une
  erreur de repère ici fausse toutes les actions sans aucun message d'erreur.

### 4.2 Doigts (`fig6d`) — corrigé le 2026-10-01 après le retour de l'Orin

- **Ordre `fig6d` :** `[thumb_oc, thumb_lat, index, middle, ring, little]`, valeurs dans **[0, 1]**.
  Les noms sont **justes**.
- **Sens : valeur haute = main ouverte, valeur basse = main fermée.**
  - Données : main ouverte ≈ `[0,8, 0, 0,9, 0,9, 0,9, 0,9]`, main refermée sur du linge ≈
    `[0,1, 0, 0,3, 0,3, 0,34, 0,21]`.
  - **Confirmé visuellement** sur la caméra poignet droite du dataset (frame 60 : doigts tendus ;
    frame 200 : doigts repliés sur le linge).
  - Cohérent avec le registre ANGLE du DDS Unitree (1 = ouvert).
  - ⚠ Le README du dataset écrit « 0.0 – 1.0, open → close » : **il se trompe sur le sens**,
    ne pas le suivre.
- **Pourquoi l'ancienne correspondance semblait contredire l'ordre natif.** Le `hand_state` du
  dataset n'est **pas** dans l'ordre natif du DDS : l'enregistreur WBT réordonne les 4 doigts
  longs. Le README du dataset le documente : par main, `1 Index, 2 Middle, 3 Ring, 4 Little,
  5 Thumb open/close, 6 Thumb lateral`, avec la main gauche en premier. L'ordre natif DDS est
  `[pinky, ring, middle, index, thumb_bend, thumb_rot]`.
- **Correspondance à appliquer côté client, par main**, avec `n` = registre ANGLE / 1000 dans
  l'ordre natif `[pinky, ring, middle, index, bend, rot]` :
  ```
  lecture  → modèle   : fig6d = n[[4, 5, 3, 2, 1, 0]]     # [bend, rot, index, middle, ring, pinky]
  modèle   → commande : n     = fig6d[[5, 4, 3, 2, 0, 1]]  # inverse exacte (vérifiée)
  ```
- **Mains :** dans le DDS natif, **droite = indices 0–5, gauche = 6–11** (xr_teleoperate
  `robot_hand_inspire.py`). L'ordre « gauche d'abord » du dataset ne concerne que le fichier
  enregistré. Côté client, chaque main est traitée séparément.
- **`thumb_lat` (rotation du pouce) :** il vaut ≈ 0 dans **tout** l'état et **toutes** les commandes
  du dataset. La téléopération maintenait la rotation du pouce à 0. Chez nous, 0,65–0,80 au repos
  sort de la distribution d'entraînement, et le modèle commandera ≈ 0. **Règle :** vérifier
  physiquement la posture `rot = 0`, puis amener le pouce à `rot = 0` avant la première requête
  au serveur.
- **Test physique à faire (main immobile sinon, rien dans la main) :** commander l'index natif
  (canal 3) à 0,2. Seul l'index doit se fermer, et sur la main attendue : ce test tranche aussi la
  correspondance IP ↔ main.

---

## 5. Actions renvoyées

Chaque clé a la forme `(1, 30, D)`, en `float32`, **dénormalisées et absolues**. Elles couvrent les
30 prochains pas à 30 Hz, soit 1 s. Le client lit `a[key][0, :chunk]`.

| Clé | D | Contenu |
|---|---|---|
| `action.left_ee_rpy` / `right_ee_rpy` | 6 | pose cible absolue `xyz + rpy` (euler `"xyz"`), dans le **même repère** que l'état envoyé |
| `action.left_fig6d` / `right_fig6d` | 6 | doigts cibles, même ordre et même sens qu'au § 4.2 (haut = ouvert) |
| `action.lower_body` | 15 | jambes G et D + taille (yaw, roll, pitch), en rad |
| `action.base_command` | 4 | `vx, vy, vw, height` — **lire l'avertissement ci-dessous** |
| `action.pivot` | 7 | `vx, vy, vw, yaw, pitch, roll, height` (repris du serveur Dex1) |
| `action.base_pose_rel` | 6 | pose de la base relative à l'instant présent : `xyz + rotvec` |

**Avertissement sur `base_command` (vérifié sur le dataset).** Dans les données, la 3e composante
(`vw`, nommée `angle_z` dans le dataset) est **strictement égale à la vitesse de lacet mesurée
par l'IMU** (`state_base_rot[5]`, écart max 5e-7). Ce n'est pas une consigne. Elle monte jusqu'à
**30 rad/s** sur certains pics. Le modèle a appris à reproduire ça. **Ne jamais envoyer `vw`
tel quel au contrôleur de locomotion.** Valeurs usuelles des autres composantes (percentiles
1 / 50 / 99) :
- `vx` : −0,27 / 0,00 / 0,22 m/s ;
- `vy` : −0,20 / 0,00 / 0,23 m/s ;
- `height` : 0,67 / 0,78 / 0,79 m.

---

## 6. Temps réel — résolu le 2026-10-02, lancer le serveur avec `--dit_bf16`

**La latence est passée de 1,40 s à 0,46 s par requête.** Elle tient donc dans la seconde que dure
un chunk : la Thor suit, le serveur reste sur la Thor.

**Ce qui bloquait.** Profilage sur la Thor (`thor_profile_latency.py`) :

| étage | avant | après |
|---|---|---|
| préparation des entrées (CPU) | 13 ms | 13 ms |
| VLM Qwen3-VL-4B | 234 ms | 234 ms |
| **DiT (1,37 Md param., 4 pas de diffusion)** | **1129 ms** | **213 ms** |
| **total `predict_action`** | **1390 ms** | **≈ 460 ms** |

Contrairement à ce qu'on pouvait croire, le VLM n'était pas en cause : **81 % du temps partait dans
le DiT**, parce que `QwenMMDiT.predict_action` l'exécute sous `autocast(float32)` alors que le
serveur a converti tous les poids en bf16. Le drapeau `--dit_bf16` entre dans un autocast bf16
à l'intérieur de cet appel (contexte le plus interne, donc prioritaire), sans modifier le dépôt.

**Vérifié, pas seulement chronométré.** L'épisode 0 rejoué entièrement en bf16 donne les mêmes
erreurs qu'en fp32, à la variation d'échantillonnage près (la diffusion est stochastique) :

| | fp32 | bf16 |
|---|---|---|
| main gauche, position | 1,92 cm | 1,81 cm |
| main droite, position | 3,23 cm | 2,96 cm |
| main gauche, rotation | 3,42° | 3,29° |
| main droite, rotation | 6,44° | 5,60° |
| doigts main droite | 0,033 | 0,037 |

Écart direct sur les actions normalisées, à bruit de diffusion identique : au pire 2 % de
l'amplitude du geste, en moyenne 0,002.

**Marges encore disponibles si besoin** (non faites) : `--dit_steps 2` diviserait encore le DiT par
deux (213 → 107 ms), au prix d'une qualité à revalider ; puis torch.compile.

- **Côté client :**
  - un chunk couvre 1,0 s et arrive en ≈ 0,46 s : il reste de la marge, mais **envoyer la requête
    suivante dès réception d'un chunk**, sans attendre la fin de son exécution ;
  - ignorer les premiers pas du nouveau chunk, déjà périmés : environ **14 pas** à 30 Hz pour
    0,46 s de latence (contre 42 avant) ;
  - prévoir malgré tout le cas d'une réponse lente (première requête, charge).

  **Mesure la latence vue depuis l'Orin** : elle ajoute le réseau et la capture des caméras.

---

## 7. Consignes vues à l'entraînement (Inspire / WBT)

Collection HF `unitreerobotics/G1_WBT_Inspire_*`, environ 30 tâches, notamment :
Put_Clothes_Into_Basket, Pick_Up_Clothes, Pick_Up_Drinks, Put_Drinks_Into_Fridge, Sort_Fruit,
Pick_Up_Fruit, Put_Vegetables_Into_Basket, Put_Plate_Into_Dishwasher, Hang_Up_Clothes,
Pick_Up_Multiple_Cushions, Push_Wheelchair, Supermarket_Shelf_Organizing…

Formulation exacte vérifiée pour l'épisode testé :
`"Pick up the clothes from the bed and place them into the basket"`.
La consigne exacte de chaque dataset est dans son `meta/tasks.parquet`.

---

## 8. Ce que je te demande côté Orin

1. **Test de cinématique directe (§ 4.1)** contre le dataset. Critère : erreur de position de
   quelques mm. Rapporte le repère retenu et l'erreur obtenue.
2. **Correspondance des mains Inspire (§ 4.2)** avec ton driver : ordre des canaux, échelle, sens.
3. **Client en mode ombre uniquement :** vraies caméras, vrai état, vraie IMU → serveur → actions
   **journalisées, rien n'est envoyé aux moteurs**. Journalise par requête : l'horodatage d'envoi
   et de réception, l'état envoyé, le chunk reçu.
4. **Pour les futurs essais réels, règles de sécurité que je recommande :**
   - bras et doigts **uniquement**, à vitesse réduite ;
   - **ignorer** `action.lower_body`, `action.base_command`, `action.pivot` et
     `action.base_pose_rel` : jambes et équilibre restent au WBC de l'Orin, ce qui prolonge le
     choix « `_no_agile` » déjà fait pour GR00T ;
   - saturer les cibles de main à l'intérieur des plages du § 4.1 ;
   - limiter le pas de position entre deux cibles ;
   - prévoir un garde-fou contre les fermetures parasites de la main gauche (§ 1).

**Compte rendu attendu :** résultats des points 1 et 2, et un extrait du journal du mode ombre
(10 requêtes) avec la latence aller-retour vue depuis l'Orin.
