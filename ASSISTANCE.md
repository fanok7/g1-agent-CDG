# 🆘 Appel d'assistance humaine — mode d'emploi complet

Mise en relation **vocale** entre le visiteur et un responsable humain, quand le robot
ne peut pas aider lui-même : fauteuil roulant, mobilité réduite, problème médical,
bagage perdu, ou simplement « je veux parler à quelqu'un ».

---

## 1. Vue d'ensemble

```text
 Visiteur                Robot G1                    Cloud                 Responsable
    │                       │                          │                        │
    │ 🆘 ou "je veux un     │                          │                        │
    │    responsable"       │                          │                        │
    ├──────────────────────▶│                          │                        │
    │                       │ écran d'AVERTISSEMENT    │                        │
    │◀──────────────────────┤ (rien n'est lancé)       │                        │
    │  « Oui, appeler »     │                          │                        │
    ├──────────────────────▶│ POST /call/start         │                        │
    │                       ├── crée un salon ────────▶│ Daily.co               │
    │                       ├── alerte ───────────────▶│ n8n → ntfy ───────────▶│ 🔔 lien
    │                       ├── /tmp/ai_paused (IA muette)                      │
    │                       ├── le robot rejoint le salon (micro + HP)          │
    │◀══════════ conversation audio ═══════════════════════════════════════════▶│
    │  « Raccrocher »       │ POST /call/end → salon supprimé → IA réveillée     │
```

**Quatre garanties de conception :**

| Garantie | Comment |
|----------|---------|
| Jamais d'appel non voulu | L'écran d'avertissement précède **toujours** la mise en relation |
| Jamais deux visiteurs dans le même salon | Nom de salon en `uuid`, salon détruit au raccrochage |
| Jamais de robot muet pour toujours | Watchdog : le verrou saute au bout de 10 min |
| Jamais de responsable seul en ligne | Le raccrochage **supprime le salon** → il est éjecté |

---

## 2. Les deux déclencheurs

**Bouton 🆘 (tactile)** — entrée `menuAssist` du menu d'accueil de la tablette. C'est la
seule entrée du menu qui ne passe pas par l'IA (`phrase = null` dans `IDLE_MENU`).

**Voix** — le LLM appelle le tool `appeler_assistance(motif, langue)` quand le visiteur
demande de l'aide humaine.

Les deux chemins convergent sur **le même écran d'avertissement**, puis sur le même
`POST /call/start`. Le tool vocal **ne lance pas l'appel** : il ne fait qu'afficher la
confirmation (`push_assist_confirm`) et rend la main. Le robot reste donc à l'écoute
tant que le visiteur n'a pas touché « Oui, appeler ».

> Le tool renvoie explicitement au LLM : *« N'appelle pas ce tool à nouveau ; attends sa
> confirmation »* — sans ça le modèle réessaie en boucle en voyant que rien ne se passe.

L'avertissement en attente est mémorisé côté serveur (`_pending_confirm`) : il survit à
un rechargement de la tablette, et disparaît si le visiteur touche « Annuler »
(`POST /assist/cancel`) ou après 90 s d'inactivité.

---

## 3. Le salon Daily

`_creer_salon()` crée un salon **neuf pour chaque appel**, nommé `g1-assist-<uuid12>`.

| Propriété | Valeur | Pourquoi |
|-----------|--------|----------|
| `privacy` | `public` | Le responsable rejoint d'un simple clic, sans compte |
| nom | `uuid` non devinable | Un salon public à nom prévisible serait écoutable par un tiers |
| `exp` | maintenant + 10 min + 5 min | Auto-destruction : pas de salon orphelin, facturation bornée |
| `eject_at_room_exp` | `true` | Personne ne reste connecté après expiration |
| `start_video_off` | `true` | Appel **audio seul** |
| `enable_prejoin_ui` | `false` | Aucun écran « rejoindre » — le robot ne peut pas cliquer |
| `enable_chat` / `enable_screenshare` | `false` | Inutiles, surface en moins |

**Repli sans clé API** : si `DAILY_ROOM_URL` est défini, on utilise ce salon fixe créé à
la main. Salon permanent et partagé — dépannage uniquement. `fermer_salon()` refuse de le
supprimer (il ne détruit que les salons dont le nom commence par `g1-assist-`), sinon le
premier raccrochage casserait tous les appels suivants.

