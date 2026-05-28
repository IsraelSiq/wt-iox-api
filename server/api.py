# server/api.py
# REST endpoints + WebSocket + Radar UI + Dashboard HUD
# Adapted from dcs-iox-api for War Thunder
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from contextlib import asynccontextmanager
import asyncio
import json
import logging
import time
import aiohttp

from server.models import AircraftState, ContactsPacket
from server import state as shared

log = logging.getLogger("iox-api")


# ----------------------------------------------------------------
# WebSocket connection manager
# ----------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        log.info(f"WS client connected. Total: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        log.info(f"WS client disconnected. Total: {len(self.active)}")

    async def broadcast(self, data: str):
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.active:
                self.active.remove(ws)


manager_telem = ConnectionManager()   # /ws/telemetry
manager_radar = ConnectionManager()   # /ws/radar


# ----------------------------------------------------------------
# Background broadcast loops
# ----------------------------------------------------------------
async def broadcast_telemetry():
    log.info("Telemetry WS broadcast loop started")
    while True:
        await asyncio.sleep(1 / 10)   # WT polls at ~10 Hz
        if shared.latest_state and manager_telem.active:
            await manager_telem.broadcast(shared.latest_state.model_dump_json())


async def broadcast_radar():
    """Broadcasts combined self+contacts frame at 5 Hz to radar clients."""
    log.info("Radar WS broadcast loop started")
    while True:
        await asyncio.sleep(1 / 5)
        if not manager_radar.active:
            continue
        frame = {
            "self":     shared.latest_state.model_dump() if shared.latest_state else None,
            "contacts": [c.model_dump() for c in shared.contacts.values()],
            "ts":       time.time(),
        }
        await manager_radar.broadcast(json.dumps(frame))


# ----------------------------------------------------------------
# App lifespan
# ----------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    t1 = asyncio.create_task(broadcast_telemetry())
    t2 = asyncio.create_task(broadcast_radar())
    yield
    for t in (t1, t2):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


# ----------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------
app = FastAPI(
    title="wt-iox-api",
    description="War Thunder IOX API — localhost:8111 poller + REST/WebSocket server",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------
# Root
# ----------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/radar")


# ----------------------------------------------------------------
# REST endpoints
# ----------------------------------------------------------------
@app.get("/health", tags=["System"])
async def health():
    return {
        "status":         "ok",
        "uptime":         time.time() - shared.start_time,
        "polls":          shared.poll_count,
        "wt_connected":   shared.wt_connected,
        "contacts_count": len(shared.contacts),
    }


@app.get("/logs", tags=["System"])
async def get_logs(n: int = 50):
    entries = list(shared.log_buffer)
    return {"count": len(entries), "logs": entries[-min(n, 200):]}


@app.get("/state", response_model=AircraftState, tags=["Telemetry"])
async def get_state():
    if shared.latest_state is None:
        raise HTTPException(status_code=503, detail="No data from War Thunder yet. Is the game running?")
    return shared.latest_state


@app.get("/telemetry", tags=["Telemetry"])
async def get_telemetry():
    if shared.latest_state is None:
        raise HTTPException(status_code=503, detail="No data from War Thunder yet. Is the game running?")
    s = shared.latest_state
    return {
        "aircraft":  s.aircraft,
        "timestamp": s.timestamp,
        "position":  {"lat": s.lat, "lon": s.lon, "alt_msl_m": s.alt_msl_m},
        "speed":     {"ias_ms": s.ias_ms, "ias_kts": round(s.ias_ms * 1.944, 1), "tas_ms": s.tas_ms, "mach": s.mach, "vvi_ms": s.vvi_ms},
        "attitude":  {"heading_deg": s.heading_deg, "pitch_deg": s.pitch_deg, "bank_deg": s.bank_deg, "aoa_deg": s.aoa_deg},
    }


@app.get("/contacts", tags=["Radar"])
async def get_contacts():
    contacts = sorted(shared.contacts.values(), key=lambda c: c.dist_m)
    return {
        "count":     len(contacts),
        "timestamp": shared.contacts_timestamp,
        "contacts":  [c.model_dump() for c in contacts],
    }


# ----------------------------------------------------------------
# Map — GeoJSON tactical picture
# ----------------------------------------------------------------
@app.get("/map", tags=["Map"])
async def get_map():
    """Returns a GeoJSON FeatureCollection with own-ship + all contacts.

    Each Feature carries the full contact/state payload as ``properties``.
    Coalition values: 1 = allies, 2 = enemies, 0/None = neutral/unknown.
    """
    features: list[dict] = []

    # Own ship
    if shared.latest_state:
        s = shared.latest_state
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [s.lon, s.lat],
            },
            "properties": {
                "id":          "self",
                "role":        "self",
                "name":        s.aircraft or "own-ship",
                "alt_msl_m":   s.alt_msl_m,
                "heading_deg": s.heading_deg,
                "speed_ms":    s.ias_ms,
                "coalition":   None,
                "timestamp":   s.timestamp,
            },
        })

    # Contacts
    for c in shared.contacts.values():
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [c.lon, c.lat],
            },
            "properties": {
                "id":          c.id,
                "role":        "contact",
                "name":        c.name,
                "type":        c.type,
                "category":    c.category,
                "coalition":   c.coalition,
                "alt_msl_m":   c.alt_msl_m,
                "heading_deg": c.heading_deg,
                "speed_ms":    c.speed_ms,
                "dist_m":      c.dist_m,
            },
        })

    return {
        "type":      "FeatureCollection",
        "timestamp": time.time(),
        "count":     len(features),
        "features":  features,
    }


