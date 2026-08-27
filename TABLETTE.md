# 📱 Tablette du robot G1 — mode d'emploi complet

Tout ce qui concerne l'**écran tactile** porté par le robot : le matériel, la liaison
USB, le serveur qui l'alimente, l'interface, les pannes courantes et comment la faire
évoluer.

> **En une phrase :** la tablette n'a **pas de cerveau à elle**. C'est un simple
> navigateur en kiosque qui affiche une page servie par le robot. Le micro, le
> haut-parleur et l'IA restent **entièrement sur le Jetson**.

---

## 1. Vue d'ensemble

```text
┌─ Tablette Android (KT107) ─────────┐        ┌─ Jetson Orin NX ──────────────────┐
│ Fully Kiosk Browser                │        │ main.py                           │
│   http://localhost:8000            │◀─USB──▶│  └─ thread daemon : uvicorn       │
│   • affiche : veille/texte/QR/plan │  adb   │      tablet_server/server.py      │
│   • pousse : taps, notes, appels   │reverse │      0.0.0.0:8000 (FastAPI + SSE) │
└────────────────────────────────────┘        └───────────────────────────────────┘
        ▲ Server-Sent Events (le serveur pousse)  │
        └─ POST /respond, /api/rating, /call/* ───┘ (la tablette répond)
```

