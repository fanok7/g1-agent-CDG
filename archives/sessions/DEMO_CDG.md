# Démo agent CDG — Charly (Terminal 2F)

Préparation pour la démo du **10** (voix ET questions vols — pas de gestes/marche,
le robot est en état d'erreur moteur).

## Objectif
Le robot (Charly) répond **oralement** aux questions des passagers sur les vols au
terminal 2F de CDG, via les données **Airlabs** en temps réel, et dans la langue du
visiteur.

## Statut de validation (27/08)
- ✅ Données vols Airlabs : fonctionnelles en direct (arrivées, terminal 2F, statut vol).
- ✅ OpenAI Realtime (GPT mini) : connexion + réponse OK (clé valide, voix `marin`).
- ✅ Haut-parleur robot : fonctionnel (voix française OK via service DDS).
- ⚠️ Micro robot : **à valider** — la sélection auto (`find_microphone`, `robot/audio.py`)
  cherche un périphérique nommé "MIC"/"USB" ; sur ce G1 tout est sur la carte "APE" →
  risque qu'il retombe sur HDMI (sans micro). **À tester avant le jour J.**
- ⚠️ Lancement `main.py` : init matériel lourd (mains, RealSense, tablette, ESP32, Spotify)
  — à vérifier qu'il ne bloque pas en "voix seule" sur le robot en panne moteur.

## Scénario type (questions à poser au robot)
Chaîne attendue : micro → OpenAI Realtime (comprend) → tool Airlabs (données réelles) →
réponse vocale (max 3 phrases, noms de ville, porte/tapis si fournis).

1. **"Bonjour, quel est ton nom ?"** → Charly, agent d'accueil et de sûreté du Terminal 2F.
2. **"Quel est le prochain vol qui arrive ?"** → renvoie les prochaines arrivées (heure + ville).
3. **"Le vol AF7xxx de Lyon arrive à quelle heure ?"** → `get_flight_status` → heure arrivée
   (estimée), terminal 2F, tapis.
4. **"Quels vols arrivent au terminal 2F bientôt ?"** → `get_arrivals(terminal="2F")` →
   3-4 vols max : ville, heure, éventuellement retard.
5. **"Y a-t-il des vols en retard ?"** → `get_delayed_flights`.
6. **"Peux-tu me trouver un hôtel pas loin ?"** → outil hôtel (si on veut montrer plus).

> Règle système importante : toujours appeler le tool (jamais inventer un horaire),
> 3-4 vols max à l'oral, dires noms de ville (pas de codes), signaler porte "à confirmer".
> Radié sur la langue du visiteur (auto-détection).

## Checklist AVANT la démo (jour J)
1. **Brancher micro + haut-parleur du robot**, puis VÉRIFIER qu'un canal APE capture la voix :
   `python3.8 -c "import sounddevice as sd; print(sd.query_devices())"`
   — confirmer le bon index du micro ; si besoin corriger `find_microphone()` dans
   `robot/audio.py` (ou un device fixe).
2. Connexion réseau : `eth0` (190.168.123.x) pour DDS robot + accès internet pour
   OpenAI/Airlabs. « Test internet : curl https://airlabs.co/api/v9/flights?api_key=… »
3. Vérifier que `AIRLABS_API_KEY`, `OPENAI_API_KEY` sont chargés (`.env`).
4. Données vols "vivantes" le jour J (les heures changent) — aucun horaire codé en dur.

## Lancement
```bash
cd /home/unitree/unitree_sdk2_python && /usr/bin/python3.8 /home/unitree/g1_agent_interim/main.py --mode cdg
```
- Toujours lancer depuis `/home/unitree/unitree_sdk2_python` (SDK Unitree l'exige).
- Saisie alternative sans micro : taper la question + Entrée (text_input_loop).
- Arrêt : Ctrl+C. Si le port 8000 est occupé : `pkill -f g1_agent_interim/main.py`.