# ----------------------------------------------------------------
# fix #8 — Map image proxy
# Proxies GET /map.img from WT localhost:8111 so browser clients
# (served from a different port) can load the map tile without
# running into CORS restrictions.
# ----------------------------------------------------------------
@app.get("/map/image", tags=["Map"])
async def map_image():
    """Proxies the War Thunder map image (map.img) from localhost:8111.

    Returns the raw PNG/JPEG bytes with the original Content-Type.
    Responds with 503 if WT is not reachable or returns a non-200 status.
    """
    url = f"{shared.wt_base}/map.img"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=3.0)
        ) as session:
            async with session.get(url) as r:
                if r.status != 200:
                    raise HTTPException(
                        status_code=503,
                        detail=f"WT returned HTTP {r.status} for map.img",
                    )
                content_type = r.headers.get("Content-Type", "image/jpeg")
                data = await r.read()
                return Response(content=data, media_type=content_type)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Could not reach War Thunder map image: {exc}",
        )


# ----------------------------------------------------------------
# fix #9 — Static map objects
# Returns airfields, spawn points, capture zones and other objects
# that don't move between frames (category == "Static").
# These are separated from /contacts so radar clients don't have
# to filter them out on every frame.
# ----------------------------------------------------------------
@app.get("/map/static", tags=["Map"])
async def get_static_objects():
    """Returns static map objects: airfields, spawn points, capture zones.

    These are polled from map_obj.json but separated from dynamic contacts
    because they don't move — clients can cache them for the duration of
    a mission and only re-fetch when ``/map/info`` reports a new
    ``map_generation``.
    """
    return {
        "count":     len(shared.static_objects),
        "timestamp": shared.contacts_timestamp,
        "objects":   shared.static_objects,
    }


# ----------------------------------------------------------------
# Map info — exposes current map metadata (name, generation, bounds)
# ----------------------------------------------------------------
@app.get("/map/info", tags=["Map"])
async def get_map_info():
    """Returns current map metadata: name, generation counter, and bounds.

    Clients can poll this endpoint and compare ``map_generation`` to detect
    when the map changes, then re-fetch ``/map/image`` and ``/map/static``
    accordingly.
    """
    if shared.map_info is None:
        raise HTTPException(status_code=503, detail="Map info not available yet.")
    return shared.map_info