| Élément | Valeur |
|---------|--------|
| Modèle | **XMOBILE KT107**, Android 13 (ROM AOSP standard, chipset MediaTek) |
| Série adb | `0123456789ABCDEF` (visible dans `adb devices`) |
| Navigateur | **Fully Kiosk Browser** `de.ozerov.fully` v1.61-play |
| URL affichée | `http://localhost:8000` (⚠️ **localhost**, pas l'IP du robot) |
| Liaison | **USB + `adb reverse tcp:8000 tcp:8000`** |
| Serveur | `tablet_server/server.py`, démarré **par `main.py`** (thread daemon) |

### Pourquoi USB et pas le Wi-Fi

Le Wi-Fi des sites clients isole les clients entre eux (*client isolation*) : la tablette
ne peut pas joindre `192.168.123.164:8000` même connectée au même réseau. L'USB rend la
liaison **indépendante du réseau du site** — c'est le seul mode fiable en démo.
L'USB tethering a aussi été essayé et écarté (instable au reboot).

> La tablette peut **en plus** être sur un Wi-Fi avec Internet : c'est nécessaire
> uniquement pour l'**iframe Google Maps** de l'écran « plan » (`embed_url`). Sans
> Internet côté tablette, le plan retombe sur l'image statique — le reste fonctionne.

---

## 2. La liaison USB (`adb reverse`)

`adb reverse` fait écouter le port 8000 **sur la tablette** et redirige tout vers le port
8000 **du Jetson** à travers le câble. D'où l'URL `localhost` côté tablette.

### Persistance : le service systemd

Au moindre reboot (robot ou tablette) et à chaque débranchement, le tunnel saute. Un
service le rétablit automatiquement :

- `scripts/adb_reverse_tablet.sh` — boucle infinie, vérifie et recrée le tunnel **toutes
  les 5 s**
- `scripts/adb-reverse-tablet.service` — le lance au boot, `Restart=always`

```bash
# état / logs
systemctl status adb-reverse-tablet
journalctl -u adb-reverse-tablet -f

# (ré)installation, si le service manque
sudo cp scripts/adb-reverse-tablet.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adb-reverse-tablet
```

### Vérifier la liaison en 3 commandes

```bash
adb devices                 # doit afficher : 0123456789ABCDEF   device
adb reverse --list          # doit afficher : UsbFfs tcp:8000 tcp:8000
curl -s localhost:8000/ping # doit répondre : {"ok":true}
```

Ces trois lignes couvrent 90 % des pannes : **tablette vue**, **tunnel ouvert**,
**serveur vivant**. Si les deux premières passent et pas la troisième, le problème n'est
pas la tablette — c'est que `main.py` ne tourne pas.

### Réglages déjà faits sur la tablette (à refaire sur une tablette neuve)

1. **Options développeur** → *Débogage USB* activé
2. À la 1re connexion, accepter la fenêtre **« Autoriser le débogage USB ? »** → cocher
   *Toujours autoriser depuis cet ordinateur* (la clé est dans `/home/unitree/.android/adbkey`)
3. **Fully Kiosk** : *Start URL* = `http://localhost:8000`, *Launch on Boot* activé,
   permission `RECEIVE_BOOT_COMPLETED` accordée
4. **Batterie** : Fully exempté de l'optimisation batterie (sinon Android le gèle)
5. Aucun gestionnaire d'autostart constructeur à configurer (ROM AOSP standard)

### Pièges connus

| Piège | Symptôme | Ce qu'il faut savoir |
|-------|----------|----------------------|
| Câble « charge seule » | `adb devices` vide | Un câble USB sans paires data ne remontera jamais la tablette |
| Autorisation RSA non accordée | `unauthorized` dans `adb devices` | Débrancher/rebrancher, accepter la fenêtre sur la tablette, cocher « toujours » |
| Deux serveurs adb | Tunnel qui saute sans raison | Un `adb` lancé par un autre utilisateur (root, conda) tient le device. `adb kill-server` puis laisser le service reprendre |
| Port 5000 | Page blanche | **C'est 8000**, pas 5000 — erreur historique du projet |
| Reboot du robot | Tablette figée sur l'ancienne page | Le service rétablit le tunnel, mais tant que `main.py` n'est pas lancé le port 8000 est muet |

---

## 3. Le serveur — `tablet_server/server.py`

FastAPI + **Server-Sent Events**. La page n'est **jamais rechargée** : le serveur pousse
les changements dans un flux ouvert. Démarré par `main.py` dans un thread daemon
(`_start_tablet_server`, `host=0.0.0.0`, `port=8000` — constante `TABLET_PORT`).

### Ce que le robot pousse vers la tablette

Chaque message SSE est une enveloppe JSON à **une seule clé** — le JS distingue dessus.

| Fonction | Enveloppe | Appelée par | Effet |
|----------|-----------|-------------|-------|
| `push_display(payload)` | `{"display": …}` | `tools/tablet_tools.py` | Contenu de l'écran : `idle` / `text` / `qr` / `plan` |
| `push_status(s)` | `{"status": …}` | `agent/events.py` | Pastille d'état : `ecoute` / `reflechit` / `parle` |
| `push_chat(role, txt)` | `{"chat": …}` | `agent/events.py` | Bulle de conversation (`user` / `assistant`) |
| `push_choices(list)` | `{"choices": […]}` | tool `proposer_choix` | Boutons tactiles |
| `push_lang(code)` | `{"lang": …}` | tool `definir_langue_ecran` | Langue de l'interface (ignorée si retour au `fr` alors que l'écran est déjà ailleurs — anti-dérive du modèle) |
| `push_lang_user(code)` | `{"lang_user": …}` | `agent/events.py` | Langue **entendue dans la voix** — canal prioritaire, **toujours** appliquée |
| `push_assist_confirm(i)` | `{"assist_confirm": …}` | `tools/assistance_tool.py` | Écran d'avertissement avant un appel demandé **à la voix** |
| `push_call(info)` | `{"call": …}` | `tools/assistance_tool.py` | Bascule en mode appel |
| `push_call_status(s)` | `{"call_status": …}` | `robot/call_audio.py` | `waiting` → `connected` |
| `push_call_end()` | `{"call_end": true}` | serveur | Fin d'appel, retour à l'écran normal |

État courant mémorisé côté serveur (`_current_state`, `_current_status`, `_current_lang`,
`_current_call`, `_chat_history`, …) et **rejoué au (re)chargement** de la page : un reload
de la tablette ne perd ni la conversation ni un appel en cours.

### Ce que la tablette envoie au robot

| Route | Méthode | Rôle |
|-------|---------|------|
| `/` | GET | La page (`templates/index.html`) |
| `/events` | GET | Le flux SSE |
| `/ping` | GET | Sonde de vie — sert à détecter un `main.py` mort |
| `/respond` | POST | Tap sur un bouton → injecté dans la conversation Realtime **comme si c'était dit au micro** (via `agent.text_input.input_queue`) |
| `/api/rating` | POST | Note 1–5 → CSV `statistiques_robot.csv` + `analytics.log_rating` |
| `/call/start` | POST | **Seul** déclencheur réel d'un appel d'assistance (toujours après confirmation) |
| `/call/end` | POST | Raccroche : retire `/tmp/ai_paused`, ferme le salon |
| `/assist/cancel` | POST | Le visiteur a refusé l'appel → oublie la demande en attente |
| `/reset` | POST | Nouvelle session : vide l'historique côté serveur |
| `/clientlog` | POST | **Journal du navigateur** → `/tmp/tablet_client.log` + console |
| `/api/export-logs` | GET | Télécharge `analytics_events.jsonl` |

