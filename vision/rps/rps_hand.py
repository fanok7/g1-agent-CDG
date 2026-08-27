"""
rps_hand.py — Contrôle mains Inspire RH56E2 pour Pierre Feuille Ciseaux
========================================================================
Joue les gestes pierre/feuille/ciseaux avec délai pouce.
Lit les poses depuis hand_gestures.yaml

Utilisation standalone :
    python3.8 rps_hand.py

Utilisation comme module :
    from rps_hand import RPSHand
    hand = RPSHand(side='r')
    hand.play('pierre')
"""

import time
import yaml
import os
import argparse
from pymodbus.client import ModbusTcpClient

# ── Config ───────────────────────────────────────────────────────────────────
HANDS_CONFIG = {
    "r": {"ip": "192.168.123.211", "name": "droite"},
    "l": {"ip": "192.168.123.210", "name": "gauche"},
}
PORT          = 6000
DEVICE_ID     = 1
GESTURES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hand_gestures.yaml")

REG_POS_SET  = 1474
REG_FORCE_SET= 1498
REG_SPEED_SET= 1522
N_DOF        = 6

FINGER_NAMES = ["pinky", "ring", "middle", "index", "thumb_bend", "thumb_rot"]
THUMB_INDICES = [4, 5]   # thumb_bend, thumb_rot — délai 1s

FORCE_DEFAULT = 800
SPEED_DEFAULT = 1000
THUMB_DELAY   = 1.0      # secondes avant de bouger le pouce

# Poses par défaut si YAML absent
DEFAULT_POSES = {
    "pierre":  {"pinky": 1.0, "ring": 1.0, "middle": 1.0,
                "index": 1.0, "thumb_bend": 0.8, "thumb_rot": 0.5},
    "feuille": {"pinky": 0.0, "ring": 0.0, "middle": 0.0,
                "index": 0.0, "thumb_bend": 0.0, "thumb_rot": 0.5},
    "ciseaux": {"pinky": 1.0, "ring": 1.0, "middle": 0.0,
                "index": 0.0, "thumb_bend": 0.8, "thumb_rot": 0.5},
}

# Limites calibrées (min=ouvert, max=fermé)
FINGER_LIMITS = {
    "pinky":      {"min": 106,  "max": 1800},
    "ring":       {"min": 126,  "max": 1800},
    "middle":     {"min":  78,  "max": 1500},
    "index":      {"min": 129,  "max": 1250},
    "thumb_bend": {"min": 246,  "max": 1250},
    "thumb_rot":  {"min":  50,  "max": 1500},
}

POS_OPEN = [106, 126, 78, 129, 246, 50]

def norm_to_raw(name, val_norm):
    """Convertit 0.0→1.0 en valeur raw calibrée."""
    lim = FINGER_LIMITS[name]
    return int(lim["min"] + val_norm * (lim["max"] - lim["min"]))

# ── Chargement poses ──────────────────────────────────────────────────────────
def load_poses(side="r"):
    suffix = "droite" if side == "r" else "gauche"
    if not os.path.exists(GESTURES_FILE):
        print(f"[WARN] {GESTURES_FILE} absent — poses par défaut utilisées")
        return DEFAULT_POSES
    with open(GESTURES_FILE) as f:
        data = yaml.safe_load(f) or {}
    poses = {}
    for geste in ["pierre", "feuille", "ciseaux"]:
        key = next((k for k in [f"{geste}_{suffix}", f"{geste.capitalize()}_{suffix}"]
                    if k in data), None)
        if key is None:
            poses[geste] = DEFAULT_POSES[geste]
            print(f"[WARN] Pose '{geste}_{suffix}' absente du YAML — défaut utilisé")
            continue
        normalized = {}
        for finger, val in data[key].items():
            if isinstance(val, dict) and 'value' in val:
                mn, mx = val['min'], val['max']
                normalized[finger] = (val['value'] - mn) / (mx - mn) if mx != mn else 0.0
            else:
                normalized[finger] = float(val)
        poses[geste] = normalized
    return poses

# ── Classe principale ─────────────────────────────────────────────────────────
class RPSHand:
    def __init__(self, side="r"):
        cfg            = HANDS_CONFIG[side]
        self.ip        = cfg["ip"]
        self.name      = cfg["name"]
        self.poses     = load_poses(side)
        self.client    = ModbusTcpClient(self.ip, port=PORT)
        self.connected = self.client.connect()

        if self.connected:
            self.client.write_registers(REG_FORCE_SET,
                                        [FORCE_DEFAULT]*N_DOF, slave=DEVICE_ID)
            self.client.write_registers(REG_SPEED_SET,
                                        [SPEED_DEFAULT]*N_DOF, slave=DEVICE_ID)
            print(f"[OK] Main {self.name} connectée ({self.ip})")
        else:
            print(f"[WARN] Main {self.name} non connectée — mode simulation")

    def _send(self, positions):
        if not self.connected:
            print(f"[SIM] Envoi : {positions}")
            return
        try:
            self.client.write_registers(REG_POS_SET,
                                        [int(v) for v in positions],
                                        slave=DEVICE_ID)
        except Exception as e:
            print(f"[{self.name}] Erreur Modbus : {e}")

    def play(self, geste):
        """
        Joue un geste pierre/feuille/ciseaux.
        Séquence : doigts d'abord → +1s → pouce
        """
        if geste not in self.poses:
            print(f"[ERR] Geste '{geste}' inconnu")
            return

        pose = self.poses[geste]
        pos  = [norm_to_raw(n, pose.get(n, 0.0)) for n in FINGER_NAMES]

        # Étape 1 — doigts sans le pouce
        pos_sans_pouce        = pos[:]
        pos_sans_pouce[4]     = POS_OPEN[4]   # thumb_bend ouvert
        pos_sans_pouce[5]     = POS_OPEN[5]   # thumb_rot ouvert
        self._send(pos_sans_pouce)
        print(f"[{self.name}] {geste} — doigts")

        # Étape 2 — pouce après délai
        time.sleep(THUMB_DELAY)
        self._send(pos)
        print(f"[{self.name}] {geste} — pouce")

    def open(self):
        """Ouvre complètement la main."""
        # Pouce d'abord pour éviter conflit
        pos_pouce_ouvert = POS_OPEN[:]
        self._send(pos_pouce_ouvert)
        print(f"[{self.name}] Ouverture")

    def disconnect(self):
        """Ouvre la main puis ferme la connexion. Idempotent."""
        if not self.connected:
            return
        self.open()
        time.sleep(1.0)
        self.client.close()
        self.connected = False
        print(f"[{self.name}] Déconnectée")

# ── Test standalone ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hand",  default="r", help="r=droite  l=gauche")
    ap.add_argument("--geste", default=None,
                    help="pierre / feuille / ciseaux (test direct)")
    args = ap.parse_args()

    hand = RPSHand(side=args.hand)

    if args.geste:
        hand.play(args.geste)
        time.sleep(2.0)
        hand.disconnect()
    else:
        print("\nTest séquentiel : pierre → feuille → ciseaux")
        for g in ["pierre", "feuille", "ciseaux"]:
            print(f"\n→ {g}")
            hand.play(g)
            time.sleep(2.0)
        hand.disconnect()
