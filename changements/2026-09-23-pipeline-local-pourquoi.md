# Pipeline conversationnel local — pourquoi, et pour qui

Ce document explique, sans rentrer dans le code, ce qu'on est en train de
construire avec le "pipeline local" (`--engine local`) et pourquoi. Détail
technique des fichiers : `changements/2026-09-23-pipeline-local-ollama.md`.
Détail archi complet : `THOR.md` + `CLAUDE.md`.

## Comment ça marche aujourd'hui

Le robot ne "pense" pas lui-même. Tout ce qu'il entend part en streaming vers
l'API OpenAI Realtime, dans le cloud : c'est là que le modèle écoute, décide
quoi répondre ou quel outil appeler (regarder l'heure, faire un geste,
chercher un vol...), et génère la voix qui revient. Le Jetson embarqué du
robot ne fait qu'exécuter les outils et jouer le son — le "cerveau" tourne
chez OpenAI, sur internet.

Ça marche bien, mais ça a trois implications à assumer en continu :
- **Dépendance internet** : pas de wifi/4G correct → pas d'agent du tout.
- **Coût récurrent** : chaque minute de conversation est facturée à l'usage
  (API OpenAI).
- **Les conversations sortent du robot** : tout ce que dit un visiteur (et le
  robot) transite par les serveurs OpenAI. Pour un agent de sûreté aéroportuaire
  au Terminal 2F, ou pour l'agence I-Interim qui traite des données
  candidats/badges, c'est une question qui peut légitimement se poser.

## Ce qu'on construit

Une chaîne alternative qui tourne **entièrement sur le matériel du robot** :

```
Micro → détection de la voix (VAD) → transcription (faster-whisper)
      → modèle de langage LOCAL (Ollama, sur la Jetson Thor du robot)
      → vérification du format de sortie
      → synthèse vocale (Piper) → haut-parleur
```

Rien ne sort du réseau du robot. Le "cerveau" (le modèle de langage) tourne
sur la Jetson Thor déjà présente à bord — la même machine qui fait déjà
tourner la reconnaissance faciale et la détection d'objets par vision (voir
THOR.md). On ne loue plus d'intelligence dans le cloud, on utilise celle
qu'on a déjà embarquée.

Activable au choix, sans rien casser : `main.py --engine local` ou
`--engine openai` (le comportement actuel, par défaut, inchangé). Les deux
peuvent coexister le temps de la validation.

## Le gain

- **Indépendance réseau** : le cœur de la conversation ne dépend plus d'une
  connexion internet fiable. Seule la vision (déjà déportée sur la Thor) et
  quelques tools annexes (recherche web, vols, emails...) ont encore besoin
  d'internet — pas la conversation elle-même.
- **Coût** : plus de facturation à la minute/au token pour la conversation.
  Investissement matériel déjà fait (la Thor est déjà à bord pour la vision).
- **Confidentialité / souveraineté des données** : les échanges (candidats
  à l'agence, passagers au Terminal 2F) restent sur le réseau du robot,
  jamais envoyés à un tiers cloud.
- **Pas de dépendance à un fournisseur** : le modèle, le prompt et le
  comportement sont entièrement sous notre contrôle — pas de dépréciation
  surprise d'un modèle OpenAI, pas de changement de comportement de l'API
  décidé ailleurs.

## Ce que ça coûte en échange (honnêtement)

- **Un modèle plus petit** : `mistral-small`/`qwen3:8b` tournant sur une
  Jetson sont nettement moins capables qu'un modèle cloud de pointe — d'où
  le besoin de comparer les deux avant de trancher (`scripts/
  compare_local_models.py`), et une couche de validation supplémentaire
  (`agent_local/validator.py`) pour rattraper les sorties hors-piste,
  plus fréquentes sur un petit modèle.
- **Latence à valider** : ASR + LLM + TTS enchaînés localement peut être
  plus lent (ou plus rapide, à mesurer) que le streaming temps réel actuel
  d'OpenAI — pas encore mesuré en conditions réelles.
- **Périmètre volontairement réduit pour l'instant** : ce premier squelette
  ne porte que 3 outils (gestes, heure, arbre de décision visiteur) sur le
  mode `iinterim` uniquement — pas encore la vision, la tablette, l'agenda,
  les emails, ni les modes `cdg`/`daneel`.
- **Pas encore déployé** : à date, le code est prêt et testé isolément, mais
  le conteneur Ollama n'est pas encore lancé sur la Thor et rien n'a encore
  tourné de bout en bout avec le micro/haut-parleur du robot.

## En une phrase

On teste si on peut remplacer "louer un cerveau dans le cloud, minute par
minute" par "utiliser le cerveau qu'on a déjà à bord" — au prix d'un modèle
plus modeste, pour gagner l'indépendance réseau, la confidentialité et le
coût récurrent.