> `/clientlog` est précieux : la tablette est en kiosque, **sans console accessible**.
> Une erreur JS (micro refusé, Daily injoignable…) n'apparaît nulle part ailleurs.
> `tail -f /tmp/tablet_client.log` sur le robot pendant un test.

---

## 4. L'interface — `tablet_server/templates/index.html`

Un seul fichier (~1240 lignes) : HTML + CSS + JS, sans build ni dépendance externe.
Repères par commentaire de section (`// ── … ──`).

### Les trois vues

| Vue | Quand | Contenu |
|-----|-------|---------|
| **Veille / menu** | Au repos | Titre d'accueil + menu tactile + drapeaux de langue |
| **Conversation** | Pendant l'échange | Historique complet des bulles + affichage texte/QR/plan |
| **Appel** | Assistance humaine | Écran « en attente » puis « communication établie » |

### Menu d'accueil (`IDLE_MENU`, ligne ~627)

Chaque entrée = `[clé i18n, icône, phrase envoyée au robot]`. Un tap envoie la phrase
**comme si le visiteur l'avait dite** — c'est l'IA qui répond, pas un chemin codé en dur.
L'entrée 🆘 est la seule exception (`phrase = null`) : elle déclenche l'assistance
directement, sans passer par l'IA.

Les drapeaux (`IDLE_LANGS`) font deux choses d'un coup : basculer l'interface **et** dire
au robot de parler cette langue.

### Internationalisation (ligne ~513)

8 langues : `fr en es de it ar zh ja`, repli sur `en` pour toute autre. Le dictionnaire
`I18N` porte les libellés ; les textes d'appel sont ajoutés ensuite par `Object.assign`.

**Deux canaux de langue, priorités différentes** — c'est subtil :
- `lang` (tool `definir_langue_ecran`) : un retour au `fr` est **ignoré** si l'écran est
  déjà dans une autre langue → protège de la dérive du modèle
- `lang_user` (langue réellement entendue) : **toujours** appliqué, retour au `fr` compris

### Retour en veille automatique

`IDLE_MS = 90000` (ligne ~672) : **90 s sans aucune interaction** (ni voix ni tap) →
`fullReset()` efface la conversation, remet le `fr`, revient à l'accueil et appelle
`POST /reset`. Objectif : confidentialité, le visiteur suivant ne voit pas la session
précédente. **Jamais déclenché pendant un appel** (`viewMode === 'call'`).

### Résilience (le point le plus important en démo)

Deux garde-fous indépendants, parce qu'à travers le tunnel USB une connexion TCP peut
rester **semi-ouverte** — le SSE ne voit alors pas la mort du serveur :

| Garde-fou | Seuil | Effet |
|-----------|-------|-------|
| Reconnexion SSE (~ligne 1220) | 6 échecs | `location.reload()` |
| **Sonde active `/ping`** (~ligne 1226) | 3 échecs (~12 s), toutes les 4 s | `location.reload()` |

Conséquence voulue : si `main.py` s'arrête, la tablette affiche « page introuvable »
plutôt qu'une interface figée qui **fait croire** que le robot fonctionne. Dès que le
serveur revient, la page se recharge toute seule.

Autre garde-fou : l'overlay « Réflexion » se retire de force après 15 s (`_loadingTimer`).

### Notation

Barre d'étoiles en bas → `POST /api/rating` → ligne dans
`tablet_server/statistiques_robot.csv` (`Horodatage, Note, Question, Reponse`) **et**
`analytics.log_rating`. Restitution dans le dashboard Streamlit — voir
[DASHBOARD.md](DASHBOARD.md) et [STATISTIQUES.md](STATISTIQUES.md).

---

## 5. Les tools qui pilotent l'écran

Enregistrés par `import tools.tablet_tools` dans `main.py`. Le LLM les appelle comme
n'importe quel autre tool.

| Tool | Effet à l'écran | Payload `display` |
|------|-----------------|-------------------|
| `proposer_choix(options)` | 2 à 4 boutons tactiles | *(canal `choices`)* |
| `afficher_texte_ecran(titre, contenu_texte)` | Texte + QR de récupération | `{type:"text", titre, contenu, qr_url, qr_mode}` |
| `afficher_qr_ecran(...)` | QR plein écran | `{type:"qr", titre, image_url}` |
| `afficher_plan_ecran(...)` | Itinéraire : iframe Maps + image + QR | `{type:"plan", titre, image_url, qr_url, embed_url}` |
| `definir_langue_ecran(langue)` | Bascule l'interface | *(canal `lang`)* |

