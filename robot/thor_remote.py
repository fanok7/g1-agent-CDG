"""
robot/thor_remote.py — Démarrage/vérification de la vision déportée sur la Thor.

La caméra UGREEN 2K est branchée sur la Jetson Thor (192.168.123.166, pas ce
Jetson) depuis que face_id y a été déplacé pour profiter de son GPU. Un service
systemd --user tourne là-bas en continu (Restart=always, activé au boot via
`loginctl enable-linger unitree`) :
    g1-face-id.service  → vision/face_id/face_id.py (InsightFace GPU + YOLO)

Il persiste déjà seul (systemd), donc ce module n'est PAS ce qui le maintient en
vie — il sert juste à ce que `main.py` le vérifie/relance explicitement au
démarrage d'une session, pour un état propre et visible dans les logs plutôt que
de supposer silencieusement que la Thor va bien.

`g1-realsense-push.service` ne figure plus ici : la RealSense D435I est restée
branchée sur CE Jetson et n'est plus utilisée, donc le service bouclait sur un
crash côté Thor (`pyrealsense2` n'y voit aucun device). Il a été désactivé.

Connexion SSH par clé (pas de mot de passe en dur) — la clé publique de ce
Jetson doit être dans ~/.ssh/authorized_keys sur la Thor (déjà fait).
"""

import os
import subprocess

_VERBOSE_LOGS = os.environ.get('G1_VERBOSE_LOGS', '0') == '1'


def _vprint(*args, **kwargs):
    if _VERBOSE_LOGS:
        print(*args, **kwargs)


THOR_HOST = "unitree@192.168.123.166"
THOR_SERVICES = ("g1-face-id.service",)
SSH_TIMEOUT = 10


def _ssh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
         "-o", "BatchMode=yes", THOR_HOST, cmd],
        capture_output=True, text=True, timeout=SSH_TIMEOUT,
    )


def ensure_thor_vision_running() -> None:
    """Redémarre le service vision sur la Thor pour un état propre à chaque
    session. Best-effort : la Thor peut être injoignable (Wi-Fi coupé,
    éteinte...) sans que ça bloque le reste de l'agent — les tools vision
    dégraderont proprement (frames périmées) plutôt que de planter."""
    try:
        r = _ssh(
            "export XDG_RUNTIME_DIR=/run/user/$(id -u); "
            "systemctl --user restart " + " ".join(THOR_SERVICES)
        )
        if r.returncode != 0:
            print(f"[THOR] Échec redémarrage services vision : {r.stderr.strip()}")
            return
        _vprint("[THOR] Vision déportée (face_id) redémarrée sur la Thor.")
    except subprocess.TimeoutExpired:
        _vprint("[THOR] Injoignable (timeout SSH) — vision déportée non vérifiée, "
              "les tools vision utiliseront des données périmées si c'est le cas.")
    except Exception as e:
        print(f"[THOR] Erreur vérification vision déportée : {e}")


def thor_vision_status() -> dict:
    """État des services (pour diagnostic/dashboard) — {service: 'active'|'failed'|...}."""
    try:
        r = _ssh(
            "export XDG_RUNTIME_DIR=/run/user/$(id -u); "
            "systemctl --user is-active " + " ".join(THOR_SERVICES)
        )
        lines = r.stdout.strip().splitlines()
        return dict(zip(THOR_SERVICES, lines)) if lines else {}
    except Exception:
        return {}
