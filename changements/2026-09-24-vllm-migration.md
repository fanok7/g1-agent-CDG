# Migration Ollama → vLLM + état du pipeline local

Date : 2026-09-24 (suite de `2026-09-23-pipeline-local-ollama.md`)

## Pourquoi ce changement

Ollama produisait une sortie GPU corrompue (charabia) sur la Jetson Thor au-delà
d'un certain volume de prompt — bug NVIDIA confirmé et non résolu (voir
sources dans la conversation, forums NVIDIA + GitHub `ollama/ollama`).
Remplacé par **vLLM**, qui tourne correctement sur ce même GPU (validé
empiriquement, prompt complet de 4300+ tokens, réponses cohérentes).

## Ce qui a changé dans le code

- **Supprimés** : `agent_local/ollama_client.py`, `scripts/build_modelfiles.py`,
  `models/` (Modelfiles Ollama — concept qui n'existe pas côté vLLM, le
  system prompt est envoyé à chaque requête, pas baké dans un modèle).
- **`agent_local/vllm_client.py`** (nouveau, remplace `ollama_client.py`) —
  client `/v1/chat/completions` (API OpenAI standard). Point de conception
  important, découvert empiriquement : **`response_format` (JSON forcé) et
  les tool_calls sont incompatibles sur ce serveur** — dès qu'un format JSON
  est imposé, le modèle ne peut plus émettre la syntaxe d'appel d'outil.
  Solution : abandon du format JSON `{"reponse","geste"}`, retour à du texte
  libre + tool-calling standard pour tout, y compris les gestes
  (`executer_geste` est un tool comme les autres, exécuté en effet de bord
  dans `run_turn()`) — exactement le même principe que le pipeline OpenAI
  Realtime existant (`agent/events.py`).
- **`agent_local/validator.py`** — simplifié en conséquence : valide du texte
  brut (didascalies, jargon, injection), plus de schema JSON à vérifier.
- **`agent_local/schema_adapter.py`** — `get_ollama_tools()` renommé
  `get_tool_schemas()` (le format nested `{'type':'function','function':{...}}`
  est le même pour Ollama et OpenAI/vLLM, donc pas de changement de logique).
- **`agent_local/orchestrator.py`** — simplifié : plus besoin d'extraire un
  champ "geste" séparément, le tool s'exécute déjà tout seul dans
  `run_turn()`.
- **`config.py`** — `OLLAMA_*` remplacés par `VLLM_HOST`
  (`http://192.168.123.166:8000`) et `VLLM_MODEL` (`Qwen/Qwen3-8B`).
- **`scripts/test_vllm_pipeline.py`** (remplace `compare_local_models.py`,
  qui comparait 2 modèles Ollama — plus pertinent avec un seul moteur).

## État du déploiement sur la Thor (192.168.123.166)

Conteneur vLLM officiel NVIDIA (`ghcr.io/nvidia-ai-iot/vllm:0.19.0-r38.2.arm64-sbsa-cu130-24.04`),
lancé avec :
```bash
docker run -d --name vllm --restart unless-stopped --runtime nvidia \
  --ipc=host -p 192.168.123.166:8000:8000 -v vllm_data:/data \
  ghcr.io/nvidia-ai-iot/vllm:0.19.0-r38.2.arm64-sbsa-cu130-24.04 \
  sleep infinity

docker exec -d vllm bash -c 'HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  vllm serve Qwen/Qwen3-8B --host 0.0.0.0 --port 8000 --max-model-len 16384 \
  --enable-auto-tool-choice --tool-call-parser hermes \
  > /data/vllm_serve.log 2>&1'
```
Modèle Qwen3-8B (HuggingFace, bf16, 16 Go) dans le volume `vllm_data`, chemin
`/data/models/huggingface/models--Qwen--Qwen3-8B` (⚠️ PAS de sous-dossier
`hub/` — l'image fixe `HUGGINGFACE_HUB_CACHE=/data/models/huggingface`
directement, sans convention standard `$HF_HOME/hub`).

## ⚠️ Deux bugs matériel/pilote confirmés sur cette Thor (pas notre code)

1. **Le conteneur perd parfois son attache réseau** après un restart/exit
   (`docker inspect vllm --format '{{json .NetworkSettings.Networks}}'` rend
   `{}`) — le port mapping devient inactif silencieusement. Fix : toujours
   `docker rm -f` + recréer plutôt que `docker start` sur un conteneur
   existant.