**Pourquoi Daily et pas Jitsi** — le SDK Daily rejoint un salon en mode `callObject`,
donc **sans iframe**. Les serveurs Jitsi gratuits sont inutilisables ici : soit ils
refusent l'iframe (`X-Frame-Options`, cas de meet.ffmuc.net), soit ils exigent un compte
et parquent le robot dans un lobby (meet.jit.si depuis 2024).

---

## 4. L'alerte du responsable (n8n → ntfy)

Le robot poste sur `N8N_ASSIST_WEBHOOK_URL`
(défaut `https://n8n.i-interim.com/webhook/assistance`) :

```json
{"motif": "...", "langue": "fr", "salon": "g1-assist-...",
 "url": "https://<domaine>.daily.co/g1-assist-...",
 "horodatage": "2026-08-04 22:10:03", "source": "robot-G1"}
```

Le workflow (`scripts/n8n_assistance_workflow.json`) fait trois choses :

1. **Webhook** `POST /webhook/assistance`
2. **Formater le message** — traduit le code langue en toutes lettres, compose le texte
3. **Notifier (ntfy)** — `POST https://ntfy.sh/<topic>` avec `Priority: urgent`,
   `Tags: rotating_light` et surtout **`Click: {{ $json.url }}`** : toucher la
   notification ouvre directement le salon

> ⚠️ Le workflow versionné contient volontairement le placeholder `g1-assist-CHANGE-MOI`.
> **Ne jamais y committer le vrai topic** : un topic ntfy.sh n'est pas authentifié —
> quiconque le connaît peut lire les alertes *et* en publier de fausses. Le vrai topic
> ne vit que dans le workflow n8n ; il se transmet de la main à la main.
>
> **C'est lui qui décide QUI est prévenu** — pas la clé Daily. Pour le retrouver :
> l'ouvrir dans n8n, ou lire le champ `topic` de la réponse à un POST de test sur le
> webhook (voir § 11).

L'alerte est **best-effort** (timeout 6 s) : si n8n est injoignable, l'appel a lieu quand
même — le salon existe, et le lien est imprimé en clair dans les logs du robot :

```text
[ASSIST] 👉 REJOINDRE L'APPEL (ouvre ce lien pour parler au visiteur) :
[ASSIST]    https://interim-g1.daily.co/g1-assist-3f2a1b9c4d5e
```

C'est le filet de secours : en démo, on peut toujours rejoindre en copiant ce lien.

---

## 5. La sourdine de l'IA

Fichier verrou **`/tmp/ai_paused`**, qui contient le nom du salon.

Tant qu'il existe, `agent/events.py` :

- n'envoie plus le micro à OpenAI — il le redirige vers `call_audio.push_mic()`
- ne joue plus la voix du robot

Un seul flux ALSA est ouvert sur le micro : ouvrir un second échouerait. D'où la
redirection plutôt qu'une capture parallèle.

Le verrou est nettoyé **au démarrage de `main.py`** : s'il restait d'un crash, le robot
serait sourd et muet en permanence sans qu'on comprenne pourquoi.

**Si le robot est muet sans raison :** `rm /tmp/ai_paused`.

---

## 6. Le raccrochage

`POST /call/end` (bouton « Raccrocher » ou départ du salon) enchaîne :

1. `rm /tmp/ai_paused` → l'IA se réveille
2. `push_call_end()` → la tablette revient à l'écran normal + statistique enregistrée
3. `fermer_salon(room)` → `call_audio.arreter()` puis **DELETE du salon Daily**

L'étape 3 rend le raccrochage **symétrique** : sans elle, le responsable resterait seul
dans un salon ouvert sans savoir que l'échange est fini. Un `404` sur le DELETE est
normal (salon déjà expiré).

---

## 7. Le watchdog

Si personne ne raccroche (visiteur parti, tablette plantée), l'IA resterait muette
indéfiniment. Un thread lève le verrou après `ASSIST_CALL_TIMEOUT` (**600 s** par défaut).

Il vérifie que `/tmp/ai_paused` contient **toujours le même salon** avant de couper :
sinon un appel expiré couperait la sourdine d'un appel plus récent.

---

## 8. Où passe la voix

Deux modes, commutateur `ASSIST_AUDIO` — détails au **§ 13**.

En mode `robot`, la tablette n'affiche que l'écran d'appel. En mode `tablette`, elle
rejoint elle-même le salon via daily-js chargé depuis unpkg : **il lui faut Internet**,
et l'autorisation micro de la WebView. `probeMic()` teste `getUserMedia` **avant**
l'appel pour nommer la panne précisément dans `/tmp/tablet_client.log`.

