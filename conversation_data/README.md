# conversation_data — corpus de conversations réelles

Dossier créé le 2026-09-24. Objectif : constituer un corpus de conversations
réelles avec l'agent (visiteurs I-Interim, passagers CDG) pour évaluer et,
à terme, fine-tuner le pipeline local (`agent_local/`, `--engine local`) —
voir [changements/2026-09-23-pipeline-local-pourquoi.md](../changements/2026-09-23-pipeline-local-pourquoi.md)
pour le contexte de ce pipeline.

## Ce qui est collecté

Chaque conversation est archivée en texte intégral (pas juste des métriques
anonymes) à la fin de la session — déclenché par `/reset` dans
`tablet_server/server.py`, appelé par la tablette après une longue
inactivité. S'y ajoutent, avec le même mécanisme, deux cas déjà existants
avant ce dossier : une conversation notée ≤2★, ou une conversation ayant
déclenché un appel d'assistance humaine.

Écrit par `analytics.py` (`log_transcript`) dans **`transcripts_archive.jsonl`**
(une ligne JSON par session) :
```json
{"ts": "...", "session_id": "sess_xxxxx", "reason": "session_end", "turns": [{"role": "user", "text": "..."}, {"role": "assistant", "text": "..."}], "meta": {}}
```
`reason` vaut `session_end`, `low_rating`, ou `assistance_call`.

Ce fichier ne fait **pas doublon** avec `tablet_server/analytics_events.jsonl`
(télémétrie anonyme du dashboard Streamlit, voir [STATISTIQUES.md](../STATISTIQUES.md))
— celui-ci reste anonyme et sert à d'autres fins (statistiques d'usage). Ici,
c'est le texte brut, gardé exprès pour avoir de la vraie donnée de
conversation à relire et réutiliser.

### Dataset par tour — `turns_dataset.jsonl` (2026-11-02)

Complète `transcripts_archive.jsonl` (qui n'écrit qu'en fin de session) par
un enregistrement **à chaque tour** (question + tool_calls réels + réponse),
le format directement utile pour comparer/évaluer/fine-tuner le pipeline
local plus tard — un tour isolé avec son contexte d'appel d'outils, plutôt
qu'une conversation entière à ré-découper après coup.

Écrit par `analytics.py` (`log_turn`), appelé depuis `agent/events.py` à la
fin de chaque réponse parlée, dans **`turns_dataset.jsonl`** :
```json
{"ts": "...", "session_id": "sess_xxxxx", "mode": "iinterim", "user_text": "...", "assistant_text": "...", "tool_calls": [{"name": "executer_geste", "args": {"geste": "saluer"}, "status": "ok"}], "lang": "fr", "latency_ms": 1234, "is_fallback": false}
```
`tool_calls` est la liste réelle des outils appelés par le modèle pendant ce
tour (nom, arguments, statut ok/error) — permet de voir, pour une question
donnée, si le bon outil a été appelé avec les bons arguments (le genre de
problème relevé dans `changements/2026-09-24-vllm-migration.md`, § confusion
de nom d'outil pour les gestes). `mode` distingue `iinterim`/`cdg`/`daneel`.

Mêmes précautions que `transcripts_archive.jsonl` ci-dessous (texte brut,
gitignoré, jamais poussé) — voir § données sensibles.

## Ce que ça contient comme données sensibles

Ce fichier peut contenir des **données personnelles** : nom donné pour
vérifier un rendez-vous, numéro de badge ou de téléphone dicté à l'oral,
motif médical exposé lors d'une demande d'assistance, contenu d'un billet
d'avion scanné évoqué dans la conversation... Rien n'est filtré à
l'écriture.

`turns_dataset.jsonl` contient exactement le même type de données
sensibles (texte brut), en plus fréquent (un enregistrement par tour, pas
par session) — donc un volume et une exposition plus grands à volume de
visiteurs égal.

Conséquences pratiques :
- **`transcripts_archive.jsonl` et `turns_dataset.jsonl` sont gitignorés**
  (`conversation_data/*.jsonl` dans `.gitignore`) — ne jamais forcer un
  `git add` dessus.
- **Ne jamais faire sortir ces fichiers du Jetson tels quels** (email,
  upload, partage) — utiliser une version anonymisée (voir ci-dessous ;
  `anonymize_names.py` ne couvre aujourd'hui que `transcripts_archive.jsonl`,
  pas encore `turns_dataset.jsonl`).
- **Politique de rétention : pas encore définie.** Les deux fichiers
  grossissent sans purge automatique. À trancher avant un usage prolongé en
  production (par qui, combien de temps, qui a accès).

## Anonymisation des noms

`anonymize_names.py` lit `transcripts_archive.jsonl` et écrit
`transcripts_archive.anon.jsonl` à côté (idem gitignoré), avec les noms
propres de personnes remplacés par `[NOM]` dans le texte des tours. Le
fichier brut n'est jamais modifié — relancer le script à tout moment
reconstruit l'anonymisé en entier depuis le brut.

```bash
python3.8 conversation_data/anonymize_names.py
```

**Méthode actuelle : un appel `gpt-4o-mini` par session** (même logique que
le résumé de mail dans `tools/gmail.py`), donc le texte est envoyé à
OpenAI pour l'anonymisation. Accepté pour l'instant car les conversations
transitent de toute façon par l'API Realtime d'OpenAI en temps réel avec le
moteur `openai` (celui actif aujourd'hui) — ça n'ajoute pas d'exposition
nouvelle. **Si le pipeline local devient la voie de production**, cette
étape devra tourner en local (modèle Ollama sur la Thor) pour ne pas
recréer une fuite vers le cloud que le pipeline local cherche justement à
éliminer — pas encore fait, à prévoir.

## Limites connues

- **Seuls les noms de personnes sont anonymisés.** Numéros de badge,
  numéros de téléphone, adresses ne sont **pas** touchés par ce script —
  hors périmètre de la demande initiale. À ajouter si le corpus doit un jour
  sortir du Jetson ou être partagé plus largement.
- **Best-effort, pas garanti à 100 %** : un appel LLM peut manquer un nom
  (variante orthographique, nom coupé par le bruit de la transcription) ou,
  plus rarement, en supprimer un qui n'en était pas un. Ne pas considérer le
  fichier `.anon.jsonl` comme totalement anonyme sans relecture si l'usage
  l'exige (ex. avant un partage externe).
- **Pas de purge automatique**, pas de limite de taille, pas d'opt-out
  visiteur — ce dossier documente ce qui existe aujourd'hui, pas une
  politique de conformité RGPD complète.

## Fichiers de ce dossier

| Fichier | Contenu | Suivi git |
|---|---|---|
| `README.md` | ce document | ✅ |
| `anonymize_names.py` | script d'anonymisation des noms | ✅ |
| `transcripts_archive.jsonl` | archive brute par session, texte intégral | ❌ gitignoré |
| `transcripts_archive.anon.jsonl` | copie avec noms anonymisés (générée) | ❌ gitignoré |
| `turns_dataset.jsonl` | archive brute par tour (question+tool_calls+réponse) | ❌ gitignoré |
