"""
robot/hand_idle.py — Mouvement naturel des mains Inspire RH56E2 pour l'agent vocal
====================================================================================
Fait bouger légèrement les mains (respiration + bruit organique par doigt) en
tâche de fond, pour éviter l'effet "statue" pendant que le robot écoute ou parle.

Utilisation dans main.py / events.py :
    from robot import hand_idle
    hand_idle.start()   # au démarrage de l'agent — tourne en continu
    hand_idle.stop()    # avant un geste précis des doigts (ex: RPS)
    hand_idle.start()   # pour reprendre après le geste

Connexion non-bloquante et tolérante aux pannes : si les mains ne répondent
pas, l'agent continue de fonctionner sans elles (juste un warning au log).
"""

import time
import math
import random
import threading
from pymodbus.client import ModbusTcpClient

HANDS = {
    "r": {"ip": "192.168.123.210", "name": "droite"},
    "l": {"ip": "192.168.123.211", "name": "gauche"},
}
PORT      = 6000
DEVICE_ID = 1

REG_POS_SET   = 1474
REG_FORCE_SET = 1498
REG_SPEED_SET = 1522
REG_POS_ACT   = 1534

N_DOF = 6

MODE_RIGID = {
    "force": [1000] * N_DOF,
    "speed": [900]  * N_DOF,
}

# Amplitude du mouvement idle (position 0-1000, doigt le plus mobile — voir
# _IDLE_WEIGHTS). Doublée le 2026-08-13 (150→300) pour un mouvement plus visible
# à l'œil nu ; au-delà, risque de clipping visible si la position de repos
# (base_pose = position au moment de start()) est déjà proche de 0 ou 1000.
IDLE_AMPLITUDE = 300