En mode `callObject`, daily-js ne fabrique aucune interface — il ne joue donc pas tout
seul la voix des participants. `attacherAudio()` branche chaque piste distante sur un
`<audio>` ; sans ça l'appel se connecte mais reste **muet**.

---

## 9. Configuration

Toutes dans `~/.env` ou le `.env` du repo (le repo gagne en cas de doublon) :

| Variable | Défaut | Rôle |
|----------|--------|------|
| `DAILY_API_KEY` | *(vide)* | **Requise.** Crée un salon neuf par appel |
| `DAILY_ROOM_URL` | *(vide)* | Repli : salon fixe, sans clé API |
| `N8N_ASSIST_WEBHOOK_URL` | `https://n8n.i-interim.com/webhook/assistance` | Alerte du responsable |
| `ASSIST_AUDIO` | `robot` | `robot` ou `tablette` |
| `ASSIST_CALL_TIMEOUT` | `600` | Secondes avant la levée automatique du verrou |
| `ASSIST_MIC_GAIN` | `6.0` | Gain du micro robot vers le salon (≈ +15,6 dB) |

⚠️ Ces variables sont lues **à l'import du module**, pas à chaque appel : modifier
`~/.env` n'a aucun effet tant que `main.py` n'est pas relancé.

---

## 10. Changer de compte Daily

Le domaine n'est codé en dur **nulle part** : l'URL du salon vient de la réponse de l'API
(`r.json()["url"]`), et `call_audio.demarrer(url)` la reçoit en paramètre. Le SDK JS de la
tablette vient d'unpkg, générique. Changer de compte = changer **une seule ligne**.

```bash
# 1. Vérifier la clé AVANT de l'installer — affiche le domaine auquel elle donne accès
curl -s https://api.daily.co/v1/ -H "Authorization: Bearer <NOUVELLE_CLE>" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['domain_name'])"

# 2. La poser dans ~/.env (sans la faire apparaître dans l'historique du shell)
read -rs -p "Clé Daily : " K; echo
python3 - "$K" <<'PY'
import sys, pathlib
p = pathlib.Path.home() / ".env"
lignes = [l for l in p.read_text().splitlines() if not l.startswith("DAILY_API_KEY=")]
lignes.append("DAILY_API_KEY=" + sys.argv[1])
p.write_text("\n".join(l for l in lignes if l.strip()) + "\n")
PY
unset K

# 3. RELANCER main.py — sinon l'ancienne clé reste en mémoire (voir § 9)

# 4. Test réel : crée puis supprime un salon, et montre le nouveau domaine
K=$(grep '^DAILY_API_KEY=' ~/.env | cut -d= -f2-)
curl -s -X POST https://api.daily.co/v1/rooms -H "Authorization: Bearer $K" \
  -H "Content-Type: application/json" -d '{"name":"g1-assist-testcle"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('url'))"
curl -s -o /dev/null -w "DELETE %{http_code}\n" -X DELETE \
  https://api.daily.co/v1/rooms/g1-assist-testcle -H "Authorization: Bearer $K"
```

**Ce que ça ne change PAS : qui est prévenu.** La notification part vers le topic ntfy
configuré dans n8n (§ 4). Le nouveau responsable doit s'abonner à ce topic, sinon il
possède les salons mais n'apprend jamais qu'on l'appelle.

**Historique :** domaine `i-interimcdg` → **`interim-g1`** le 2026-08-04.

---

## 11. Dépannage

| Symptôme | Cause probable | Fix |
|----------|----------------|-----|
| Rien ne se passe après « Oui, appeler » | Clé Daily invalide | Logs `[ASSIST] Création salon Daily refusée (401)` |
| `Création salon refusée (402)` | Quota du compte Daily atteint | Vérifier le plan sur dashboard.daily.co |
| Appel ouvert mais responsable jamais prévenu | Webhook n8n ou topic ntfy | `[ASSIST] Webhook n8n injoignable` ; sinon vérifier le topic dans n8n |
| Le responsable n'a rien reçu mais l'appel marche | Alerte best-effort : l'appel continue sans elle | Copier le lien imprimé dans les logs `[ASSIST]` |
| Le robot reste muet après l'appel | Verrou resté | `rm /tmp/ai_paused` |
| Appel connecté mais muet (mode tablette) | Pistes audio non attachées / micro refusé | `tail -f /tmp/tablet_client.log` → `ERREUR MICRO:` ou `daily-js injoignable` |
| Appel muet (mode robot) | `call_audio.demarrer()` a échoué | Le repli tablette est automatique — chercher `→ repli sur la tablette` |
| On ne peut pas couper la parole au responsable | **Normal** : half-duplex assumé (§ 13) | Passer `ASSIST_AUDIO=tablette` si le full duplex est indispensable |
| Le responsable reste seul après le raccrochage | DELETE du salon en échec | Logs `[ASSIST] Fermeture salon refusée` |
| L'écran de confirmation revient tout seul | `_pending_confirm` mémorisé, tablette rechargée | Toucher « Annuler » ou attendre 90 s |
| Aucune trace de la demande vocale | Le tool n'a pas été appelé | `cat /tmp/assist_debug.log` (motif + nb de tablettes connectées) |

