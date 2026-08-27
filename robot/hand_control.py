"""
main/hand_control.py — Couche bas niveau Modbus pour les mains Inspire RH56E2

Toutes les fonctions Modbus sont ici — les scripts de plus haut niveau
(shake_hand.py, finger_control.py…) importent depuis ce module.

Usage :
    from robot.hand_control import HandControl

    hand = HandControl('left')
    hand.connect()
    hand.open()
    hand.close(500)
    palm = hand.read_palm()
    hand.disconnect()
"""

from __future__ import annotations

import struct
import logging
from pymodbus.client import ModbusTcpClient

log = logging.getLogger(__name__)

# ── IPs des mains ──────────────────────────────────────────────────────────
HAND_IPS = {
    'left':  '192.168.123.211',
    'right': '192.168.123.210',
}
PORT      = 6000
DEVICE_ID = 1

# ── Registres Inspire RH56E2 ───────────────────────────────────────────────
REG_POS_SET   = 1474   # position setpoint   × 6 doigts (0=ouvert, 1000=fermé)
REG_ANGLE_SET = 1486   # angle setpoint      × 6 doigts (0–1000)
REG_FORCE_SET = 1498   # force setpoint      × 6 doigts (0–3000)
REG_SPEED_SET = 1522   # vitesse setpoint    × 6 doigts (0–1000)
REG_POS_ACT   = 1534   # position actuelle   × 6 doigts (lecture)
REG_FORCE_ACT = 1582   # force actuelle      × 6 doigts (signé, grammes)
REG_PALM      = 4900   # paume tactile        112 points (8×14, 0–4095)
PALM_COUNT    = 112

# Ordre des 6 doigts
# [0]=auriculaire [1]=annulaire [2]=majeur [3]=index [4]=pouce-flex [5]=pouce-rot

# ── Seuils paume par défaut ────────────────────────────────────────────────
DEFAULT_CONTACT_POINTS = 3    # nb points actifs → contact détecté
DEFAULT_RELEASE_POINTS = 1    # nb points → paume considérée libre
DEFAULT_CONTACT_TOTAL  = 100  # somme totale min pour valider le contact


class HandControl:
    """Contrôle bas niveau d'une main Inspire RH56E2 via Modbus TCP."""

    def __init__(self, side: str = 'left'):
        assert side in HAND_IPS, f"side doit être 'left' ou 'right', pas '{side}'"
        self.side   = side
        self.ip     = HAND_IPS[side]
        self._client = None

    # ── Connexion ──────────────────────────────────────────────────────────

    def connect(self) -> bool:
        self._client = ModbusTcpClient(self.ip, port=PORT)
        ok = self._client.connect()
        if ok:
            log.info('[HAND:%s] Connecté à %s', self.side, self.ip)
        else:
            log.error('[HAND:%s] Connexion échouée vers %s', self.side, self.ip)
        return ok

    def disconnect(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            log.info('[HAND:%s] Déconnecté.', self.side)

    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_socket_open()

    def reconnect(self) -> bool:
        self.disconnect()
        return self.connect()

    # ── Contrôle position (tous doigts) ───────────────────────────────────

    def open(self):
        """Ouvre complètement la main."""
        self._write(REG_POS_SET, [0] * 6)

    def close(self, value: int):
        """Ferme tous les doigts à la position donnée (0–1000)."""
        v = max(0, min(value, 1000))
        self._write(REG_POS_SET, [v] * 6)

    def set_positions(self, positions: list):
        """Contrôle individuel des 6 doigts. positions = liste de 6 valeurs 0–1000."""
        assert len(positions) == 6
        vals = [max(0, min(int(v), 1000)) for v in positions]
        self._write(REG_POS_SET, vals)

    # ── Contrôle vitesse & force ───────────────────────────────────────────

    def set_speed(self, speed: int):
        """Vitesse de tous les doigts (0–1000)."""
        v = max(0, min(speed, 1000))
        self._write(REG_SPEED_SET, [v] * 6)

    def set_force_limit(self, force: int):
        """Limite de force de tous les doigts (0–3000)."""
        v = max(0, min(force, 3000))
        self._write(REG_FORCE_SET, [v] * 6)

    # ── Lecture capteurs ───────────────────────────────────────────────────

    def read_palm(self) -> list | None:
        """Retourne les 112 valeurs du capteur tactile de paume (0–4095)."""
        r = self._client.read_holding_registers(REG_PALM, PALM_COUNT, DEVICE_ID)
        return list(r.registers) if not r.isError() else None

    def read_force(self) -> list | None:
        """Retourne la force actuelle des 6 doigts (grammes, signé)."""
        r = self._client.read_holding_registers(REG_FORCE_ACT, 6, DEVICE_ID)
        if r.isError():
            return None
        packed = struct.pack('>' + 'H' * 6, *r.registers)
        return list(struct.unpack('>' + 'h' * 6, packed))

    def read_positions(self) -> list | None:
        """Retourne la position actuelle des 6 doigts (0–1000)."""
        r = self._client.read_holding_registers(REG_POS_ACT, 6, DEVICE_ID)
        return list(r.registers) if not r.isError() else None

    # ── Analyse paume ──────────────────────────────────────────────────────

    def palm_contact(
        self,
        palm_data: list | None,
        min_points: int = DEFAULT_CONTACT_POINTS,
        min_total:  int = DEFAULT_CONTACT_TOTAL,
    ) -> tuple[bool, int, int]:
        """
        Analyse les données de paume et retourne :
            (contact: bool, nb_points_actifs: int, total: int)
        """
        if palm_data is None:
            return False, 0, 0
        total     = sum(palm_data)
        nb_points = sum(1 for v in palm_data if v > 0)
        contact   = nb_points >= min_points and total >= min_total
        return contact, nb_points, total

    def palm_released(
        self,
        palm_data: list | None,
        max_points: int = DEFAULT_RELEASE_POINTS,
    ) -> bool:
        """Retourne True si la paume est considérée comme libre."""
        if palm_data is None:
            return True
        nb_points = sum(1 for v in palm_data if v > 0)
        return nb_points < max_points

    # ── Interne ────────────────────────────────────────────────────────────

    def _write(self, register: int, values: list):
        if not self.is_connected():
            log.warning('[HAND:%s] Écriture ignorée — non connecté.', self.side)
            return
        self._client.write_registers(register, values, DEVICE_ID)