# ----------------------------------------------------------------
# Debug endpoint — raw category/type inspection
# ----------------------------------------------------------------
@app.get("/debug/raw_contacts", tags=["Debug"])
async def debug_raw_contacts():
    """Returns raw 'type' and 'category' fields for each contact.
    Use this in-mission to see exactly what strings WT sends,
    so the radar category filter can be tuned accordingly."""
    return [
        {
            "id":       c.id,
            "name":     c.name,
            "type":     c.type,
            "category": c.category,
            "coalition": c.coalition,
        }
        for c in shared.contacts.values()
    ]


@app.get("/debug/map_obj", tags=["Debug"])
async def debug_map_obj():
    """Returns the raw list last received from WT /map_obj endpoint.
    Useful for inspecting new field names or unexpected object types."""
    return {
        "count":   len(shared.raw_map_obj),
        "objects": shared.raw_map_obj,
    }


# ----------------------------------------------------------------
# Logs view
# ----------------------------------------------------------------
@app.get("/logs/view", response_class=HTMLResponse, tags=["System"], include_in_schema=False)
async def logs_view():
    html = """
<!DOCTYPE html><html lang="pt-BR"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>wt-iox-api | Logs</title>
<style>
  :root{--bg:#171614;--surface:#1c1b19;--border:#393836;--text:#cdccca;--text-muted:#797876;--primary:#4f98a3;--radius:6px;--font:'Fira Code','Cascadia Code','Consolas',monospace;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:13px;min-height:100vh;padding:24px}
  header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px}
  .logo{display:flex;align-items:center;gap:10px}
  .logo h1{font-size:16px;font-weight:600;letter-spacing:.05em;color:var(--primary)}
  .logo span{font-size:12px;color:var(--text-muted)}
  .badge{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);font-size:12px;color:var(--text-muted)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--primary);animation:pulse 2s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
  .toolbar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
  .toolbar label{color:var(--text-muted);font-size:12px}
  select,input[type=number]{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:var(--radius);font-family:var(--font);font-size:12px}
  .btn{padding:4px 12px;background:var(--primary);color:#171614;border:none;border-radius:var(--radius);cursor:pointer;font-size:12px;font-weight:600;font-family:var(--font);transition:opacity .15s}
  .btn:hover{opacity:.85}
  #log-box{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px;height:calc(100vh - 180px);overflow-y:auto;display:flex;flex-direction:column;gap:2px}
  .log-line{display:grid;grid-template-columns:70px 70px 1fr;gap:12px;padding:3px 0;border-bottom:1px solid rgba(255,255,255,.03);line-height:1.5}
  .log-line:last-child{border-bottom:none}
  .ts{color:var(--text-muted)}.lvl{font-weight:700}.msg{word-break:break-all}
  .empty{color:var(--text-muted);text-align:center;padding:40px;font-style:italic}
</style></head><body>
<header>
  <div class="logo">
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:var(--primary)"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
    <div><h1>wt-iox-api</h1><span>Live Server Logs</span></div>
  </div>
  <div style="display:flex;gap:8px;align-items:center">
    <div class="badge"><div class="dot"></div><span id="status-text">conectando...</span></div>
    <span id="countdown" class="badge">next refresh: 10s</span>
  </div>
</header>
<div class="toolbar">
  <label>Linhas:</label><input type="number" id="n-lines" value="50" min="10" max="200" style="width:70px">
  <label>Filtro:</label>
  <select id="filter-level"><option value="ALL">Todos</option><option>INFO</option><option>WARNING</option><option>ERROR</option><option>DEBUG</option></select>
  <button class="btn" onclick="fetchLogs()">&#8635; Atualizar</button>
  <button class="btn" onclick="clearView()" style="background:#393836;color:var(--text)">Limpar</button>
</div>
<div id="log-box"><div class="empty">Aguardando logs...</div></div>
<script>
  let countdown=10,timer;
  async function fetchLogs(){
    const n=document.getElementById('n-lines').value||50;
    const level=document.getElementById('filter-level').value;
    try{
      const res=await fetch('/logs?n='+n);
      const data=await res.json();
      renderLogs(data.logs,level);
      document.getElementById('status-text').textContent=data.count+' entradas | '+new Date().toLocaleTimeString('pt-BR');
    }catch(e){document.getElementById('status-text').textContent='erro';}
    resetCountdown();
  }
  function renderLogs(logs,level){
    const box=document.getElementById('log-box');
    const f=level==='ALL'?logs:logs.filter(l=>l.level===level);
    if(!f.length){box.innerHTML='<div class="empty">Nenhum log.</div>';return;}
    box.innerHTML=f.map(l=>'<div class="log-line"><span class="ts">'+l.ts+'</span><span class="lvl" style="color:'+l.color+'">'+l.level+'</span><span class="msg">'+l.message.replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</span></div>').join('');
    box.scrollTop=box.scrollHeight;
  }
  function clearView(){document.getElementById('log-box').innerHTML='<div class="empty">View limpa.</div>';}
  function resetCountdown(){
    clearInterval(timer);countdown=10;
    document.getElementById('countdown').textContent='next refresh: '+countdown+'s';
    timer=setInterval(()=>{countdown--;document.getElementById('countdown').textContent='next refresh: '+countdown+'s';if(countdown<=0)fetchLogs();},1000);
  }
  fetchLogs();
</script></body></html>
    """
    return HTMLResponse(content=html)


