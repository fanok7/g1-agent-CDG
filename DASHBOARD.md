# 📊 Dashboard de télémétrie — mode d'emploi

Visualisation des statistiques **anonymes** du robot G1 : questions des visiteurs,
langues, sujets, satisfaction, appels d'assistance, alertes sécurité, santé du robot.

- **Fichier** : `dashboard_stats.py` (Streamlit)
- **Lanceur** : `scripts/lancer_dashboard.sh` (Linux/Mac/WSL) · `scripts/lancer_dashboard.bat` (Windows)

> ⚠️ Ne pas confondre avec `dashboard.py` (à la racine) qui est le **superviseur
> ESP32** (Flask, port 8888), un composant différent.

---

## 1. Lancer le dashboard

Tout est déjà installé **sur le robot** (streamlit + pandas + openpyxl, sur le
Python miniconda). Tu le lances sur le robot, tu le consultes depuis ton PC.

### Sur le robot (terminal SSH)

```bash
cd /home/unitree/g1_agent_interim
bash scripts/lancer_dashboard.sh
```

Le lanceur détecte qu'il n'y a pas d'écran → il démarre en mode **headless** sur le
réseau. **Garde ce terminal ouvert** tant que tu utilises le dashboard.

Pour qu'il reste actif même après avoir fermé le terminal :

```bash
cd /home/unitree/g1_agent_interim
nohup bash scripts/lancer_dashboard.sh > /tmp/streamlit.log 2>&1 &
```

### Puis, dans le navigateur de TON ordinateur

```
http://192.168.123.164:8501
```

(c'est l'IP du robot sur le LAN + le port 8501)

### Pour l'arrêter

Dans le terminal où il tourne : **Ctrl+C**. S'il a été lancé avec `nohup` :

```bash
pkill -f "streamlit run"
```

---

## 2. Charger les données

Dans la **barre latérale** (à gauche), 3 possibilités — dans l'ordre le plus simple :

1. **🔄 Récupérer depuis le robot** — va chercher les données à jour toutes seules
   (nécessite que `main.py` tourne, pour l'API `/api/export-logs`).
2. **Déposer un fichier** — glisse un `.xlsx`, `.csv` ou `.jsonl` déjà téléchargé.
3. **Automatique** — si le dashboard tourne sur le robot, il lit directement le
   fichier local même sans `main.py`.

> Le dashboard est **vide au début** : c'est normal, il se remplit avec les vraies
> conversations du robot.

---

## 3. Lire le dashboard

**En-tête — 8 chiffres clés** : Sessions (visites) · Note moyenne · Taux de
fallback IA · Latence moyenne · Questions posées · Appels d'assistance ·
Alertes sécurité · Zone la plus sollicitée.

**Sections** (graphiques + tableaux, même style) :

| Section | Ce qu'elle montre |
|---|---|
| 🧭 Sujets des questions | thèmes demandés (Vols, Hôtels, Transport…) |
| 🛠️ Services utilisés | outils réellement appelés par le robot |
| 🗣️ Langues des visiteurs | répartition des langues parlées |
| 📅 Activité par jour | fréquentation dans le temps |
| 🕐 Affluence par heure | heures de pointe (pour l'équipe) |
| ⭐ Satisfaction | notes + **satisfaction par zone** (repère un sujet mal noté) |
| ❓ Questions fréquentes | les formulations les plus courantes |
| 🆘 Derniers appels | appels à l'humain : motif, durée, répondu |
| 🚨 Alertes sécurité | chutes / feu détectés |

---

## 4. Exporter les données

En bas, section **⬇️ Exporter les données** :

- **📗 Excel lisible (.xlsx)** — pour LIRE dans Excel. Un **onglet par type**
  (Questions, Services, Avis, Appels, Alertes, Santé), colonnes en français, aucune
  cellule vide. S'ouvre directement (pas de souci de séparateur).
- **📄 Journal brut (.jsonl)** — format technique, sert à **recharger** les données
  dans le dashboard.

Les deux fichiers peuvent être **redéposés** dans la barre latérale (`.xlsx`, `.csv`
et `.jsonl` acceptés).

---

## 5. Alternative : lancer sur TON ordinateur

Si tu préfères ne pas faire tourner Streamlit sur le robot :

```bash
pip install streamlit pandas openpyxl
G1_ROBOT_URL="http://192.168.123.164:8000" bash scripts/lancer_dashboard.sh
# (Windows : scripts\lancer_dashboard.bat)
```

Avec écran, il ouvre ton navigateur automatiquement. Le bouton
« 🔄 Récupérer depuis le robot » télécharge les données via l'API du robot.

---

## 6. En cas de souci

| Symptôme | Cause | Solution |
|---|---|---|
| `…:8501` ne s'ouvre pas | le dashboard n'est pas lancé | relancer `bash scripts/lancer_dashboard.sh` |
| « Robot injoignable » sur 🔄 | `main.py` éteint | lancer `main.py`, ou lire le fichier local |
| Dashboard vide | pas encore de données | normal — se remplit avec l'usage réel |
| Robot sur le Wi-Fi | mauvaise IP | utiliser `http://192.168.0.128:8501` |

---

Pour ce qui est **collecté** (schémas d'événements, anonymat, fichiers de données) :
voir [STATISTIQUES.md](STATISTIQUES.md).
