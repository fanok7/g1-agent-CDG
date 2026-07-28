
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
- `robot/call_audio.py` — périphériques virtuels Daily ; le micro est alimenté
  par `push_mic()` depuis `agent/events.py` (réutilise le flux sounddevice
  existant : ouvrir un 2e flux ALSA sur le même micro échouerait).
- **Half-duplex obligatoire** : HP et micro du robot sont à quelques cm sans AEC
  matérielle. Tant que le responsable parle (`niveau > _SEUIL_VOIX`, hangover
  350 ms), le micro n'émet pas. Conséquence assumée : on ne peut pas se couper
  la parole.
- Le seuil ne filtre **que** le micro, jamais la lecture — conditionner la
  lecture au niveau hacherait les passages doux.

Sauvegarde de la version 100 % tablette : `backups/index.html.tablette-audio` et
`backups/assistance_tool.py.tablette-audio`.

---

## 14. Statistiques anonymes

L'appel d'assistance alimente la télémétrie : un `log_call` (motif, durée,
répondu) est enregistré au raccrochage. La collecte et la visualisation sont
décrites ailleurs :

- **[STATISTIQUES.md](STATISTIQUES.md)** — ce qui est mesuré, anonymat, fichiers.
- **[DASHBOARD.md](DASHBOARD.md)** — le dashboard Streamlit (lancement, lecture, export).

L'ancien dashboard Chart.js (`/admin`) et le rapport HTML autonome ont été retirés
au profit du dashboard Streamlit unique.
