"""
launcher_server.py — Petit serveur de démarrage, indépendant de main.py.

Problème résolu : la page de la tablette (tablet_server) n'existe que parce
que main.py la démarre lui-même (thread interne) — tant que main.py n'est
pas lancé, il n'y a donc rien à afficher sur la tablette pour proposer un
bouton "Démarrer" (œuf et poule).

Ce script occupe le port 8000 (celui que la tablette atteint via le tunnel
adb reverse) UNIQUEMENT quand main.py ne tourne pas, avec une page minimale
"Démarrer". Dès qu'on clique, il :
  1. lance main.py (même commande que le lancement manuel documenté :
     cwd=/home/unitree/unitree_sdk2_python, requis par le SDK Unitree),
  2. libère immédiatement le port 8000 pour laisser main.py y brancher son
     propre tablet_server quelques secondes plus tard,
  3. attend la fin du processus main.py (crash ou arrêt volontaire),
  4. reprend la main sur le port 8000 et réaffiche la page "Démarrer".

Ne modifie AUCUN fichier existant (main.py, tablet_server, launch.py) —
composant additif, lancé via un service systemd séparé (voir
scripts/g1-launcher.service).
"""

import http.server
import os
import socketserver
import subprocess
import threading
import time

BASE_DIR = "/home/unitree/g1_agent_interim"
SDK_DIR = "/home/unitree/unitree_sdk2_python"
PORT = 8000

_HTML_START_PAGE = b"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>G1 - Demarrage</title>
<style>
  body { margin:0; height:100vh; display:flex; align-items:center; justify-content:center;
         background:#F5F5F7; font-family:'Segoe UI',Arial,sans-serif; }
  #card { background:#fff; border-radius:24px; padding:48px 56px; text-align:center;
          box-shadow:0 20px 40px rgba(0,0,0,0.08); }
  h1 { color:#1F1F27; font-size:1.4em; margin:0 0 24px; }
  button { font-size:1.2em; font-weight:700; padding:20px 48px; border:none;
           border-radius:20px; background:#7B2CBF; color:#fff; cursor:pointer; }
  button:disabled { opacity:0.5; }
  #msg { margin-top:20px; color:#5B5B6A; font-size:0.95em; min-height:1.2em; }
</style></head>
<body>
  <div id="card">
    <h1>Robot arrete</h1>
    <button id="btn" type="button">Demarrer</button>
    <div id="msg"></div>
  </div>
<script>
  const btn = document.getElementById('btn');
  const msg = document.getElementById('msg');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    msg.textContent = 'Demarrage en cours...';
    try {
      await fetch('/start', { method: 'POST' });
    } catch (e) {
      msg.textContent = 'Erreur, reessayez.';
      btn.disabled = false;
      return;
    }
    msg.textContent = 'Demarrage en cours (peut prendre 20-30s)...';
    setTimeout(pollUntilReady, 4000);
  });
  function pollUntilReady() {
    fetch('/', { cache: 'no-store' })
      .then(() => location.reload())
      .catch(() => setTimeout(pollUntilReady, 2000));
  }
</script>
</body></html>"""


class _StartHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_HTML_START_PAGE)))
            self.end_headers()
            self.wfile.write(_HTML_START_PAGE)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/start":
            self.server.start_requested = True
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        print(f"[Launcher] {self.address_string()} - {fmt % args}")


class _StartServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _serve_start_page() -> None:
    """Bloque jusqu'à ce que POST /start soit reçu, puis libère le port."""
    srv = _StartServer(("0.0.0.0", PORT), _StartHandler)
    srv.start_requested = False
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    print(f"[Launcher] Page de demarrage servie sur :{PORT}")
    while not srv.start_requested:
        time.sleep(0.2)
    srv.shutdown()
    thread.join()
    srv.server_close()


def main() -> None:
    while True:
        _serve_start_page()
        print("[Launcher] Lancement de main.py...")
        proc = subprocess.Popen(
            ["python3.8", os.path.join(BASE_DIR, "main.py")],
            cwd=SDK_DIR,
        )
        proc.wait()
        print(f"[Launcher] main.py termine (code {proc.returncode}) — page de demarrage a nouveau disponible.")


if __name__ == "__main__":
    main()