class InspireHand:
    def __init__(self, ip, name):
        self.ip = ip
        self.name = name
        self.client = ModbusTcpClient(ip, port=PORT, timeout=2.0)
        self._idle_thread = None
        self._idle_stop_event = None

    def connect(self):
        return self.client.connect()

    def write(self, reg, values):
        r = self.client.write_registers(reg, values, device_id=DEVICE_ID)
        return not r.isError()

    def read(self, reg, count=N_DOF):
        r = self.client.read_holding_registers(reg, count=count, device_id=DEVICE_ID)
        if r.isError():
            return None
        return list(r.registers)

    def set_rigid(self):
        self.write(REG_FORCE_SET, MODE_RIGID["force"])
        self.write(REG_SPEED_SET, MODE_RIGID["speed"])

    def get_position(self):
        return self.read(REG_POS_ACT)

    # Couplage inter-doigts (synergie tendineux cubital → radial) — voir
    # hand_compliant.py pour la justification biomécanique complète.
    SYNERGY_MATRIX = [
        [1.00, 0.00, 0.00, 0.00, 0.00, 0.00],
        [0.20, 1.00, 0.00, 0.00, 0.00, 0.00],
        [0.00, 0.15, 1.00, 0.00, 0.00, 0.00],
        [0.00, 0.00, 0.10, 1.00, 0.00, 0.00],
        [0.00, 0.00, 0.00, 0.00, 1.00, 0.00],
        [0.00, 0.00, 0.00, 0.00, 0.00, 1.00],
    ]

    @staticmethod
    def apply_synergy(pose):
        coupled = []
        for row in InspireHand.SYNERGY_MATRIX:
            val = sum(coef * p for coef, p in zip(row, pose))
            coupled.append(max(0, min(1000, round(val))))
        return coupled

    _IDLE_WEIGHTS = [0.6, 0.8, 1.0, 1.0, 0.4, 0.3]
    _IDLE_PHASES  = [0.0, 0.4, 0.15, 0.55, 0.8, 0.25]

    class _ValueNoise1D:
        """Bruit de valeur continu (façon Perlin 1D), sans discontinuité de
        courbure — cf. hand_compliant.py pour le détail."""
        def __init__(self, period, rng):
            self._period = period
            self._rng = rng
            self._a = rng.uniform(-1, 1)
            self._b = rng.uniform(-1, 1)
            self._segment = 0

        def sample(self, t):
            phase = t / self._period
            seg = math.floor(phase)
            f = phase - seg
            while seg > self._segment:
                self._a, self._b = self._b, self._rng.uniform(-1, 1)
                self._segment += 1
            smooth_f = f * f * (3 - 2 * f)
            return self._a + (self._b - self._a) * smooth_f

    def _idle_animation(self, base_pose, control_hz=30, amplitude=150,
                         breathing_period=3.5, tremor_amplitude=6,
                         tremor_freq=9.0, synergy=True, stop_event=None):
        dt = 1.0 / control_hz
        t0 = time.perf_counter()
        rng = random.Random()
        noise = [self._ValueNoise1D(period=0.6 + 0.15 * i, rng=rng) for i in range(N_DOF)]
        tremor_phase = [rng.uniform(0, 2 * math.pi) for _ in range(N_DOF)]

        while stop_event is None or not stop_event.is_set():
            elapsed = time.perf_counter() - t0
            pose = []
            for i, (b, w, phase) in enumerate(zip(base_pose, self._IDLE_WEIGHTS, self._IDLE_PHASES)):
                breathing = amplitude * 0.5 * w * math.sin(
                    2 * math.pi * (elapsed / breathing_period + phase))
                wander = noise[i].sample(elapsed) * amplitude * 0.3 * w
                tremor = tremor_amplitude * w * math.sin(
                    2 * math.pi * tremor_freq * elapsed + tremor_phase[i])
                pose.append(max(0, min(1000, round(b + breathing + wander + tremor))))

            if synergy:
                pose = self.apply_synergy(pose)
            self.write(REG_POS_SET, pose)

            step = int((time.perf_counter() - t0) / dt) + 1
            next_tick = t0 + step * dt
            sleep_time = next_tick - time.perf_counter()
            if sleep_time > 0:
                time.sleep(sleep_time)

    def start_idle(self, base_pose=None, **kwargs):
        if self._idle_thread is not None and self._idle_thread.is_alive():
            return
        if base_pose is None:
            base_pose = self.get_position()
            if base_pose is None:
                print(f"[HAND_IDLE] Impossible de lire la position de {self.name}, idle non démarrée")
                return
        self._idle_stop_event = threading.Event()
        self._idle_thread = threading.Thread(
            target=self._idle_animation,
            args=(base_pose,),
            kwargs={**kwargs, "stop_event": self._idle_stop_event},
            daemon=True,
        )
        self._idle_thread.start()

    def stop_idle(self, timeout=2.0):
        if self._idle_stop_event is not None:
            self._idle_stop_event.set()
        if self._idle_thread is not None:
            self._idle_thread.join(timeout=timeout)
        self._idle_thread = None
        self._idle_stop_event = None


# ── API module-level, utilisée par le reste de l'agent ─────────────────────
_hands = {}
_lock = threading.Lock()
_available = False


def _connect_all():
    global _available
    ok = []
    for k, cfg in HANDS.items():
        h = InspireHand(cfg["ip"], cfg["name"])
        if h.connect():
            h.set_rigid()
            _hands[k] = h
            ok.append(cfg["name"])
        else:
            print(f"[HAND_IDLE] Main {cfg['name']} ({cfg['ip']}) injoignable — idle désactivée pour cette main")
    _available = len(ok) > 0
    if ok:
        print(f"[HAND_IDLE] Mains connectées : {', '.join(ok)}")


def start():
    """Démarre (ou reprend) le mouvement naturel des mains en tâche de fond.
    Sûr à appeler plusieurs fois — no-op si déjà en cours. Connexion Modbus
    tentée une seule fois (non bloquant pour l'agent si les mains sont hors
    tension : log un warning et continue sans elles)."""
    with _lock:
        if not _hands and not _available:
            _connect_all()
        if not _hands:
            return
        for h in _hands.values():
            pos = h.get_position()
            if pos:
                h.start_idle(base_pose=pos, amplitude=IDLE_AMPLITUDE)


def stop():
    """Arrête le mouvement naturel — à appeler avant un geste précis des
    doigts (ex: le module RPS qui pilote les mains via rps_hand.py)."""
    with _lock:
        for h in _hands.values():
            h.stop_idle()
