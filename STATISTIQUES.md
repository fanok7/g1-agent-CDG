# 📊 Statistiques du robot G1 — mémo

Statistiques **anonymes** d'usage : questions fréquentes des passagers, langues
parlées, zones sollicitées, et satisfaction. Aucune donnée personnelle.

---

## Comment y accéder

Le **dashboard Streamlit** est le seul point de visualisation. Mode d'emploi
complet (lancement, lecture, export Excel) : **[DASHBOARD.md](DASHBOARD.md)**.

En bref — sur le robot :

```bash
cd /home/unitree/g1_agent_interim && bash scripts/lancer_dashboard.sh
```

Puis, dans le navigateur du PC : **http://192.168.123.164:8501**. Les données sont
récupérées via l'API du robot (`GET /api/export-logs`) ou lues localement.

---

## Ce qui est mesuré

| Indicateur | Source |
|---|---|
| **Questions fréquentes** | transcriptions du visiteur, regroupées par formulation |
| **Langues** | langue détectée dans la voix du visiteur |
| **Sujets des questions** | mots-clés (fr+en) → Vols, Hôtels, Transport, Commodités… |
| **Services utilisés** | tool réellement appelé → zone (fiable, toutes langues) |
| **Activité par jour** | nombre d'interactions par date |
| **Affluence par heure** | interactions par tranche horaire (0-23 h) → heures de pointe |
| **Appels d'assistance** 🆘 | chaque appel lancé : heure, motif, langue |
| **Alertes sécurité** 🚨 | chutes / feu / fumée détectés par la vision |
| **Interactions totales / jour le + actif** | vue d'ensemble |
| **Satisfaction** | note 1-5 ★, note moyenne, **satisfaction par zone** |

La **satisfaction par zone** est la plus utile : elle croise la note avec le
sujet de la question notée. Ex. « Hôtels : 1,5 ★ » = un sujet mal traité à
corriger. Barres colorées : rouge ≤ 2,5 ★, orange ≤ 3,5 ★, vert au-dessus.

---

## Anonymat (par conception)

- **Aucune identité** : ni nom, ni reconnaissance faciale, ni session reliée à
  une personne.
- **Chiffres masqués** dans les questions (numéro de badge / téléphone → `#`).
- **Horodatage à la minute** seulement — pas de reconstitution de parcours.

---

## Où sont les données

| Fichier | Contenu |
|---|---|
| `tablet_server/analytics_events.jsonl` | tous les événements (1 JSON/ligne) — **source du dashboard** |
| `tablet_server/statistiques_robot.csv` | historique brut des avis (sauvegarde) |

Les deux sont **gitignorés** (données locales, non versionnées).

**Repartir de zéro** (efface tout l'historique de stats) :

```bash
rm tablet_server/analytics_events.jsonl tablet_server/statistiques_robot.csv

bonjour
```

---

## Sous le capot (pour maintenance)

- **`analytics.py`** — collecte uniquement, via la classe `AnalyticsLogger`
  (écriture asynchrone, session éphémère 2 min, event `health` toutes les 15 min).
  Fonctions : `log_question()`, `log_tool()`, `log_rating()`, `log_call()`,
  `log_alert()`. **Plus d'agrégation ici** : la visualisation est 100 % dans le
  dashboard Streamlit (`dashboard_stats.py`).
- **Capture** dans `agent/events.py` (best-effort, n'interrompt jamais la
  conversation) : transcription → `log_question`, appel de tool → `log_tool`,
  chute/feu → `log_alert`. Avis tablette → `POST /api/rating` → `log_rating` (+ CSV).
  Appel d'assistance → `log_call` au raccrochage (durée + répondu).
- **Restitution** : `GET /api/export-logs` sert le `.jsonl` brut au dashboard.
- **Ajouter une zone** : compléter `_TOOL_ZONE` (par tool) ou `_KEYWORD_ZONE`
  (par mots-clés) dans `analytics.py`.