# ----------------------------------------------------------------
# Dashboard — Live Cockpit HUD (War Thunder)
# ----------------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse, tags=["System"], include_in_schema=False)
async def dashboard():
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WT IOX — Live HUD</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700&display=swap');
  :root{--bg:#0a0c0a;--panel:#0d110d;--border:#1a2a1a;--green:#39ff6e;--green-dim:#1a7a35;--amber:#ffb830;--red:#ff4040;--blue:#40c8ff;--text:#c8e8c8;--muted:#4a6a4a;--font-mono:'Share Tech Mono',monospace;--font-hud:'Orbitron',sans-serif;--glow:0 0 8px rgba(57,255,110,0.35);}
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:var(--font-mono);font-size:13px;min-height:100vh;display:flex;flex-direction:column;overflow-x:hidden}
  #topbar{display:flex;align-items:center;justify-content:space-between;padding:8px 20px;border-bottom:1px solid var(--border);background:var(--panel);flex-shrink:0}
  #topbar .logo{font-family:var(--font-hud);font-size:14px;font-weight:700;color:var(--green);letter-spacing:.15em;text-shadow:var(--glow)}
  #topbar .logo span{color:var(--muted);font-weight:400;font-size:11px;margin-left:8px}
  #ws-status{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--muted)}
  #ws-dot{width:8px;height:8px;border-radius:50%;background:var(--muted);transition:background .3s}
  #ws-dot.live{background:var(--green);box-shadow:var(--glow);animation:blink 2s infinite}
  #ws-dot.error{background:var(--red)}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.4}}
  #aircraft-id{font-family:var(--font-hud);font-size:12px;color:var(--amber);letter-spacing:.1em}
  #main{flex:1;display:grid;grid-template-columns:200px 1fr 200px;grid-template-rows:1fr auto;gap:1px;background:var(--border);min-height:0}
  .panel{background:var(--panel);display:flex;flex-direction:column;align-items:center;justify-content:center;padding:16px 12px;gap:16px}
  #center-panel{background:var(--bg);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:20px;padding:20px}
  canvas{display:block}
  .gauge-block{width:100%;display:flex;flex-direction:column;align-items:center;gap:4px}
  .gauge-label{font-size:10px;color:var(--muted);letter-spacing:.12em;text-transform:uppercase}
  .gauge-value{font-family:var(--font-hud);font-size:22px;font-weight:700;color:var(--green);text-shadow:var(--glow);line-height:1;transition:color .2s}
  .gauge-unit{font-size:10px;color:var(--muted)}
  .gauge-bar{width:100%;height:4px;background:var(--border);border-radius:2px;overflow:hidden}
  .gauge-bar-fill{height:100%;background:var(--green);border-radius:2px;transition:width .1s linear,background .2s}
  #heading-tape-wrap{width:300px;height:36px;background:#0a140a;border:1px solid var(--border);border-radius:4px;overflow:hidden;position:relative}
  #hdg-bug{position:absolute;top:0;left:50%;transform:translateX(-50%);width:2px;height:36px;background:var(--amber);pointer-events:none}
  #vsi-wrap{display:flex;flex-direction:column;align-items:center;gap:4px}
  #vsi-arrow{font-size:20px;line-height:1;transition:transform .15s,color .15s}
  #bottombar{grid-column:1/-1;background:var(--panel);border-top:1px solid var(--border);display:flex;align-items:center;height:40px;overflow:hidden}
  .stat-cell{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;padding:0 12px;border-right:1px solid var(--border);height:100%}
  .stat-cell:last-child{border-right:none}
  .stat-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
  .stat-val{font-family:var(--font-hud);font-size:13px;color:var(--green)}
  .divider{width:80%;height:1px;background:var(--border)}
  #offline{display:none;position:fixed;inset:0;background:rgba(10,12,10,.88);z-index:100;align-items:center;justify-content:center;flex-direction:column;gap:16px;font-family:var(--font-hud);color:var(--red);font-size:18px;letter-spacing:.1em;text-align:center}
  #offline.show{display:flex}
  #offline small{font-family:var(--font-mono);font-size:12px;color:var(--muted)}
  #retry-btn{margin-top:8px;padding:8px 24px;background:none;border:1px solid var(--red);color:var(--red);font-family:var(--font-hud);font-size:12px;cursor:pointer;letter-spacing:.1em;transition:background .2s}
  #retry-btn:hover{background:rgba(255,64,64,.15)}