---

## 12. Fichiers

| Chemin | Rôle |
|--------|------|
| `tools/assistance_tool.py` | Salon, alerte n8n, verrou, watchdog, tool vocal |
| `robot/call_audio.py` | Audio par le robot (daily-python, half-duplex) |
| `tablet_server/server.py` | Routes `/call/start`, `/call/end`, `/assist/cancel` + `push_call*` |
| `tablet_server/templates/index.html` | Écrans d'avertissement et d'appel (~ligne 758) |
| `scripts/n8n_assistance_workflow.json` | Workflow n8n à importer |
| `/tmp/ai_paused` | Verrou de sourdine (contient le nom du salon) |
| `/tmp/assist_debug.log` | Trace des demandes vocales |
| `/tmp/tablet_client.log` | Journal du navigateur de la tablette |
| `backups/index.html.tablette-audio` | Version 100 % tablette (avant § 13) |

---

## 13. Audio par le robot (ASSIST_AUDIO)

Depuis le 2026-07-23, l'appel passe par le **micro et le haut-parleur du robot**
(`daily-python` sur le Jetson) au lieu de ceux de la tablette.

Motif : SNR mesuré à **34 dB** sur le micro Cubilux à 1 m (bruit de fond
−80 dBFS, ventilateurs à peine présents), alors que le micro de la tablette
captait le souffle des ventilos à 30 cm.

**Commutateur** — dans `~/.env` :

| Valeur | Audio | Duplex | Usage |
|---|---|---|---|
| `ASSIST_AUDIO=robot` (défaut) | micro + HP du robot | half-duplex | meilleure prise de son |
| `ASSIST_AUDIO=tablette` | navigateur de la tablette | full duplex (AEC navigateur) | **secours** |

Le repli est aussi **automatique** : si `call_audio.demarrer()` échoue, l'appel
bascule sur la tablette pour cet appel-là (champ `audio` du payload SSE).

Points d'implémentation :

- `robot/call_audio.py` — périphériques virtuels Daily (`g1-mic`, `g1-spk`) ; le micro est
  alimenté par `push_mic()` depuis `agent/events.py` (réutilise le flux sounddevice
  existant : ouvrir un 2e flux ALSA sur le même micro échouerait).
- **Half-duplex obligatoire** : HP et micro du robot sont à quelques cm sans AEC
  matérielle. Tant que le responsable parle (`niveau > _SEUIL_VOIX` = 300, hangover
  350 ms), le micro n'émet pas. Conséquence assumée : on ne peut pas se couper
  la parole.
- Le seuil ne filtre **que** le micro, jamais la lecture — conditionner la
  lecture au niveau hacherait les passages doux.
- `on_participant_joined` → `push_call_status("connected")` : l'écran passe de
  « En attente d'un responsable… » à « Communication établie ».

Sauvegarde de la version 100 % tablette : `backups/index.html.tablette-audio` et
`backups/assistance_tool.py.tablette-audio`.

---

## 14. Statistiques anonymes

L'appel d'assistance alimente la télémétrie : un `log_call` (motif, durée,
répondu) est enregistré au raccrochage. `answered` est une heuristique honnête : un
échange d'au moins **5 s** compte comme réellement décroché.

La collecte et la visualisation sont décrites ailleurs :

- **[STATISTIQUES.md](STATISTIQUES.md)** — ce qui est mesuré, anonymat, fichiers.
- **[DASHBOARD.md](DASHBOARD.md)** — le dashboard Streamlit (lancement, lecture, export).

L'ancien dashboard Chart.js (`/admin`) et le rapport HTML autonome ont été retirés
au profit du dashboard Streamlit unique.