Les fichiers générés vivent dans `tablet_server/static/` : `qrcodes/`, `plans/`, `notes/`.
Ils **s'accumulent sans nettoyage** — voir § Améliorations.

Le QR d'un texte long ne contient pas le texte mais un **lien LAN** vers une note HTML
servie par le robot (`_save_text_retrieval_qr`) : un QR direct serait illisible. Ce lien
n'est joignable que depuis le réseau du robot.

---

## 6. Démarrage

Rien ne s'affiche tant que **`main.py` ne tourne pas** — c'est lui qui porte le serveur du
port 8000. Trois modes possibles ; **aujourd'hui le robot est en mode manuel.**

| Mode | Port 8000 tenu par | État actuel |
|------|--------------------|-------------|
| **Manuel** | `main.py` uniquement | ✅ **en service** |
| Launcher (page « Démarrer ») | `launcher_server.py` puis `main.py` | `g1-launcher` installé mais **désactivé** |
| Auto-start | `main.py` sous systemd | `g1-main.service` **pas installé** (fichier présent dans `scripts/`) |

Les commandes des trois modes sont dans le [README](README.md#lancement).

⚠️ **Les modes s'excluent** : deux d'entre eux veulent le port 8000. Si `g1-launcher` et
`g1-main` sont actifs en même temps, l'un des deux crashe en boucle sur
`Address already in use`. `scripts/installer_autostart.sh` désactive le launcher
justement pour ça.

---

## 7. Dépannage

| Symptôme | Cause probable | Fix |
|----------|----------------|-----|
| Écran blanc / « page introuvable » | `main.py` ne tourne pas | Lancer `main.py` ; vérifier `curl localhost:8000/ping` |
| « page introuvable » alors que `main.py` tourne | Tunnel adb tombé | `adb reverse --list` ; `systemctl restart adb-reverse-tablet` |
| `adb devices` vide | Câble charge-seule, ou débogage USB coupé | Changer de câble ; réactiver le débogage USB |
| `adb devices` → `unauthorized` | Autorisation RSA non accordée | Rebrancher, accepter sur la tablette, cocher « toujours autoriser » |
| Tunnel qui saute sans arrêt | Un 2e serveur adb (root/conda) | `adb kill-server`, laisser le service reprendre |
| Interface figée, robot qui parle | Flux SSE mort mais TCP semi-ouvert | Attendre ~12 s : la sonde `/ping` recharge seule. Sinon `POST /reset` |
| Boutons sans effet | `agent.text_input` non branché | Logs `[TABLETTE]` ; vérifier que `text_input_loop` tourne dans `main.py` |
| L'écran ne change pas de langue | Le tool a renvoyé `fr` alors que l'écran est ailleurs → **ignoré volontairement** | Comportement voulu (anti-dérive). Le canal voix `lang_user` force, lui |
| Le plan s'affiche sans carte | Pas d'Internet **côté tablette** | Connecter la tablette au Wi-Fi ; sinon repli image statique |
| Écran de veille qui revient trop vite | `IDLE_MS` = 90 s | Augmenter la constante dans `index.html` |
| Aucune erreur JS visible | Kiosque sans console | `tail -f /tmp/tablet_client.log` (route `/clientlog`) |
| L'IA ne parle plus après un appel | Verrou `/tmp/ai_paused` resté | `rm /tmp/ai_paused` (nettoyé aussi au démarrage de `main.py`) |
| Appel jamais lancé après « Oui » | Webhook n8n / clé Daily | `cat /tmp/assist_debug.log` ; `DAILY_API_KEY` dans `~/.env` |
| Fully ne démarre pas au boot | Optimisation batterie / Launch on Boot | Revoir les 5 réglages du § 2 |
| Adresse déjà utilisée au lancement | `g1-launcher` ou un ancien `main.py` tient le port | `sudo systemctl stop g1-launcher` ; `pkill -f g1_agent_interim/main.py` |

**Réflexe de diagnostic** — dans l'ordre, ça isole la panne en 30 secondes :

```bash
adb devices && adb reverse --list        # 1. le câble et le tunnel
curl -s localhost:8000/ping              # 2. le serveur (donc main.py)
tail -f /tmp/tablet_client.log           # 3. le navigateur de la tablette
```

---

## 8. Faire évoluer la tablette

### Ajouter une langue à l'interface
1. Ajouter l'entrée dans `I18N` (`index.html` ~ligne 513) — copier un bloc existant
2. Ajouter les libellés d'appel via `Object.assign(I18N.xx, {callWait…})`
3. Ajouter le code dans `_LANG_CODES` de `tools/tablet_tools.py` (+ alias dans
   `_LANG_ALIASES`)
4. Optionnel : un drapeau dans `IDLE_LANGS`

Sans l'étape 3, le tool normalisera la langue en `en`.

### Ajouter une entrée au menu d'accueil
Une ligne dans `IDLE_MENU` : `['menuXxx', '🎫', 'La phrase envoyée au robot']`, plus la
clé `menuXxx` dans **chaque** langue de `I18N`. Aucun code backend : la phrase part par
`/respond` et l'IA décide de la suite.

### Ajouter un type d'affichage
1. Un tool dans `tools/tablet_tools.py` qui appelle `push_display({"type": "montype", …})`
2. Un bloc de rendu dans `index.html` — la chaîne `if (state.type === 'text') … else if
   … 'qr' … 'plan'` (~ligne 1095) : y ajouter une branche
3. Le CSS de la vue

Le serveur n'a **rien** à savoir du nouveau type : il relaie le payload tel quel.

### Changer le délai de retour en veille
`IDLE_MS` dans `index.html` (~ligne 672). 90 s convient à un stand ; monter à 3–5 min
pour un usage bureau où les silences sont plus longs.

### Changer le port
Trois endroits **à changer ensemble** : `TABLET_PORT` (`main.py`), `PORT`
(`scripts/adb_reverse_tablet.sh`), `_TABLET_PORT` (`tools/tablet_tools.py`) — plus la
Start URL de Fully et `PORT` dans `launcher_server.py`.

### Nettoyer les fichiers générés
`tablet_server/static/{qrcodes,plans,notes}/` grossissent à chaque QR, plan et note, sans
purge. Sur un robot qui tourne des mois, ça finit par peser. Piste : purger au démarrage
comme `main.py` le fait déjà pour `vision/Screenshot/`, ou garder les N derniers.

### Passer la tablette en Wi-Fi
Techniquement immédiat — le serveur écoute déjà sur `0.0.0.0` : il suffit de pointer Fully
sur `http://192.168.123.164:8000`. **Mais** ça ne marche que sur un réseau sans isolation
client, et casse la portabilité en démo. L'USB reste le mode de référence.

### Pistes plus ambitieuses
- **Autonomie d'affichage** : la tablette ne montre rien tant que `main.py` n'est pas
  lancé. Le launcher (§ 6) répond en partie ; activer `g1-main.service` réglerait le
  sujet définitivement
- **Deuxième écran** : `_open_tablet_display()` (`main.py`) ouvre déjà Chromium en kiosque
  sur un écran branché au Jetson. Les deux affichages peuvent coexister — le SSE est
  multi-abonnés
- **Retour tactile hors conversation** : un clavier virtuel pour les environnements très
  bruyants (`/respond` accepte déjà n'importe quel texte)

---

## 9. Fichiers à connaître

| Chemin | Rôle |
|--------|------|
| `tablet_server/server.py` | Serveur FastAPI + SSE, toutes les routes et `push_*` |
| `tablet_server/templates/index.html` | Toute l'interface (HTML+CSS+JS) |
| `tablet_server/static/{qrcodes,plans,notes}/` | Fichiers générés par les tools |
| `tablet_server/statistiques_robot.csv` | Historique des notes |
| `tablet_server/analytics_events.jsonl` | Journal télémétrie anonyme |
| `tools/tablet_tools.py` | Les 5 tools d'écran |
| `tools/assistance_tool.py` | Appel d'assistance (voir [ASSISTANCE.md](ASSISTANCE.md)) |
| `scripts/adb_reverse_tablet.sh` + `.service` | Persistance du tunnel USB |
| `launcher_server.py` + `scripts/g1-launcher.service` | Page « Démarrer » |
| `scripts/g1-main.service` + `installer_autostart.sh` | Mode auto-start |
| `/tmp/tablet_client.log` | Journal du navigateur de la tablette |
| `/tmp/assist_debug.log` | Trace des demandes d'assistance |
| `/tmp/ai_paused` | Verrou : IA muette pendant un appel |