2. **La mémoire GPU n'est jamais rendue** quand `vllm serve` s'arrête (même
   proprement, `pkill` sans `-9`) — confirmé empiriquement 3 fois aujourd'hui.
   **Un seul fix connu : redémarrer entièrement la Thor** (`sudo reboot`,
   nécessite le mot de passe — je n'ai pas de sudo passwordless dessus).
   Conséquence pratique : **ne jamais arrêter `vllm serve` sans une bonne
   raison** — chaque arrêt coûte un reboot complet de la machine (et
   redémarre aussi le service vision qui tourne dessus).

## Résultats du test (texte, 6 cas — `scripts/test_vllm_pipeline.py`)

| Cas | Latence | Résultat |
|---|---|---|
| salutation | 3.0s | ✅ |
| heure | 3.1s | ✅ tool `date_heure_actuelle` |
| poignée de main | 3.5s | ⚠️ appelle un tool inexistant `serre_main` au lieu de `executer_geste(geste="serrer_main")` — geste non exécuté |
| RDV sans documents | 3.2s | ✅ |
| filière hors périmètre | 10.9s | ✅ (3 tours) |
| filière valide, diplômes OK | 16.0s | ✅ (4 tours) |

Aucune didascalie/jargon détecté, arbre de décision visiteur bien suivi.

## Ce qu'il reste à faire

1. ⚠️ **Confusion de nom d'outil pour les gestes — partiellement traitée,
   toujours en échec (2026-09-24)** :
   - Ajouté : description du schema `executer_geste` (`tools/gesture_tool.py`)
     et intro du bloc `_GESTES` (`config.py`) précisant que c'est le SEUL tool
     de geste, avec exemple d'appel et orthographe exacte de la valeur
     (`serrer_main`, pas `serre_main`).
   - Résultat mesuré (`scripts/test_vllm_pipeline.py`, cas
     `poignee_de_main`) : **inchangé après les deux tentatives**. Le modèle
     n'invente plus de tool `serre_main`, mais écrit maintenant
     `executer_geste(geste="serre_main")` en **texte brut dans le contenu de
     la réponse**, pas comme un vrai `tool_call` structuré — le parser
     `--tool-call-parser hermes` de vLLM ne le reconnaît donc pas et le geste
     n'est jamais exécuté. Le prompt à lui seul ne suffit pas ; c'est un
     problème de format d'émission du tool_call sur ce cas précis, pas
     seulement de vocabulaire.
   - Pistes non essayées : regarder si ce cas particulier (poignée de main)
     déclenche un raisonnement `<think>` Qwen3 différent qui casse le
     parsing hermes ; tester avec `--tool-call-parser` alternatif ou
     `enable_thinking=False` explicite côté requête ; logger le contenu brut
     renvoyé par vLLM (`/data/vllm_serve.log` sur la Thor) pour ce cas précis
     avant d'ajuster encore le prompt.
2. ✅ **Installer ASR + TTS sur l'Orin NX** — fait (2026-09-24), tout validé
   individuellement par du code exécuté (pas seulement une installation) :
   - `faster-whisper` installé (pip --user). ⚠️ Piège rencontré : sa
     dépendance `tokenizers` (défaut 0.21.0) n'a pas de wheel manylinux
     aarch64/cp38 sur PyPI — pip la reconstruit depuis les sources et produit
     un `.so` cassé (`undefined symbol: PyInterpreterState_Get`, symbole
     absent en Python 3.8). Fixé en épinglant `tokenizers==0.15.2` (dernière
     version avec wheel prébuilt cp38-aarch64) ; ajouté dans
     `requirements.txt` avec l'explication, pour que ça ne recasse pas au
     prochain `pip install -r requirements.txt`. Testé : chargement du
     modèle `base` int8 + `transcribe()` sur un buffer silence → OK.
   - Silero VAD : `torch` était déjà installé (2.0.0+nv23.05, présent avant
     cette session). Téléchargé et mis en cache via `torch.hub.load(...)`
     (`~/.cache/torch/hub/`) → OK, fonctionne hors-ligne ensuite.
   - Piper : le paquet pip `piper-tts` n'a **aucun wheel** pour
     aarch64/cp38 (dépendance native `piper-phonemize`, `ResolutionImpossible`
     puis `No matching distribution`) — confirme la décision déjà actée dans
     `agent_local/tts.py` d'utiliser le **binaire** plutôt que le paquet
     Python. Installé depuis les releases GitHub officielles
     (`rhasspy/piper`, tag `2023.11.14-2`, asset `piper_linux_aarch64.tar.gz`)
     dans `~/piper/` (RPATH interne, pas besoin de `LD_LIBRARY_PATH`) ; ajouté
     `export PATH="$HOME/piper:$PATH"` dans `~/.bashrc` pour que
     `shutil.which('piper')` (`agent_local/tts.py`) le trouve.
   - Voix française `fr_FR-siwis-medium` téléchargée (HuggingFace
     `rhasspy/piper-voices`) dans `~/piper_voices/` — chemin exact déjà
     attendu par défaut dans `config.PIPER_VOICE_MODEL`.
   - Test bout en bout : `agent_local.tts.synthesize("Bonjour, bienvenue
     chez i-interim.")` → 121614 octets PCM 24kHz produits, aucune erreur.
     RTF piper mesuré ≈ 0.2 (largement temps-réel sur CPU).
3. ✅ **Test vocal réel (ASR seul) — validé (2026-09-24)**, en plusieurs
   étapes car les tests automatisés en ligne de commande ne peuvent pas se
   synchroniser avec une vraie voix humaine (aucun canal pour dire "parlez
   maintenant" en temps réel à travers un outil non-interactif) :
   - **Bug réel trouvé et corrigé dans `agent_local/asr.py`** : `_FRAME_MS =
     30` donne, après resampling 48kHz→16kHz, des frames de 480 échantillons
     — Silero VAD (version actuelle, via `torch.hub`) exige un minimum de
     512 échantillons à 16kHz et levait `torch.jit.Error: Input audio chunk
     is too short`, plantant `listen_for_utterance()` dès la première frame.
     Corrigé : `_FRAME_MS` passé à 32 (32ms × 16000Hz = 512 échantillons
     pile) + filet de sécurité dans `_prob()` (pad à 512 si un taux micro
     non standard fait encore arrondir en dessous).
   - **Faux négatif intermédiaire** : un premier test a renvoyé `"bonheur
     bonheur bonheur bonheur bonheur"` — pas une hallucination Whisper mais
     un vrai silence côté capture (RMS mesuré à 0.0 exact, y compris en
     contournant complètement Python/PortAudio avec `arecord` direct sur
     `hw:0,0`) : le **bouton mute physique du boîtier PowerConf** était
     activé. Une fois désactivé par l'utilisateur, confirmé avec
     `arecord ... && aplay ...` lancé et écouté par l'utilisateur lui-même
     (seul moyen fiable de synchroniser la parole avec l'enregistrement).
   - **Validation finale** : le fichier `/tmp/mic_test.wav` ainsi obtenu
     (RMS=0.064, contient "Hello" dit par l'utilisateur) a été rejoué dans
     le pipeline réel — `_resample()` (48k→16k) → Silero VAD (probs jusqu'à
     0.999 exactement sur le segment de parole, ~0 avant/après, donc
     segmentation correcte) → `faster-whisper` (`transcribe(language='fr')`)
     → **transcription `"Hello !"`, exacte**. Chaîne complète confirmée
     fonctionnelle sur de l'audio réel.
   - **Pipeline complet lancé pour la première fois** (`main.py --mode
     iinterim --engine local`) — deux nouveaux bugs réels trouvés et
     corrigés, plus un problème de qualité/latence :
     - `agent_local/tts.py` : `shutil.which('piper')` dépend du `PATH` du
       shell qui a lancé `main.py` — ne trouvait rien dans un terminal ouvert
       avant l'ajout à `~/.bashrc` (et n'aurait pas fonctionné non plus sous
       systemd, qui ne source jamais `.bashrc`). **Corrigé** : repli
       explicite sur `~/piper/piper` dans `_find_piper_bin()`, indépendant
       du shell.
     - **Qualité de transcription mauvaise en conditions réelles**
       (répétitions, mots incohérents : "Vomu de l'autre", "Je vous ai déjà
       fait des choses" en boucle) — root cause : `_resample()` utilisait
       `np.interp` (interpolation linéaire), **sans filtre anti-repliement**,
       pour décimer 48kHz→16kHz. Le micro capte de l'énergie au-delà de
       8kHz (Nyquist de la cible) qui se replie en bruit dans la bande utile
       une fois décimée sans filtre — dégrade fortement Whisper. **Corrigé** :
       `scipy.signal.resample_poly` (filtre FIR correct, ratio exact 1:3
       calculé via `math.gcd`) ; `scipy` ajouté à `requirements.txt`. Revérifié
       sur `/tmp/mic_test.wav` : toujours `"Hello"` correct après le
       changement.
     - **Latence perçue ~10s** — cause identifiée par instrumentation ajoutée
       (`[ASR] écoute+VAD / whisper`, `[LOCAL] Latence TTS`) : `WhisperModel`
       sans `cpu_threads` explicite tournait ~3-4x plus lentement que
       nécessaire sur cette Jetson (8 cœurs) — mesuré : 4.27s (défaut) vs
       1.14s (`cpu_threads=4`) pour transcrire 3s d'audio. **Corrigé** :
       `cpu_threads=4` fixé dans `Listener.__init__`. Pas encore mesuré l'effet
       cumulé sur la latence totale ressentie (ASR + vLLM ~1.6-3.2s + TTS +
       lecture) — **à revalider avec un nouveau run vocal réel**.
   - **Nouveau crash au relancement** (avant même la 1ère écoute) :
     `ImportError: ... libgomp-d22c30c5.so.1.0.0: cannot allocate memory in
     static TLS block` à `from faster_whisper import WhisperModel` →
     `import ctranslate2`. Classique sur aarch64/glibc : `ctranslate2`
     embarque sa propre copie de `libgomp` et la charge tardivement
     (`dlopen`) ; selon ce qui est déjà chargé dans le process à ce moment
     (dépend de l'environnement du shell — **non reproduit dans mes propres
     tests**, y compris en rejouant l'import exact des 52 tools de
     `main.py` puis `WhisperModel`), glibc peut refuser de lui allouer de
     l'espace TLS statique. **Corrigé** par le fix standard de ce problème :
     `main.py` re-exécute maintenant lui-même le process avec `LD_PRELOAD`
     pointé sur la copie de libgomp de `ctranslate2.libs/` (repli sur la
     libgomp système si absente), **avant** tout autre import — ainsi elle
     est comptée dans le budget TLS initial. Limité à `--engine local`
     (guard `if _args.engine == "local"` dans `main.py`, juste après le
     parsing argparse) : **aucun risque sur le chemin `cdg`/`openai` en
     service aujourd'hui**. Testé isolément (re-exec sans boucle infinie,
     `WhisperModel` charge sous `LD_PRELOAD`) mais **pas encore confirmé sur
     le crash réel** puisque je ne l'ai pas reproduit moi-même — à valider
     au prochain lancement par l'utilisateur.
   - ✅ **Confirmé par l'utilisateur** : le fix `LD_PRELOAD` passe, plus de
     crash `libgomp`, `date_heure_actuelle` appelé et répondu correctement
     de bout en bout (ASR→vLLM→TTS→HP).
   - ⚠️ **Nouveaux problèmes réels révélés par ce premier run complet** :
     - **Latence Whisper très variable** (1.31s / 6.34s / 14.44s / 6.41s
       pour des audios de 1.3 à 8.9s, sans corrélation propre avec la durée)
       — cause trouvée : la Jetson tournait en **mode 25W**, CPU plafonné à
       1.5GHz, gouverneur `schedutil` (fréquence dynamique selon charge,
       jamais stabilisée). **Corrigé** (avec accord explicite de
       l'utilisateur, changement système) : `sudo nvpmodel -m 0` (MAXN) +
       `sudo jetson_clocks` → CPU verrouillé à 1.98GHz sur les 8 cœurs.
       Rebenchmarké : 2.26s / 2.30s / 1.27s / 0.81s sur 4 runs successifs du
       même audio 3s — nettement plus stable, converge vers RTF≈0.3-0.4
       après un premier run plus lent (cache CPU/scheduling, pas la
       fréquence). ⚠️ **Compromis assumé, pas encore acté comme permanent** :
       MAXN + jetson_clocks augmente conso/chaleur/bruit ventilateur tant
       que c'est actif ; ne survit pas à un reboot (à re-appliquer, ou
       scripter au démarrage si le mode local devient permanent).
     - **Bouclage de répétition Whisper** toujours présent sur audio plus
       long ("Bonne soirée. Bonne soirée. Merci beaucoup. Merci beaucoup...")
       — classique du décodage glouton (`beam_size=1`) sans pénalité de
       répétition. Le tour suivant ("Oui.") a halluciné une réponse "Bonne
       soirée." cohérente avec ce contexte corrompu — **pas un bug
       distinct**, un effet en cascade du même problème. **Corrigé** (pas
       encore revérifié sur un vrai cas de bouclage, faute de pouvoir le
       reproduire moi-même) : ajout de `condition_on_previous_text=False` +
       `no_repeat_ngram_size=3` à l'appel `transcribe()` — mitigation connue
       et documentée de ce mode d'échec Whisper. Revérifié seulement que ça
       ne casse pas la transcription "Hello" déjà validée.
   - **Prochaine étape** : nouveau run vocal réel pour juger si
     latence + qualité sont maintenant satisfaisantes ; si le bouclage
     persiste malgré `no_repeat_ngram_size`, la piste suivante est un modèle
     Whisper plus grand (`small`) plutôt que d'empiler encore des
     paramètres de décodage sur `base`.
4. **Nettoyer les dossiers de transfert résiduels** : `~/hf_bootstrap` (Orin),
   `~/hf_import` (Thor) — copies intermédiaires, plus utiles une fois le
   modèle confirmé stable dans le volume `vllm_data`.
5. **Documenter dans CLAUDE.md/README** une fois le pipeline validé
   vocalement — pour l'instant tout est dans `changements/`.
6. Modes `cdg`/`daneel` et tools hors périmètre : toujours non touchés, comme
   prévu dans le plan initial.
