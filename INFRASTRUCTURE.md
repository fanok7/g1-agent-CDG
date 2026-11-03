# Infrastructure — état au 2026-09-25 et CLI de lancement

Ce document résume ce qui a changé dans l'infrastructure de l'agent depuis la dernière
mise à jour de [CLAUDE.md](CLAUDE.md) (travail en cours, **pas encore commité**, voir
`git status`) et rassemble **tous** les flags CLI de `main.py`, y compris le nouveau
`--engine`. Détail complet du chantier en cours : dossier [`changements/`](changements/)
(notes chronologiques). Ce fichier n'a pas vocation à durer — à fusionner dans
`CLAUDE.md`/`README.md` une fois le pipeline local validé vocalement (voir § 5).

## 1. Vue d'ensemble — deux Jetson

Le robot embarque **deux** machines en réseau (`eth0`, 192.168.123.x), pas une seule :

```
┌─ Jetson Orin NX (192.168.123.164) ───────────┐   ┌─ Jetson Thor (192.168.123.166) ──────────┐
│ main.py — agent, hardware (bras/audio/DDS),   │   │ GPU CUDA (Blackwell)                      │
│ tablette (8000), dashboard ESP32 (8888)       │   │                                            │
│                                                │   │ • Vision (déjà en service, voir THOR.md) :│
│  --engine openai (défaut, en service) ───────►│───┼─▶ OpenAI Realtime (cloud, hors schéma)    │
│  --engine local  (squelette, § 2.2)      SSH  │   │   g1-face-id.service, g1-realsense-push   │
│      └─ thor_remote.ensure_thor_vision_running()──┼─▶ (systemd --user, redémarrés à chaque    │
│         (vérifie/relance la vision à chaque   │   │    lancement de main.py)                  │
│         lancement, même en --engine openai)   │   │                                            │
│                                                │   │ • Conversation locale (nouveau, § 2.2) :  │
│                                                │◀──┼── conteneur Docker vLLM (Qwen3-8B),       │
│                                                │   │    /v1/chat/completions, port 8000        │
└────────────────────────────────────────────────┘   └────────────────────────────────────────────┘
```

La Thor portait déjà la vision déportée (InsightFace + YOLO — voir [THOR.md](THOR.md),
déjà documenté, rien de nouveau côté architecture vision elle-même). Ce qui est
**nouveau** : `robot/thor_remote.py` fait maintenant vérifier/relancer ces deux services
par SSH **à chaque lancement de `main.py`** (avant, ils persistaient seuls via systemd
sans vérification active côté agent) — et la Thor héberge maintenant *aussi* un serveur
d'inférence LLM (vLLM) pour le nouveau moteur conversationnel local.

## 2. Le moteur conversationnel — deux chemins (`--engine`)

