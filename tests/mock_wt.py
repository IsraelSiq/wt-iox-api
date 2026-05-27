"""tests/mock_wt.py

Simula a API HTTP nativa do War Thunder (localhost:8111)
para testes locais sem o jogo rodando.

Uso:
    python tests/mock_wt.py

Certifique-se de que o servidor está rodando:
    python -m server.main
"""

import json
import math
import random
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8111

# Unidades simuladas (posições normalizadas 0.0-1.0)
UNITS = [
    dict(id="u1", btype="P-51D",   color="Blue",   x=0.52, y=0.48, icon="Fighter",    alt=20000),
    dict(id="u2", btype="Spitfire",color="Blue",   x=0.55, y=0.52, icon="Fighter",    alt=18000),
    dict(id="u3", btype="Bf109G",  color="Red",    x=0.60, y=0.40, icon="Fighter",    alt=22000),
    dict(id="u4", btype="Fw190A",  color="Red",    x=0.65, y=0.35, icon="Fighter",    alt=15000),
    dict(id="u5", btype="T-34",    color="Blue",   x=0.45, y=0.55, icon="Tank",       alt=100),
    dict(id="u6", btype="Tiger I", color="Red",    x=0.38, y=0.58, icon="Tank",       alt=100),
    dict(id="u7", btype="B-17",    color="Blue",   x=0.48, y=0.42, icon="Bomber",     alt=25000),
]

_t0 = time.time()
_states = {u["id"]: {**u} for u in UNITS}


def get_state():
    t = time.time() - _t0
    return {
        "type":         "P-51D-30-NA",
        "IAS, km/h":    round(480 + math.sin(t * 0.1) * 40, 1),
        "TAS, km/h":    round(510 + math.sin(t * 0.1) * 40, 1),
        "altitude_10":  round(6000 + math.sin(t * 0.05) * 300, 1),
        "vario":        round(math.sin(t * 0.3) * 3, 2),
        "compass":      round((t * 5) % 360, 1),
        "pitch":        round(math.sin(t * 0.3) * 5, 2),
        "roll":         round(math.sin(t * 0.2) * 15, 2),
        "AoA, deg":     round(2.5 + math.sin(t * 0.4) * 1.5, 2),
        "Ny":           round(1.0 + abs(math.sin(t * 0.2)) * 2, 2),
        "throttle_1":   0.85,
        "rpm_throttle_1": 0.87,
        "Mfuel":        round(max(0, 300 - t * 0.5), 1),
        "valid":        True,
    }


def get_map_info():
    return {
        "map_name": "normandy",
        "map_min": [0, 0],
        "map_max": [65536, 65536],
    }


def get_map_obj():
    t = time.time() - _t0
    objs = [{"id": "player", "x": 0.5, "y": 0.5, "icon": "Player", "color": "Blue"}]
    for uid, u in _states.items():
        # Movimento circular lento
        angle = math.radians(t * 2 + hash(uid) % 360)
        u["x"] = max(0.1, min(0.9, u["x"] + math.cos(angle) * 0.0003))
        u["y"] = max(0.1, min(0.9, u["y"] + math.sin(angle) * 0.0003))
        u["dx"] = round(math.cos(angle), 3)
        u["dy"] = round(math.sin(angle), 3)
        u["alt"] = u["alt"] + random.uniform(-50, 50)
        objs.append(dict(u))
    return objs


class WTHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # silencia logs do HTTPServer

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/state":
            body = json.dumps(get_state()).encode()
        elif path == "/map_obj.json":
            body = json.dumps(get_map_obj()).encode()
        elif path == "/map_info.json":
            body = json.dumps(get_map_info()).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = HTTPServer(("127.0.0.1", PORT), WTHandler)
    print(f"[mock_wt] War Thunder API simulada em http://127.0.0.1:{PORT}")
    print(f"[mock_wt] {len(UNITS)} unidades  |  mapa: Normandy")
    print(f"[mock_wt] Acesse: http://127.0.0.1:8000/radar")
    print("[mock_wt] Ctrl+C para parar.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock_wt] Encerrado.")


if __name__ == "__main__":
    main()
