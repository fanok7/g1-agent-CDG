# Squelette pipeline local (Ollama) — mode `iinterim`

Date : 2026-09-23

## Objectif

Construire une chaîne conversationnelle **100% locale** (ASR → LLM local via
Ollama → validation → TTS) comme alternative à l'API OpenAI Realtime actuelle,
sans jamais sortir du personnage `iinterim` ni de son system prompt, et sans
toucher aux modes `cdg`/`daneel` ni aux tools hors périmètre.

Portée v1 (squelette de validation, pas encore de production) : comparer
`mistral-small` et `qwen3:8b` sur l'adhérence au personnage, le respect du
format de sortie et la latence. Tools exposés au modèle local : uniquement
`executer_geste`, `relacher_bras`, `date_heure_actuelle` + l'arbre de décision
visiteur (purement conversationnel, aucun tool).

Plan détaillé (architecture, décisions validées) :
`/home/unitree/.claude/plans/goofy-inventing-bunny.md`.

## Architecture

```
Orin NX (main.py --engine local)              Jetson Thor (192.168.123.166)
mic → Silero VAD → faster-whisper (ASR)   →   Ollama (conteneur NVIDIA), port 11434
   → ollama_client.run_turn() (tools+format)      modèles : iinterim-mistral, iinterim-qwen
   → validator.py (schema + garde-fou)            (SYSTEM prompt baké depuis config.py)
   → tts.py (Piper) → robot.audio.play_audio
```

ASR et TTS restent sur l'Orin NX (CPU, Piper/faster-whisper légers) — seul
l'appel Ollama traverse le réseau robot vers la Thor, qui héberge déjà
l'inférence vision GPU (voir THOR.md). Piste future si la latence ASR CPU est
trop élevée : déporter `asr.py` sur la Thor via un petit service HTTP, même
patron que `vision/realsense_remote_push.py`.

## Fichiers créés

- **`agent_local/`** (nouveau package)
  - `schema_adapter.py` — whitelist de 3 tools + reformatage vers le format
    tool-calling d'Ollama, à partir de `tools.registry.get_schemas()` (aucun
    tool réécrit).
  - `ollama_client.py` — client HTTP `/api/chat` (pas de streaming, pas de
    call_id) + boucle de tool calls (max 4 itérations), exécution via
    `tools.registry.call()`.
  - `validator.py` — schema attendu `{"reponse": str, "geste": enum|null}`
    (énumération reprise de `robot.gestures.ACTION_MAP`), rejette didascalies,
    jargon technique, marqueurs d'injection/jailbreak.
  - `asr.py` — micro (réutilise `robot.audio.find_microphone`) + VAD Silero +
    transcription faster-whisper (`base`, int8, `language='fr'`). Ignore les
    frames tant que `ipc.RESPONDING_FLAG` existe (pas d'écho de sa propre
    voix).
  - `tts.py` — Piper en sous-processus (binaire CLI), resample vers 24kHz
    (format attendu par `robot.audio.play_audio`).
  - `orchestrator.py` — `run_local()`, boucle agent minimale (pas de
    tablette/vision/Spotify pour ce squelette).

- **`scripts/build_modelfiles.py`** — génère `models/Modelfile.iinterim.mistral`
  et `models/Modelfile.iinterim.qwen` en injectant `config.SYSTEM_PROMPT_IINTERIM`
  tel quel (jamais retapé à la main). À relancer à chaque changement du prompt
  dans `config.py`.

- **`scripts/compare_local_models.py`** — comparaison texte (pas d'audio) des
  deux modèles sur des échanges scriptés couvrant l'arbre de décision
  visiteur, une question d'heure et une demande de geste. Affiche latence,
  conformité de format, et si le tool attendu a été appelé.

- **`models/Modelfile.iinterim.mistral`, `models/Modelfile.iinterim.qwen`** —
  générés par le script ci-dessus (`FROM mistral-small` / `FROM qwen3:8b` +
  `SYSTEM` baké + `temperature 0.3` + `num_ctx 4096`).

## Fichiers modifiés

- **`main.py`** — ajout du flag `--engine openai|local` (défaut `openai`,
  comportement actuel strictement inchangé). Un seul branchement ajouté dans
  `if __name__ == '__main__':` ; le reste de `main.py` (imports de tools,
  `run()`) n'a pas bougé.
- **`config.py`** — nouvelles variables : `OLLAMA_HOST` (défaut
  `http://192.168.123.166:11434`), `OLLAMA_MODEL_MISTRAL`, `OLLAMA_MODEL_QWEN`,
  `OLLAMA_MODEL`, `PIPER_VOICE_MODEL`.
- **`agent/session.py`** — extraction de `build_instructions()` (contexte
  temporel + `SYSTEM_PROMPT`) hors de `connect()`, pour que le pipeline local
  réutilise exactement la même logique plutôt que de la dupliquer.
- **`requirements.txt`** — ajout `faster-whisper`, `torch` (Silero VAD).
  Piper reste piloté en sous-processus (binaire externe, pas de paquet Python
  dans requirements.txt).

## Déploiement (hors code, manuel)

Sur la Jetson Thor (192.168.123.166) :

```bash
docker run -d --name ollama --restart unless-stopped --runtime nvidia \
  -p 192.168.123.166:11434:11434 -v ollama:/root/.ollama \
  ghcr.io/nvidia-ai-iot/ollama:r38.2.arm64-sbsa-cu130-24.04

ollama pull mistral-small && ollama pull qwen3:8b
ollama create iinterim-mistral -f Modelfile.iinterim.mistral
ollama create iinterim-qwen    -f Modelfile.iinterim.qwen
```

Sur l'Orin NX : installer le binaire Piper + une voix française (ex.
`fr_FR-siwis-medium.onnx`) sous `PIPER_VOICE_MODEL`, puis `pip install -r
requirements.txt`.

## Vérifications faites cette session

- `py_compile` (Python 3.8) sur tous les fichiers créés/modifiés — OK.
- `main.py --help` — le flag `--engine` apparaît, les flags existants
  inchangés.
- `schema_adapter.get_ollama_tools()` — format Ollama correct (testé avec
  `date_heure_actuelle` et `executer_geste`, ce dernier nécessitant le SDK
  Unitree).
- `validator.validate()` — accepte une sortie conforme, rejette une
  didascalie entre parenthèses et un JSON invalide.
- `agent.session.build_instructions()` — inchangé fonctionnellement après
  extraction (contexte temporel + prompt identiques).
- `scripts/build_modelfiles.py` — génère les deux Modelfiles, prompt
  identique à `config.SYSTEM_PROMPT_IINTERIM` (14 471 caractères).
- `scripts/compare_local_models.py` — échoue proprement ligne par ligne
  (`ERREUR` par cas) quand le conteneur Ollama n'est pas encore déployé sur
  la Thor (connexion refusée, pas de crash).

## Pas fait (hors périmètre v1, volontairement)

- Déploiement réel du conteneur Ollama sur la Thor et création des modèles.
- Installation de Piper/faster-whisper/torch sur l'Orin NX.
- Test vocal de bout en bout (`main.py --mode iinterim --engine local`) —
  nécessite le déploiement ci-dessus.
- Vérification empirique du comportement d'Ollama quand `tools` et `format`
  sont passés ensemble à `/api/chat` (voir la note dans
  `agent_local/ollama_client.py`) — à faire une fois le conteneur up, avant
  de considérer le squelette validé.
- Élargissement à d'autres tools, aux modes `cdg`/`daneel`, ou migration de
  l'ASR vers la Thor.