| | `--engine openai` (défaut, **en service**) | `--engine local` (nouveau, **squelette expérimental**) |
|---|---|---|
| Où tourne le LLM | Cloud OpenAI (Realtime API, streaming) | Jetson Thor, conteneur vLLM (`Qwen/Qwen3-8B`) |
| ASR / VAD | Géré par l'API OpenAI (intégré) | `faster-whisper` + Silero VAD, en local sur l'Orin NX |
| TTS | Généré par l'API OpenAI (audio streaming) | Piper (binaire), voix `fr_FR-siwis-medium`, en local |
| Réseau requis | Oui, en continu (sinon pas d'agent du tout) | Non pour la conversation — juste le réseau robot (eth0) vers la Thor |
| Tools exposés | 52 (mode cdg/iinterim) / 3 (daneel) | **3 seulement** : `executer_geste`, `relacher_bras`, `date_heure_actuelle` |
| Sous-systèmes démarrés | Tout : tablette, vision, Spotify, hands idle, Thor vision | **Rien d'autre** : pas de tablette, pas de vision, pas de Spotify, pas de hands idle |
| Modes supportés | cdg, iinterim, daneel | Techniquement tous (`--mode` change juste le prompt), mais seul `iinterim` a été testé (arbre de décision visiteur) |
| Statut | Stable, en production | Testé texte (`scripts/test_vllm_pipeline.py`) + un run vocal complet bout-en-bout validé par l'utilisateur ; bugs connus ouverts (§ 5) |

### 2.1 Chemin `openai` — ce qui a changé récemment (hors nouveau moteur)

- **Un seul modèle Realtime pour tous les modes** depuis le 2026-09-24 :
  `gpt-realtime-2.1-mini` (avant : ce modèle réservé à `daneel`, les autres modes sur
  l'ancien `gpt-realtime-mini-2025-12-15`). Meilleur suivi d'instructions, validé
  d'abord côté interprète puis étendu partout (`config.py`).
- **Micro** : `noise_reduction: far_field` ajouté côté `session.update`
  (`agent/session.py`) — adapté au nouveau micro Anker PowerConf (conférence,
  omnidirectionnel) qui a remplacé le Cubilux (voir mémoire `project_powerconf_mic`).
- **Transcription forcée en français en mode `iinterim`** (`language: 'fr'`) — les
  visiteurs de l'agence sont quasi exclusivement francophones ; CDG/Daneel gardent
  l'auto-détection.
- `agent/session.py` expose maintenant `build_instructions()` (contexte temporel +
  `SYSTEM_PROMPT`) en fonction séparée, réutilisée telle quelle par le pipeline local
  (§ 2.2) — une seule source pour cette logique, plus de copie entre les deux chemins.

### 2.2 Chemin `local` — nouveau pipeline 100% embarqué

```
Micro → Silero VAD (torch.hub) → faster-whisper (transcription)
      → vLLM / Qwen3-8B sur la Thor (texte + tool-calling, gestes inclus)
      → validateur texte (didascalies/jargon) → Piper (TTS) → haut-parleur
```

Code : `agent_local/` (`asr.py`, `vllm_client.py`, `validator.py`, `tts.py`,
`orchestrator.py`, `schema_adapter.py`) + branche dédiée dans `main.py`
(`run_local()` au lieu de `run()`). Pourquoi ce chantier, en une phrase : remplacer
« louer un cerveau dans le cloud, minute par minute » par « utiliser le GPU déjà à
bord (Thor) », pour l'indépendance réseau, la confidentialité (rien ne sort du réseau
robot) et le coût récurrent — détail complet dans
[`changements/2026-09-23-pipeline-local-pourquoi.md`](changements/2026-09-23-pipeline-local-pourquoi.md).

**Déploiement Thor** — conteneur vLLM officiel NVIDIA, lancé manuellement (pas encore
un service systemd) :
```bash
docker run -d --name vllm --restart unless-stopped --runtime nvidia --ipc=host \
  -p 192.168.123.166:8000:8000 -v vllm_data:/data \
  ghcr.io/nvidia-ai-iot/vllm:0.19.0-r38.2.arm64-sbsa-cu130-24.04 sleep infinity
docker exec -d vllm bash -c 'HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 vllm serve \
  Qwen/Qwen3-8B --host 0.0.0.0 --port 8000 --max-model-len 16384 \
  --enable-auto-tool-choice --tool-call-parser hermes > /data/vllm_serve.log 2>&1'
```
⚠️ **Deux bugs matériel/pilote confirmés sur cette Thor** (pas le code de l'agent) :
1. Le conteneur perd parfois son attache réseau après un restart — toujours `docker rm
   -f` + recréer, jamais `docker start` sur un conteneur existant.
2. La mémoire GPU n'est **jamais rendue** quand `vllm serve` s'arrête, même proprement
   — seul fix connu : `sudo reboot` complet de la Thor. **Ne jamais arrêter `vllm
   serve` sans bonne raison** (ça redémarre aussi le service vision qui tourne sur la
   même machine).

**Nouvelles dépendances** (`requirements.txt`) : `faster-whisper`, `tokenizers==0.15.2`
(épinglé — la 0.21.0+ n'a pas de wheel aarch64/cp38, pip la recompile et produit un
`.so` cassé), `torch` (Silero VAD, CPU), `scipy` (`resample_poly`, resampling
anti-repliement 48k→16k — un simple `np.interp` dégradait fortement Whisper). Piper
lui-même est un **binaire** (`~/piper/piper`), pas un paquet pip (aucun wheel
aarch64/cp38 pour `piper-tts`).

⚠️ **Portée volontairement réduite** — pas encore vision, tablette, agenda, emails, ni
modes `cdg`/`daneel` testés. Voir § 5 pour les bugs ouverts (confusion de tool sur le
geste poignée de main, latence Whisper variable, bouclages de répétition).

## 3. CLI complet de `main.py`

Toujours depuis `/home/unitree/unitree_sdk2_python` (requis par le SDK Unitree) :

```bash
python3.8 /home/unitree/g1_agent_interim/main.py [--mode cdg|iinterim|daneel] \
    [--translate | --no-translate] [--engine openai|local]
```

| Flag | Valeurs | Défaut | Effet |
|---|---|---|---|
| `--mode` | `cdg`, `iinterim`, `daneel` | `_DEFAULT_MODE` de `config.py` (`cdg` aujourd'hui) | Personnage actif → `SYSTEM_PROMPT`, tools importés, modèle Realtime |
| `--translate` / `--no-translate` | flag | auto (`ON` seulement si `--mode daneel`) | Force/désactive la traduction interprète fr⇄zh, indépendamment du personnage |
| `--engine` | `openai`, `local` | `openai` | Moteur conversationnel — cloud Realtime (§2.1) ou pipeline embarqué (§2.2) |

`parse_known_args` : les flags inconnus sont ignorés (pas d'erreur), au cas où
`main.py` soit un jour lancé via un wrapper qui ajoute ses propres options.

### Commandes utiles

```bash
# Mode en service aujourd'hui (Charly, CDG) — inchangé
cd /home/unitree/unitree_sdk2_python && python3.8 /home/unitree/g1_agent_interim/main.py --mode cdg

# Agence I-Interim
python3.8 /home/unitree/g1_agent_interim/main.py --mode iinterim

# Interprète fr⇄zh (traduction activée automatiquement)
python3.8 /home/unitree/g1_agent_interim/main.py --mode daneel

# Persona daneel SANS traduction (conversationnel normal)
python3.8 /home/unitree/g1_agent_interim/main.py --mode daneel --no-translate

# Forcer la traduction sur un autre mode (rare, diagnostic)
python3.8 /home/unitree/g1_agent_interim/main.py --mode cdg --translate

# Sans --mode : retombe sur _DEFAULT_MODE (config.py)
python3.8 /home/unitree/g1_agent_interim/main.py

# NOUVEAU — pipeline conversationnel 100% local (squelette, § 2.2)
# Nécessite le conteneur vLLM lancé sur la Thor (192.168.123.166:8000)
python3.8 /home/unitree/g1_agent_interim/main.py --mode iinterim --engine local

# Diagnostic du pipeline local SANS micro/HP (texte seul, 6 cas scriptés)
python3.8 /home/unitree/g1_agent_interim/scripts/test_vllm_pipeline.py
```

⚠️ `--engine local` re-exécute le process une fois au démarrage (`os.execv`) pour
positionner `LD_PRELOAD` (fix `libgomp`/`ctranslate2`, voir § 2.2 et
`changements/2026-09-24-vllm-migration.md`) — normal de voir le process redémarrer
une fois dans les logs juste après le lancement.

**Arrêt** (les deux moteurs) : `Ctrl+C`. Fantôme sur le port 8000 :
`pkill -f g1_agent_interim/main.py`.

## 4. Variables d'environnement nouvelles

```
VLLM_HOST          # défaut http://192.168.123.166:8000 — serveur vLLM sur la Thor
VLLM_MODEL         # défaut Qwen/Qwen3-8B
PIPER_VOICE_MODEL  # défaut /home/unitree/piper_voices/fr_FR-siwis-medium.onnx
```
Lues à l'import de `config.py`, comme les autres — relancer `main.py` après
modification du `.env`.

## 5. Statut et travail restant (pipeline local uniquement)

- ⚠️ **Bug ouvert — geste poignée de main** : le modèle écrit parfois
  `executer_geste(geste="serre_main")` en texte brut au lieu d'un vrai `tool_call`
  structuré (le parser `hermes` de vLLM ne le reconnaît pas) → geste non exécuté.
  Prompt déjà renforcé (orthographe exacte, un seul tool de geste), sans effet sur ce
  cas précis — probablement un problème de format d'émission, pas de vocabulaire.
- ⚠️ **Latence Whisper variable** — stabilisée en verrouillant la Jetson en mode MAXN
  (`sudo nvpmodel -m 0` + `sudo jetson_clocks`), mais ce réglage **ne survit pas à un
  reboot** et augmente conso/chaleur/bruit tant qu'il est actif — pas encore acté comme
  permanent.
- ⚠️ **Bouclages de répétition Whisper** sur audio plus long — mitigé
  (`condition_on_previous_text=False`, `no_repeat_ngram_size=3`), pas revérifié sur un
  vrai cas de bouclage.
- Nettoyage à faire : dossiers de transfert résiduels `~/hf_bootstrap` (Orin),
  `~/hf_import` (Thor).
- **Ne pas documenter ce chemin dans `CLAUDE.md`/`README.md` avant validation vocale
  complète** (latence + qualité jugées satisfaisantes par l'utilisateur) — ce fichier
  sert de repère en attendant.
- Modes `cdg`/`daneel` et tools hors périmètre (vision, tablette, agenda, emails,
  Spotify) : non touchés par `--engine local`, comme prévu dès le départ.