</style>
</head><body>
<div id="offline"><div>&#9888; NO WAR THUNDER SIGNAL</div><small id="offline-msg">WebSocket disconnected</small><button id="retry-btn" onclick="initWS()">RECONNECT</button></div>
<div id="topbar"><div class="logo">WT IOX<span>LIVE HUD v0.1</span></div><div id="aircraft-id">—</div><div id="ws-status"><div id="ws-dot"></div><span id="ws-label">OFFLINE</span></div></div>
<div id="main">
  <div class="panel">
    <div class="gauge-block"><div class="gauge-label">IAS</div><div class="gauge-value" id="ias-val">0</div><div class="gauge-unit">km/h</div><div class="gauge-bar"><div class="gauge-bar-fill" id="ias-bar" style="width:0%"></div></div></div>
    <div class="divider"></div>
    <div class="gauge-block"><div class="gauge-label">MACH</div><div class="gauge-value" id="mach-val">0.00</div></div>
    <div class="divider"></div>
    <div class="gauge-block"><div class="gauge-label">AoA</div><div class="gauge-value" id="aoa-val">0.0</div><div class="gauge-unit">deg</div></div>
    <div class="divider"></div>
    <div class="gauge-block"><div class="gauge-label">G-FORCE</div><div class="gauge-value" id="g-val">1.0</div><div class="gauge-unit">g</div></div>
  </div>
  <div id="center-panel">
    <canvas id="adi-canvas" width="260" height="260"></canvas>
    <div id="heading-tape-wrap"><canvas id="heading-tape-canvas" width="300" height="36"></canvas><div id="hdg-bug"></div></div>
    <div id="vsi-wrap"><div class="gauge-label">VERTICAL SPEED</div><div id="vsi-arrow" style="color:var(--green)">&#9654;</div><div class="gauge-value" id="vsi-val" style="font-size:18px">+0</div><div class="gauge-unit">m/min</div></div>
  </div>
  <div class="panel">
    <div class="gauge-block"><div class="gauge-label">ALT MSL</div><div class="gauge-value" id="alt-val">0</div><div class="gauge-unit">meters</div><div class="gauge-bar"><div class="gauge-bar-fill" id="alt-bar" style="width:0%"></div></div></div>
    <div class="divider"></div>
    <div class="gauge-block"><div class="gauge-label">FUEL</div><div class="gauge-value" id="fuel-val">—</div><div class="gauge-unit">kg</div><div class="gauge-bar"><div class="gauge-bar-fill" id="fuel-bar" style="width:0%"></div></div></div>
    <div class="divider"></div>
    <div class="gauge-block"><div class="gauge-label">THROTTLE</div><div class="gauge-value" id="thr-val">—</div><div class="gauge-unit">%</div></div>
    <div class="divider"></div>
    <div class="gauge-block"><div class="gauge-label">RPM</div><div class="gauge-value" id="rpm-val">—</div><div class="gauge-unit">%</div></div>
  </div>
  <div id="bottombar">
    <div class="stat-cell"><span class="stat-lbl">HDG</span><span class="stat-val" id="hdg-val">---°</span></div>
    <div class="stat-cell"><span class="stat-lbl">PITCH</span><span class="stat-val" id="pitch-val">---°</span></div>
    <div class="stat-cell"><span class="stat-lbl">BANK</span><span class="stat-val" id="bank-val">---°</span></div>
    <div class="stat-cell"><span class="stat-lbl">LAT</span><span class="stat-val" id="lat-val">---.----</span></div>
    <div class="stat-cell"><span class="stat-lbl">LON</span><span class="stat-val" id="lon-val">---.----</span></div>
    <div class="stat-cell"><span class="stat-lbl">POLLS</span><span class="stat-val" id="pkt-val">0</span></div>
    <div class="stat-cell"><span class="stat-lbl">FPS</span><span class="stat-val" id="fps-val">--</span></div>
  </div>
