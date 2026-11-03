"""Capture l'état du moteur 0 dès la première trame rt/lowstate après un démarrage.

Lecture seule : aucun ordre n'est envoyé au robot (pas de publisher DDS).
But : distinguer un défaut re-détecté à la mise en puissance d'un défaut
présent dès la première trame (donc mémorisé ou capteur mort).

    python3.8 scripts/watch_motor0_boot.py        # depuis unitree_sdk2_python
"""

import time

from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

MOTOR = 0
_t0 = time.time()
_last = None


def _on_state(msg):
    global _last
    s = msg.motor_state[MOTOR]
    # temperature[0] = carcasse, temperature[1] = bobinage (terminations.hpp)
    cle = (s.motorstate, s.temperature[0], s.temperature[1], round(s.tau_est))
    if cle == _last:
        return
    _last = cle
    print('t+%6.1fs  defaut=%-5d carcasse=%3d bobinage=%3d couple=%8.1f'
          % (time.time() - _t0, s.motorstate, s.temperature[0], s.temperature[1], s.tau_est),
          flush=True)


ChannelFactoryInitialize(0, 'eth0')
ChannelSubscriber('rt/lowstate', LowState_).Init(_on_state, 10)
print('En attente de la premiere trame... (Ctrl+C pour arreter)', flush=True)
while True:
    time.sleep(1)
