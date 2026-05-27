# wt-iox-api

> Real-time telemetry and tactical radar for **War Thunder**, served as a local REST/WebSocket API.

Polls War Thunder's native localhost HTTP API (`localhost:8111`) and serves live telemetry + all map units through a FastAPI server with a tactical radar rendered in the browser.

> Inspired by [dcs-iox-api](https://github.com/IsraelSiq/dcs-iox-api). Same architecture, adapted for War Thunder.

---

## Features

- **Tactical radar** — animated sweep, contact trails, altitude filter, threat alerts
- **Full unit awareness** — all units on the minimap via `localhost:8111/map_obj.json`
- **Player telemetry** — speed, altitude, heading, G-load, fuel at ~10 Hz
- **REST + WebSocket API** — integrate with any overlay or external tool
- **No mods required** — War Thunder exposes the API natively while running
- **Standalone `.exe`** — runs without Python, opens browser automatically
- **Local mock** — test radar without War Thunder running

---

## Architecture

```
War Thunder (running)
  └── Native HTTP API  localhost:8111
        ├── /state          player telemetry
        ├── /indicators     cockpit instruments
        └── /map_obj.json   all units on the map
              │
              └──► asyncio HTTP poller (~10 Hz)
                        │
               ┌────────▼────────┐
               │   FastAPI       │  shared in-memory state
               └────────┬────────┘
                        │
               ┌────────▼────────┐
               │   :8000         │  REST + WebSocket + Radar UI
               └─────────────────┘
```

No scripts. No mods. War Thunder exposes the API natively — just start the game and the poller does the rest.

---

## Quick Start

### Option A — Python

```bash
git clone https://github.com/IsraelSiq/wt-iox-api.git
cd wt-iox-api
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python launcher.py
```

### Option B — Standalone `.exe`

```bash
pip install pyinstaller
python build.py
# Output: dist/wt-iox-api.exe
```

### Option C — Docker

```bash
docker compose up --build
```

---

## War Thunder Setup

No setup required. War Thunder exposes `localhost:8111` automatically while the game is running.

Optional: enable **"Allow localhost connections"** in War Thunder settings if the API doesn't respond.

---

## Radar

Open **`http://localhost:8000/radar`** in your browser.

| Feature | Details |
|---|---|
| Sweep animation | Rotating scan line with trailing glow |
| Contact trails | Last 8 positions per unit |
| Threat alert | Red banner when any enemy is within 20 km |
| Altitude filter | Min/max sliders in feet |
| Coalition colors | Blue = friendly · Red = enemy · Yellow = neutral |
| Labels | Unit name · altitude (ft) · speed (kts) |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Status, uptime, WT connection |
| `GET` | `/state` | Full aircraft state |
| `GET` | `/telemetry` | Position + speed + attitude |
| `GET` | `/contacts` | All detected units |
| `GET` | `/radar` | Tactical radar UI |
| `GET` | `/dashboard` | HUD overlay |
| `WS` | `/ws/telemetry` | Live telemetry stream |
| `WS` | `/ws/contacts` | Live contacts stream |
| `GET` | `/docs` | Swagger UI |

---

## Local Testing (no War Thunder required)

```bash
# Terminal 1 — server
python -m server.main

# Terminal 2 — mock
python tests/mock_wt.py
```

---

## Project Structure

```
wt-iox-api/
├── server/
│   ├── main.py             # HTTP poller + FastAPI entry point
│   ├── api.py              # REST endpoints + WebSocket + radar/dashboard UI
│   ├── models.py           # Pydantic models
│   ├── state.py            # Shared in-memory state
│   └── log_handler.py      # Buffered log handler
├── tests/
│   └── mock_wt.py          # Mock War Thunder HTTP API for local testing
├── launcher.py             # Entry point: starts server + opens browser
├── build.py                # PyInstaller build script
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WT_HOST` | `127.0.0.1` | War Thunder API host |
| `WT_PORT` | `8111` | War Thunder API port |
| `POLL_HZ` | `10` | Polling frequency |

---

## Requirements

- Python 3.10+
- War Thunder (any version with localhost API enabled)

```
fastapi
uvicorn[standard]
pydantic
aiohttp
```

---

## License

MIT
