# led_manager.py — Gestionnaire centralisé des LEDs RGB du robot G1
# Gère les priorités entre les états GPT et les événements vision.
#
# PRIORITÉS (du plus urgent au moins urgent) :
#   3 — Urgence vision    : feu, chute          (commenté, à activer plus tard)
#   2 — Vision visage     : reconnu, inconnu
#   1 — États GPT         : écoute, réfléchit, parle
#   0 — Veille / idle     : blanc doux
#
# Usage :
#   from robot.led_manager import led
#   led.set_gpt("ecoute")
#   led.set_vision("visage_reconnu")
#   led.clear_vision()

import threading
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Table des couleurs (R, G, B) — 0-255
# ---------------------------------------------------------------------------
COULEURS = {
    # États GPT
    "ecoute":          (0,   0,   255),   # 🔵 Bleu — attente passive
    "entend":          (255, 140, 0),     # 🟠 Orange — détecte ta voix en ce moment (VAD actif)
    "reflechit":       (0,   255, 0),     # 🟢 Vert — traite ta phrase, prépare la réponse
    "parle":           (255, 0,   0),     # 🔴 Rouge

    # Vision — visages
    "visage_reconnu":  (128, 0,   255),   # 💜 Violet
    "visage_inconnu":  (255, 0,   180),   # 🩷 Magenta

    # Vision — urgences (commentées, à activer quand prêt)
    # "feu":           (255, 0,   0),     # 🔴 Rouge fixe
    # "chute":         (255, 0,   0),     # 🔴 Rouge fixe (même couleur que feu)

    # Veille
    "idle":            (20,  20,  20),    # ⚪ Blanc doux
}

# ---------------------------------------------------------------------------
# Niveaux de priorité par état
# ---------------------------------------------------------------------------
PRIORITES = {
    # "feu":            3,   # Urgence vision — commenté
    # "chute":          3,   # Urgence vision — commenté
    "visage_reconnu":  2,
    "visage_inconnu":  2,
    "ecoute":          1,
    "entend":          1,
    "reflechit":       1,
    "parle":           1,
    "idle":            0,
}


class LedManager:
    """
    Gestionnaire centralisé des LEDs RGB.

    Deux canaux indépendants :
      - _etat_gpt    : état courant de l'agent GPT
      - _etat_vision : état courant de la vision (None si rien)

    La LED affichée est toujours celle du canal de plus haute priorité.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._etat_gpt = "idle"       # état GPT courant
        self._etat_vision = None      # état vision courant (None = inactif)
        self._audio_client = None     # injecté via init()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def init(self, audio_client):
        """
        À appeler après hardware.init().
        Passe l'audio_client qui expose LedControl(R, G, B).

        Exemple :
            from robot.hardware import get_audio_client
            from robot.led_manager import led
            led.init(get_audio_client())
        """
        self._audio_client = audio_client
        self._appliquer()
        logger.info("[LED] LedManager initialisé — état idle")

    # ------------------------------------------------------------------
    # API publique — états GPT
    # ------------------------------------------------------------------

    def set_gpt(self, etat: str):
        """
        Définit l'état GPT courant.
        Valeurs acceptées : 'ecoute', 'reflechit', 'parle', 'idle'
        """
        if etat not in COULEURS:
            logger.warning(f"[LED] État GPT inconnu : {etat}")
            return
        with self._lock:
            self._etat_gpt = etat
            self._appliquer()

    # ------------------------------------------------------------------
    # API publique — états vision
    # ------------------------------------------------------------------

    def set_vision(self, etat: str):
        """
        Définit l'état vision courant.
        Valeurs acceptées : 'visage_reconnu', 'visage_inconnu'

        États urgence (décommentez dans COULEURS et PRIORITES pour activer) :
            'feu', 'chute'
        """
        if etat not in COULEURS:
            logger.warning(f"[LED] État vision inconnu : {etat}")
            return
        with self._lock:
            self._etat_vision = etat
            self._appliquer()

    def clear_vision(self):
        """
        Efface l'état vision (retourne à l'état GPT courant).
        À appeler quand le visage sort du champ, quand l'alerte est terminée, etc.
        """
        with self._lock:
            self._etat_vision = None
            self._appliquer()

    # ------------------------------------------------------------------
    # Raccourcis commodes
    # ------------------------------------------------------------------

    def ecoute(self):
        self.set_gpt("ecoute")

    def entend(self):
        self.set_gpt("entend")

    def reflechit(self):
        self.set_gpt("reflechit")

    def parle(self):
        self.set_gpt("parle")

    def idle(self):
        self.set_gpt("idle")

    def visage_reconnu(self):
        self.set_vision("visage_reconnu")

    def visage_inconnu(self):
        self.set_vision("visage_inconnu")

    # Urgences vision — décommenter quand prêt
    # def feu(self):
    #     self.set_vision("feu")
    #
    # def chute(self):
    #     self.set_vision("chute")

    # ------------------------------------------------------------------
    # Logique interne
    # ------------------------------------------------------------------

    def _etat_actif(self) -> str:
        """
        Retourne l'état de plus haute priorité entre GPT et vision.
        Appelé sous _lock.
        """
        if self._etat_vision is not None:
            prio_vision = PRIORITES.get(self._etat_vision, 0)
            prio_gpt    = PRIORITES.get(self._etat_gpt, 0)
            if prio_vision >= prio_gpt:
                return self._etat_vision
        return self._etat_gpt

    def _appliquer(self):
        """
        Applique la couleur de l'état actif sur le hardware.
        Appelé sous _lock — ne jamais appeler directement.
        """
        if self._audio_client is None:
            # hardware pas encore initialisé, on ignore silencieusement
            return
        etat = self._etat_actif()
        r, g, b = COULEURS.get(etat, (20, 20, 20))
        try:
            self._audio_client.LedControl(r, g, b)
            logger.debug(f"[LED] {etat} → RGB({r},{g},{b})")
        except Exception as e:
            logger.error(f"[LED] Erreur LedControl : {e}")


# ---------------------------------------------------------------------------
# Singleton — importer et utiliser directement
# ---------------------------------------------------------------------------
led = LedManager()
