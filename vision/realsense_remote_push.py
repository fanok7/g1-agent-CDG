#!/usr/bin/env python3
"""
vision/realsense_remote_push.py — Capture RealSense D435i + push réseau.

À lancer À LA MAIN sur la Jetson Thor (192.168.123.166), là où la D435i est
physiquement branchée depuis qu'elle a été déplacée de l'Orin NX vers la Thor.

Capture en continu, encode chaque frame en JPEG et la POST vers
vision_server.py (route /push_realsense) qui tourne sur l'Orin NX — celui-ci
écrit la frame dans /tmp/latest_realsense.jpg exactement comme le faisait
l'ancienne capture locale (même contrat pour qr_tool.py et le stream MJPEG).

Usage (sur la Thor) :
    python3 vision/realsense_remote_push.py [--target http://192.168.123.164:8080]

Dépendances : pyrealsense2, opencv-python, requests (ou urllib si absent).
"""

import argparse
import sys
import time

import cv2
import numpy as np
import pyrealsense2 as rs

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    import urllib.request
    _HAS_REQUESTS = False

WIDTH, HEIGHT = 640, 480
QUALITY       = 75
FPS           = 30
PUSH_INTERVAL = 1.0 / 30   # 30 fps envoyés au réseau


def _push(url: str, jpeg_bytes: bytes) -> bool:
    try:
        if _HAS_REQUESTS:
            r = requests.post(url, data=jpeg_bytes,
                               headers={'Content-Type': 'image/jpeg'}, timeout=2)
            return r.status_code == 204
        else:
            req = urllib.request.Request(url, data=jpeg_bytes, method='POST',
                                          headers={'Content-Type': 'image/jpeg'})
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 204
    except Exception as e:
        print(f"[RS-PUSH] Échec envoi : {e}", flush=True)
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', default='http://192.168.123.164:8080',
                         help="URL de base de vision_server.py sur l'Orin NX")
    args = parser.parse_args()
    push_url = args.target.rstrip('/') + '/push_realsense'

    # Auto-détection du premier device RealSense disponible (même logique que
    # l'ancienne capture locale dans vision_server.py).
    pipe = None
    cfg = None
    for attempt in range(10):
        try:
            ctx     = rs.context()
            devices = ctx.query_devices()
            if len(devices) == 0:
                raise RuntimeError("aucun device RealSense détecté")
            serial = devices[0].get_info(rs.camera_info.serial_number)
            p   = rs.pipeline()
            cfg = rs.config()
            cfg.enable_device(serial)
            cfg.enable_stream(rs.stream.color, WIDTH, HEIGHT, rs.format.bgr8, FPS)
            p.start(cfg)
            pipe = p
            print(f"[RS-PUSH] RealSense {serial} démarrée, push vers {push_url}",
                  flush=True)
            break
        except Exception as e:
            if attempt < 9:
                print(f"[RS-PUSH] Tentative {attempt+1}/10 ({e}) — retry 5s", flush=True)
                time.sleep(5)
            else:
                print("[RS-PUSH] RealSense indisponible — abandon", flush=True)
                sys.exit(1)

    _timeout_streak = 0
    _last_push = 0.0

    while True:
        try:
            frames  = pipe.wait_for_frames(timeout_ms=5000)
            color_f = frames.get_color_frame()
            if not color_f:
                continue
            _timeout_streak = 0

            now = time.time()
            if now - _last_push < PUSH_INTERVAL:
                continue
            _last_push = now

            frame = np.asanyarray(color_f.get_data())
            ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
            if not ok:
                continue
            _push(push_url, buf.tobytes())
        except Exception as e:
            print(f"[RS-PUSH] Erreur : {e}", flush=True)
            _timeout_streak += 1
            if _timeout_streak >= 3:
                print("[RS-PUSH] 3 timeouts — hardware_reset...", flush=True)
                try:
                    pipe.stop()
                    ctx2 = rs.context()
                    devs = ctx2.query_devices()
                    if len(devs) > 0:
                        devs[0].hardware_reset()
                    time.sleep(4)
                    pipe.start(cfg)
                    print("[RS-PUSH] Reprise après reset", flush=True)
                except Exception as re:
                    print(f"[RS-PUSH] Reset échoué ({re}) — retry 5s", flush=True)
                    time.sleep(5)
                _timeout_streak = 0
            else:
                time.sleep(1)


if __name__ == '__main__':
    main()
