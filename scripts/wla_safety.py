"""
scripts/wla_safety.py — Filets de sécurité sur les actions du modèle WLA

Rendus **obligatoires** par le contrat après le lancement à blanc du 2026-10-02 côté
Thor, qui a mesuré sur 42 chunks :
  - des poses commandées jusqu'à ±1,7 m, très au-delà de l'espace de travail ;
  - des sauts de 12 à 35 cm d'un chunk au suivant, soit plusieurs m/s à 30 Hz.

Rien dans le protocole n'empêche ça : c'est au client de saturer.

⚠️ **Les deux mains n'ont PAS la même enveloppe** — le contrat § 4.1 donne des plages
distinctes, et le y est quasiment en miroir. Appliquer celle de la droite à la gauche
lui interdirait l'essentiel de son espace utile.

Ce module ne commande rien : il transforme des cibles en cibles sûres. Les décisions
sont journalisées (compteurs) pour qu'on sache si un filet se déclenche souvent — un
filet qui mord en permanence signale un modèle hors distribution, pas une sécurité qui
fait son travail.
"""

from __future__ import annotations

import numpy as np

# Enveloppes (m, repère pelvis), par main : union des plages du contrat § 4.1 (1 épisode)
# et des plages mesurées sur 245 épisodes / 181 356 frames le 2026-10-02.
# ⚠️ Le contrat sous-estimait la main GAUCHE de 20 cm en z (0,35 annoncé, 0,55 mesuré) :
# s'en tenir à ses chiffres aurait amputé l'espace utile et fait mordre le filet sur des
# gestes parfaitement valides. Ces plages ne couvrent qu'UNE tâche (Put_Clothes_Into_Basket) ;
# une autre tâche peut sortir de là.
ENVELOPE = {
    'left':  {'x': (-0.12, 0.55), 'y': (-0.23, 0.59), 'z': (-0.30, 0.55)},
    'right': {'x': (-0.03, 0.72), 'y': (-0.65, 0.33), 'z': (-0.27, 0.73)},
}
ENVELOPE_MARGIN = 0.05          # m

# Vitesse du point de préhension mesurée chez l'opérateur (par épisode, hors frontières) :
#   gauche  médiane 0,08  p99 0,45  p99,9 0,97 m/s
#   droite  médiane 0,24  p99 1,12  p99,9 1,76 m/s
# Une limite à 0,25 m/s écrêterait 41 % des cibles de la main droite — elle hacherait le
# geste au lieu de le sécuriser. À 1,5 m/s on n'en touche plus que 0,1 %, tout en bloquant
# les sauts pathologiques vus par la Thor (35 cm en un pas = 10,5 m/s).
MAX_SPEED = 1.5                 # m/s — plafond anti-aberration, ne déforme pas les données
CONTROL_HZ = 30.0

# Pour les PREMIERS essais réels, le contrat § 8 demande « vitesse réduite ». Ce facteur
# ralentit volontairement, au prix d'une déformation du geste : à 0,25 il écrête beaucoup.
# C'est un réglage d'essai, pas un réglage de production.
SPEED_SCALE = 1.0

MAX_STEP = MAX_SPEED / CONTROL_HZ   # 5 cm par pas à pleine vitesse

# Doigts : butées matérielles relevées le 2026-10-02 (voir wla_inspire_check.py).
THUMB_LAT_MIN = {'left': 0.40, 'right': 0.0}


class SafetyFilter:
    """Sature les cibles d'une main dans son enveloppe et limite leur vitesse."""

    def __init__(self, side, margin=ENVELOPE_MARGIN, max_speed=MAX_SPEED,
                 speed_scale=SPEED_SCALE):
        if side not in ENVELOPE:
            raise ValueError(side)
        self.side = side
        self.max_step = max_speed * speed_scale / CONTROL_HZ
        env = ENVELOPE[side]
        self.lo = np.array([env['x'][0], env['y'][0], env['z'][0]]) - margin
        self.hi = np.array([env['x'][1], env['y'][1], env['z'][1]]) + margin
        self._last = None
        self.n_total = 0
        self.n_clamped = 0
        self.n_limited = 0
        self.max_excess = 0.0       # m hors enveloppe, le pire vu
        self.max_jump = 0.0         # m demandés en un pas, le pire vu

    def reset(self, current_xyz=None):
        """À appeler avant une séquence : la limite de vitesse part de la pose réelle."""
        self._last = None if current_xyz is None else np.asarray(current_xyz, float).copy()

    def step(self, xyz):
        """Une cible → une cible sûre. Sature d'abord, limite la vitesse ensuite."""
        p = np.asarray(xyz, dtype=float)
        self.n_total += 1

        clamped = np.clip(p, self.lo, self.hi)
        excess = float(np.abs(p - clamped).max())
        if excess > 1e-9:
            self.n_clamped += 1
            self.max_excess = max(self.max_excess, excess)

        if self._last is None:
            self._last = clamped
            return clamped

        d = clamped - self._last
        n = float(np.linalg.norm(d))
        self.max_jump = max(self.max_jump, n)
        if n > self.max_step:
            clamped = self._last + d * (self.max_step / n)
            self.n_limited += 1

        self._last = clamped
        return clamped

    def chunk(self, poses):
        """Applique le filtre à un chunk (N, 3) ou (N, 6) — seuls xyz sont touchés."""
        out = np.array(poses, dtype=float, copy=True)
        for i in range(len(out)):
            out[i, :3] = self.step(out[i, :3])
        return out

    def report(self):
        if not self.n_total:
            return '%s : aucune cible' % self.side
        return ('%-5s %5d cibles | saturees %4d (%.1f%%, depassement max %.0f cm) | '
                'vitesse limitee %4d (%.1f%%, saut max %.0f cm)' %
                (self.side, self.n_total,
                 self.n_clamped, 100.0 * self.n_clamped / self.n_total, self.max_excess * 100,
                 self.n_limited, 100.0 * self.n_limited / self.n_total, self.max_jump * 100))


def clamp_fingers(fig6d, side):
    """Sature les doigts dans [0, 1] et respecte la butée de rotation du pouce."""
    f = np.clip(np.asarray(fig6d, dtype=float), 0.0, 1.0)
    f[1] = max(THUMB_LAT_MIN[side], f[1])     # thumb_lat
    return f
