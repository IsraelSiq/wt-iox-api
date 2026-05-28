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

# Player fica exatamente em (0.5, 0.5) — centro do mapa.
# Contatos definidos com pequenas variações ao redor desse ponto
# (~0.01-0.08 em coords normalizadas ≈ 0.6-5 km no mapa Normandy 65536m).
# alt está em PÉS (como o WT envia) — main.py converte para metros (* 0.3048).
UNITS = [
    dict(id="u1", btype="P-51D",    color="Blue", x=0.515, y=0.490, icon="Fighter",  alt=20000),
    dict(id="u2", btype="Spitfire",  color="Blue", x=0.530, y=0.510, icon="Fighter",  alt=18000),
    dict(id="u3", btype="Bf109G",   color="Red",  x=0.545, y=0.480, icon="Fighter",  alt=22000),
    dict(id="u4", btype="Fw190A",   color="Red",  x=0.560, y=0.460, icon="Fighter",  alt=15000),
    dict(id="u5", btype="T-34",     color="Blue", x=0.490, y=0.520, icon="Tank",     alt=100),
    dict(id="u6", btype="Tiger I",  color="Red",  x=0.475, y=0.535, icon="Tank",     alt=100),
    dict(id="u7", btype="B-17",     color="Blue", x=0.508, y=0.470, icon="Bomber",   alt=25000),
    dict(id="u8", btype="Destroyer",color="Red",  x=0.465, y=0.545, icon="Ship",     alt=0),
]

# Base de altitude em pés para cada unidade (sem drift acumulado)
_ALT_BASE = {u["id"]: u["alt"] for u in UNITS}

_t0 = time.time()
# Estado mutável das unidades (posição x/y atualizada a cada frame)
_states = {u["id"]: {**u} for u in UNITS}


def get_state():
    t = time.time() - _t0
    return {
        "type":             "P-51D-30-NA",
        "IAS, km/h":        round(480 + math.sin(t * 0.1) * 40, 1),
        "TAS, km/h":        round(510 + math.sin(t * 0.1) * 40, 1),
        "altitude_10":      round(6000 + math.sin(t * 0.05) * 300, 1),
        "vario":            round(math.sin(t * 0.3) * 3, 2),
        "compass":          round((t * 5) % 360, 1),
        "pitch":            round(math.sin(t * 0.3) * 5, 2),
        "roll":             round(math.sin(t * 0.2) * 15, 2),
        "AoA, deg":         round(2.5 + math.sin(t * 0.4) * 1.5, 2),
        "Ny":               round(1.0 + abs(math.sin(t * 0.2)) * 2, 2),
        "throttle_1":       0.85,
        "rpm_throttle_1":   0.87,
        "Mfuel":            round(max(0, 300 - t * 0.5), 1),
        "valid":            True,
    }


def get_map_info():
    return {
        "map_name": "normandy",
        "map_min":  [0, 0],
        "map_max":  [65536, 65536],
    }


def get_map_obj():
    t = time.time() - _t0

    # Marcador obrigatório do jogador — lido por _extract_player_pos() em main.py
    objs = [{"id": "player", "x": 0.5, "y": 0.5, "icon": "Player", "color": "Blue"}]

    for uid, u in _states.items():
        # Movimento circular lento em torno da posição inicial (sem drift acumulado)
        base_x = UNITS[next(i for i, un in enumerate(UNITS) if un["id"] == uid)]["x"]
        base_y = UNITS[next(i for i, un in enumerate(UNITS) if un["id"] == uid)]["y"]
        orbit_r = 0.01  # raio da órbita (~655m no mapa Normandy)
        angle = math.radians(t * 3 + hash(uid) % 360)
        u["x"] = round(base_x + math.cos(angle) * orbit_r, 5)
        u["y"] = round(base_y + math.sin(angle) * orbit_r, 5)
        # dx/dy = vetor de heading unitário (necessário para heading_deg no poller)
        u["dx"] = round(math.cos(angle), 3)
        u["dy"] = round(math.sin(angle), 3)
        # Altitude oscila ±200ft em torno da base (sem drift acumulado)
        u["alt"] = round(_ALT_BASE[uid] + math.sin(t * 0.1 + hash(uid)) * 200, 1)
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
    print(f"[mock_wt] Player em (0.5, 0.5) — contatos dentro de ~5 km")
    print(f"[mock_wt] Acesse: http://127.0.0.1:8000/radar")
    print("[mock_wt] Ctrl+C para parar.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock_wt] Encerrado.")


if __name__ == "__main__":
    main()
