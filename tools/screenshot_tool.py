"""
tools/screenshot_tool.py — Capture d'écran de ce que voit le robot + envoi email.

- capture_screenshot()   : copie la frame courante (/tmp/latest_ugreen.jpg) dans
                           vision/Screenshot/<prefix>_<ts>.jpg
- send_image_email()     : envoie une image en pièce jointe via Gmail (réutilise
                           l'auth OAuth de tools/gmail.py)
- tool `prendre_screenshot` : exposé à l'agent (capture + envoi email optionnel)

Utilisé aussi par fall_alert_loop (agent/events.py) : sur une chute, l'image
sauvegardée par le module fall_detection est envoyée à FALL_RECIPIENT.
"""

import os
import time
import shutil
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import ipc
from tools.registry import register

UGREEN_FRAME   = ipc.UGREEN_FRAME   # frame courante écrite par face_id.py
SCREENSHOT_DIR = "/home/unitree/g1_agent_interim/vision/Screenshot"
FALL_RECIPIENT = "nael1919@gmail.com"        # destinataire des alertes de chute
FIRE_RECIPIENT = "nael1919@gmail.com"        # destinataire des alertes de feu/fumée


def capture_screenshot(prefix: str = "screenshot") -> str:
    """Copie la dernière frame UGREEN dans SCREENSHOT_DIR. Retourne le chemin, ou ''
    si aucune frame disponible (face_id.py pas démarré)."""
    if not os.path.exists(UGREEN_FRAME):
        return ""
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    dest = os.path.join(SCREENSHOT_DIR, f"{prefix}_{int(time.time())}.jpg")
    shutil.copyfile(UGREEN_FRAME, dest)
    return dest


def send_image_email(image_path: str, to: str, subject: str, body: str) -> str:
    """Envoie `image_path` en pièce jointe via Gmail. Réutilise l'auth OAuth de
    tools/gmail.py. Dégrade proprement si Gmail indisponible."""
    try:
        from tools.gmail import _get_service
    except Exception as e:
        return f"Gmail indisponible : {e}"
    try:
        svc = _get_service()
        if not svc:
            return "Erreur : Gmail non disponible (libs ou token manquant)."

        msg = MIMEMultipart()
        msg["to"] = to
        msg["subject"] = subject
        msg.attach(MIMEText(body))

        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                img = MIMEImage(f.read())
            img.add_header("Content-Disposition", "attachment",
                           filename=os.path.basename(image_path))
            msg.attach(img)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Email envoyé à {to}."
    except Exception as e:
        return f"Erreur envoi Gmail : {e}"


def _handler(envoyer_a: str = "", description: str = "capture") -> str:
    """Tool agent : prend une photo de la scène, l'enregistre, l'envoie si demandé."""
    path = capture_screenshot(prefix="screenshot")
    if not path:
        return ("Impossible de prendre la photo : aucune image caméra disponible "
                "(face_id.py n'écrit pas /tmp/latest_ugreen.jpg).")
    if envoyer_a:
        sent = send_image_email(
            path, envoyer_a,
            subject="Photo prise par le robot G1",
            body=f"Photo prise par le robot G1 ({description}).",
        )
        return f"Photo enregistrée dans {path}. {sent}"
    return f"Photo enregistrée dans {path}."


register(
    schema={
        "name": "prendre_screenshot",
        "description": (
            "Prend une photo de ce que voit le robot et l'enregistre dans "
            "vision/Screenshot. Peut aussi l'envoyer par email si une adresse est fournie."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "envoyer_a":   {"type": "string",
                                "description": "Adresse email destinataire (optionnel). Vide = pas d'envoi."},
                "description": {"type": "string",
                                "description": "Courte description du contexte de la photo (optionnel)."},
            },
            "required": [],
        },
    },
    handler=_handler,
)