</div>
<script>
"use strict";
let ws=null,reconnectTimer=null,packetCount=0,fpsCount=0,lastFpsTime=performance.now();
const adiCanvas=document.getElementById('adi-canvas'),adiCtx=adiCanvas.getContext('2d');
const ADI_CX=130,ADI_CY=130,ADI_R=120;
function drawADI(pitch,bank){
  const ctx=adiCtx;ctx.clearRect(0,0,260,260);
  ctx.save();ctx.translate(ADI_CX,ADI_CY);ctx.rotate(bank*Math.PI/180);
  ctx.beginPath();ctx.arc(0,0,ADI_R,0,Math.PI*2);ctx.clip();
  const pitchPx=pitch*3.5;
  const skyGrad=ctx.createLinearGradient(0,-ADI_R+pitchPx,0,pitchPx);
  skyGrad.addColorStop(0,'#0a1a2e');skyGrad.addColorStop(1,'#0d2a4a');
  ctx.fillStyle=skyGrad;ctx.fillRect(-ADI_R,-ADI_R+pitchPx,ADI_R*2,ADI_R*2);
  const gndGrad=ctx.createLinearGradient(0,pitchPx,0,ADI_R+pitchPx);
  gndGrad.addColorStop(0,'#2a1a08');gndGrad.addColorStop(1,'#1a0e04');
  ctx.fillStyle=gndGrad;ctx.fillRect(-ADI_R,pitchPx,ADI_R*2,ADI_R*2);
  ctx.strokeStyle='#ffffff';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(-ADI_R,pitchPx);ctx.lineTo(ADI_R,pitchPx);ctx.stroke();
  ctx.strokeStyle='rgba(255,255,255,.7)';ctx.fillStyle='rgba(255,255,255,.7)';ctx.font='10px Share Tech Mono';ctx.textAlign='right';ctx.lineWidth=1;
  for(let p=-30;p<=30;p+=5){if(p===0)continue;const y=pitchPx-p*3.5;const w=(Math.abs(p)%10===0)?40:20;ctx.beginPath();ctx.moveTo(-w,y);ctx.lineTo(w,y);ctx.stroke();if(Math.abs(p)%10===0)ctx.fillText(p.toString(),-w-4,y+4);}
  ctx.restore();
  ctx.beginPath();ctx.arc(ADI_CX,ADI_CY,ADI_R,0,Math.PI*2);ctx.strokeStyle='#1a3a1a';ctx.lineWidth=3;ctx.stroke();
  ctx.save();ctx.translate(ADI_CX,ADI_CY);
  ctx.strokeStyle='#4a6a4a';ctx.lineWidth=1;
  for(const a of[-60,-45,-30,-20,-10,0,10,20,30,45,60]){const r=(a-90)*Math.PI/180;ctx.beginPath();ctx.moveTo(Math.cos(r)*(ADI_R-14),Math.sin(r)*(ADI_R-14));ctx.lineTo(Math.cos(r)*(ADI_R-6),Math.sin(r)*(ADI_R-6));ctx.stroke();}
  ctx.rotate(bank*Math.PI/180);ctx.fillStyle='#39ff6e';ctx.beginPath();ctx.moveTo(0,-(ADI_R-14));ctx.lineTo(-5,-(ADI_R-4));ctx.lineTo(0,2);ctx.lineTo(5,-(ADI_R-4));ctx.closePath();ctx.fill();
  ctx.restore();
  ctx.save();ctx.translate(ADI_CX,ADI_CY);ctx.strokeStyle='#ffb830';ctx.lineWidth=2.5;ctx.lineCap='round';
  ctx.beginPath();ctx.moveTo(-50,0);ctx.lineTo(-10,0);ctx.moveTo(10,0);ctx.lineTo(50,0);ctx.stroke();
  ctx.beginPath();ctx.moveTo(0,-6);ctx.lineTo(0,6);ctx.stroke();
  ctx.beginPath();ctx.moveTo(-50,0);ctx.lineTo(-45,-6);ctx.moveTo(50,0);ctx.lineTo(45,-6);ctx.stroke();
  ctx.restore();
}
const hdgCanvas=document.getElementById('heading-tape-canvas'),hdgCtx=hdgCanvas.getContext('2d');
function drawHeadingTape(hdg){
  const ctx=hdgCtx,W=300,H=36;ctx.clearRect(0,0,W,H);ctx.fillStyle='#0a140a';ctx.fillRect(0,0,W,H);
  const pxPerDeg=5,halfW=W/2;ctx.font='10px Share Tech Mono';ctx.textAlign='center';
  for(let d=-30;d<=30;d++){const deg=((hdg+d)%360+360)%360;const x=halfW+d*pxPerDeg;
    if(deg%10===0){ctx.fillStyle='#4a6a4a';ctx.fillRect(x-.5,0,1,12);const label=deg===0?'N':deg===90?'E':deg===180?'S':deg===270?'W':deg.toString();ctx.fillStyle=(deg%90===0)?'#39ff6e':'#6a9a6a';ctx.fillText(label,x,26);}
    else if(deg%5===0){ctx.fillStyle='#2a3a2a';ctx.fillRect(x-.5,0,1,6);}}
  ctx.fillStyle='#ffb830';ctx.fillRect(halfW-1,0,2,H);
}
const $=id=>document.getElementById(id);
function setVal(id,v){const el=$(id);if(el)el.textContent=v;}
function setBar(id,pct,warn,danger){const el=$(id);if(!el)return;const p=Math.min(100,Math.max(0,pct));el.style.width=p+'%';el.style.background=p>=danger?'var(--red)':p>=warn?'var(--amber)':'var(--green)';}
function setColor(id,c){const el=$(id);if(el)el.style.color=c;}
function updateFPS(){fpsCount++;const now=performance.now();if(now-lastFpsTime>=1000){setVal('fps-val',fpsCount.toString());fpsCount=0;lastFpsTime=now;}}
function updateHUD(s){
  $('aircraft-id').textContent=(s.aircraft||'UNKNOWN').toUpperCase();
  const ias=Math.round((s.ias_ms||0)*3.6);setVal('ias-val',ias);setBar('ias-bar',ias/900*100,70,90);
  setColor('ias-val',ias>800?'var(--red)':ias>650?'var(--amber)':'var(--green)');
  setVal('mach-val',(s.mach||0).toFixed(2));setColor('mach-val',(s.mach||0)>1.0?'var(--amber)':'var(--green)');
  setVal('aoa-val',(s.aoa_deg||0).toFixed(1));setColor('aoa-val',Math.abs(s.aoa_deg||0)>20?'var(--red)':'var(--green)');
  setVal('g-val',(s.g_load!=null?s.g_load:1).toFixed(1));
  const altM=Math.round(s.alt_msl_m||0);
  setVal('alt-val',altM.toLocaleString());setBar('alt-bar',altM/15000*100,70,90);
  setColor('alt-val',altM<100?'var(--red)':altM<300?'var(--amber)':'var(--green)');
  if(s.fuel_kg!=null){setVal('fuel-val',Math.round(s.fuel_kg).toLocaleString());setBar('fuel-bar',Math.min(100,s.fuel_kg/500*100),30,15);}
  if(s.throttle!=null)setVal('thr-val',Math.round(s.throttle*100));
  const rpm=s.rpm_1||s.rpm_2;if(rpm)setVal('rpm-val',Math.round(rpm));
  const vviMs=s.vvi_ms||0;const vsimpm=Math.round(vviMs*60);
  setVal('vsi-val',(vsimpm>=0?'+':'')+vsimpm.toLocaleString());
  const vsiEl=$('vsi-arrow');if(vsimpm>50){vsiEl.textContent='\u25b2';vsiEl.style.color='var(--green)';}else if(vsimpm<-50){vsiEl.textContent='\u25bc';vsiEl.style.color='var(--red)';}else{vsiEl.textContent='\u25b6';vsiEl.style.color='var(--muted)';}
  const hdg=s.heading_deg||0;
  setVal('hdg-val',Math.round(hdg)+'\u00b0');setVal('pitch-val',(s.pitch_deg||0).toFixed(1)+'\u00b0');setVal('bank-val',(s.bank_deg||0).toFixed(1)+'\u00b0');
  setVal('lat-val',(s.lat||0).toFixed(4));setVal('lon-val',(s.lon||0).toFixed(4));setVal('pkt-val',packetCount.toLocaleString());
  drawADI(s.pitch_deg||0,s.bank_deg||0);drawHeadingTape(hdg);updateFPS();
}
function setWsStatus(status){
  const dot=$('ws-dot'),lbl=$('ws-label');dot.className='';
  if(status==='live'){dot.classList.add('live');lbl.textContent='LIVE';lbl.style.color='var(--green)';$('offline').classList.remove('show');}
  else if(status==='connecting'){lbl.textContent='CONNECTING';lbl.style.color='var(--amber)';}
  else{dot.classList.add('error');lbl.textContent='OFFLINE';lbl.style.color='var(--red)';$('offline').classList.add('show');}
}
function initWS(){
  clearTimeout(reconnectTimer);if(ws){try{ws.close();}catch(e){}}ws=null;
  setWsStatus('connecting');$('offline-msg').textContent='Connecting...';
  const proto=location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(proto+'//'+location.host+'/ws/telemetry');
  ws.onopen=()=>setWsStatus('live');
  ws.onmessage=(evt)=>{try{const s=JSON.parse(evt.data);packetCount++;updateHUD(s);}catch(e){}};
  ws.onerror=()=>{};
  ws.onclose=(evt)=>{setWsStatus('offline');$('offline-msg').textContent='Disconnected ('+evt.code+'). Retry in 3s...';reconnectTimer=setTimeout(initWS,3000);};
}
drawADI(0,0);drawHeadingTape(0);initWS();
</script></body></html>"""
    return HTMLResponse(content=html)


# ----------------------------------------------------------------
# WebSocket endpoints
# ----------------------------------------------------------------
@app.websocket("/ws/telemetry")
async def ws_telemetry(websocket: WebSocket):
    await manager_telem.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager_telem.disconnect(websocket)


@app.websocket("/ws/radar")
async def ws_radar(websocket: WebSocket):
    await manager_radar.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager_radar.disconnect(websocket)
