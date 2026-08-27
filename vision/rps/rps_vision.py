"""
rps_vision.py — Détection geste adversaire Pierre Feuille Ciseaux
=================================================================
Utilise yolo11-rps-detection.pt sur la D435i (Jetson) ou
la Logitech C270 (/dev/video9) pour les tests PC.

Utilisation standalone :
    python3.8 rps_vision.py --source 0

Utilisation comme module :
    from rps_vision import RPSVision
    vision = RPSVision(model_path='yolo11-rps-detection.pt', source=0)
    vision.start()
    geste = vision.get_gesture()  # 'rock' / 'paper' / 'scissors' / None
    vision.stop()
"""

import cv2
import threading
import time
import argparse
import numpy as np
from collections import Counter
from ultralytics import YOLO

# ── Config ───────────────────────────────────────────────────────────────────
CONF_MIN    = 0.50
SKIP        = 1
BUF_LEN     = 4    # frames pour stabiliser le geste

# Mapping anglais → français (insensible à la casse)
FR = {"rock": "pierre", "paper": "feuille", "scissors": "ciseaux"}

class RPSVision:
    def __init__(self, model_path="rps_v2.engine", source=0,
                 conf=CONF_MIN):
        self.model_path    = model_path
        self.source        = source
        self.conf          = conf
        self._gesture      = None
        self._raw          = None
        self._running      = False
        self._lock         = threading.Lock()
        self._thread       = None
        self._frame        = None
        self._model        = None
        self._model_ready  = threading.Event()
        self._preloading   = False
        self._clear_req    = threading.Event()

    def preload(self):
        """Charge le modèle YOLO en avance (appeler pendant prepare())."""
        self._preloading = True
        model = YOLO(self.model_path)
        # Warm-up : la 1re inférence initialise CUDA/TensorRT (plusieurs secondes)
        model(np.zeros((640, 640, 3), dtype=np.uint8), imgsz=640, verbose=False)
        self._model = model
        self._model_ready.set()
        print(f"[RPS] Modèle prêt : {list(model.names.values())}")

    def reset(self):
        """Purge le geste stable et le buffer — à appeler à la révélation pour
        ignorer les détections faites pendant le compte à rebours."""
        self._clear_req.set()
        with self._lock:
            self._gesture = None
            self._raw     = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"[INFO] Vision démarrée — source={self.source}")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def get_gesture(self):
        """Retourne le geste stable courant (français) ou None."""
        with self._lock:
            return self._gesture

    def get_frame(self):
        """Retourne la dernière frame annotée."""
        with self._lock:
            return self._frame

    def capture_gesture(self, timeout=2.0):
        """
        Attend un geste stable pendant timeout secondes.
        Retourne le geste ou None si timeout.
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            g = self.get_gesture()
            if g: return g
            time.sleep(0.1)
        return None

    def _loop(self):
        if self._preloading:
            # preload() en cours dans un autre thread — ne pas charger en double
            self._model_ready.wait(timeout=30.0)
        if self._model is None:
            self._model = YOLO(self.model_path)
            self._model_ready.set()
        model = self._model
        print(f"[INFO] Classes modèle : {model.names}")

        cap = cv2.VideoCapture(
            int(self.source) if str(self.source).isdigit() else self.source)
        if not cap.isOpened():
            print(f"[ERR] Impossible d'ouvrir : {self.source}")
            return

        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        frame_idx   = 0
        last_res    = []
        gesture_buf = []

        while self._running:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.05); continue

            frame_idx += 1

            if self._clear_req.is_set():
                gesture_buf.clear()
                self._clear_req.clear()

            if frame_idx % SKIP == 0:
                # Frame native : le moteur TensorRT a une entrée fixe 640x640,
                # réduire à 320 divisait la résolution utile par deux
                results  = model(frame, imgsz=640, verbose=False,
                                 conf=self.conf)
                last_res = []
                for r in results:
                    if r.boxes is None or len(r.boxes) == 0: continue
                    for box, cls, c in zip(r.boxes.xyxy.cpu().numpy(),
                                           r.boxes.cls.cpu().numpy(),
                                           r.boxes.conf.cpu().numpy()):
                        last_res.append((box.copy(),
                                         model.names[int(cls)], float(c)))

                best = max(last_res, key=lambda x: x[2]) if last_res else None
                g = FR.get(best[1].lower()) if best else None
                gesture_buf.append(g)
                if len(gesture_buf) > BUF_LEN: gesture_buf.pop(0)
                valid = [x for x in gesture_buf if x]
                stable = (Counter(valid).most_common(1)[0][0]
                          if len(valid) >= BUF_LEN//2 else None)
                with self._lock:
                    self._gesture = stable
                    self._raw     = g

            # Dessin
            out = cv2.resize(frame, (1280, 720))
            sx2, sy2 = 1280/640, 720/480
            for box, label, conf_det in last_res:
                x1,y1 = int(box[0]*sx2), int(box[1]*sy2)
                x2,y2 = int(box[2]*sx2), int(box[3]*sy2)
                cv2.rectangle(out,(x1,y1),(x2,y2),(100,255,100),2)
                cv2.putText(out,
                            f"{FR.get(label,label).upper()} {conf_det:.0%}",
                            (x1,y1-10), cv2.FONT_HERSHEY_DUPLEX, 0.9,
                            (100,255,100), 2)

            with self._lock:
                g_disp = self._gesture or "?"
            cv2.putText(out, f"Geste : {g_disp.upper()}",
                        (20, out.shape[0]-20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200,200,200), 2)

            with self._lock:
                self._frame = out.copy()

        cap.release()

# ── Test standalone ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import socket
    ap = argparse.ArgumentParser()
    ap.add_argument("--model",  default="yolo11-rps-detection.pt")
    ap.add_argument("--source", default="0")
    ap.add_argument("--conf",   type=float, default=0.50)
    ap.add_argument("--port",   type=int,   default=8082)
    ap.add_argument("--stream", action="store_true", help="MJPEG HTTP stream")
    args = ap.parse_args()

    vision = RPSVision(args.model, args.source, args.conf)
    vision.start()

    if args.stream:
        # ── Serveur MJPEG ──────────────────────────────────────────────────────
        HOST = "0.0.0.0"
        print(f"[STREAM] http://192.168.123.164:{args.port}/  — Ctrl+C pour quitter")

        def _handle(conn):
            try:
                conn.recv(1024)
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: multipart/x-mixed-replace; boundary=frame\r\n\r\n"
                )
                while True:
                    frame = vision.get_frame()
                    if frame is None:
                        time.sleep(0.05)
                        continue
                    _, jpg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    data = jpg.tobytes()
                    try:
                        conn.sendall(
                            b"--frame\r\nContent-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                            + data + b"\r\n"
                        )
                    except BrokenPipeError:
                        break
                    time.sleep(0.05)
            finally:
                conn.close()

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, args.port))
        srv.listen(4)
        try:
            while True:
                conn, _ = srv.accept()
                threading.Thread(target=_handle, args=(conn,), daemon=True).start()
        except KeyboardInterrupt:
            pass
        finally:
            vision.stop()
            srv.close()

    else:
        print("Appuyez sur Q pour quitter")
        while True:
            frame = vision.get_frame()
            if frame is not None:
                cv2.imshow("RPS Vision", frame)
            if cv2.waitKey(1) & 0xFF in (ord('q'), 27):
                break
        vision.stop()
        cv2.destroyAllWindows()
