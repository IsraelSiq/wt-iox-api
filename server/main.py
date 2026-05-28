# server/main.py
# Polls War Thunder localhost:8111 API and serves data via FastAPI
# WT API docs: https://wiki.warthunder.com/Localhost_API
import asyncio
import logging
import math
import os
import time
import datetime
from collections import deque
import uvicorn
import aiohttp

from server.log_handler import BufferHandler
from server import state as shared
from server.models import AircraftState, ContactState

# ----------------------------------------------------------------
# Logging
# ----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "iox-api"):
    logging.getLogger(name).addHandler(BufferHandler())

log = logging.getLogger("iox-api")

# ----------------------------------------------------------------
# Config
# ----------------------------------------------------------------
WT_HOST  = os.getenv("WT_HOST", "127.0.0.1")
WT_PORT  = int(os.getenv("WT_PORT", "8111"))
POLL_HZ  = int(os.getenv("POLL_HZ", "10"))

WT_BASE       = f"http://{WT_HOST}:{WT_PORT}"
URL_STATE     = f"{WT_BASE}/state"
URL_MAP_OBJ   = f"{WT_BASE}/map_obj.json"
URL_MAP_INFO  = f"{WT_BASE}/map_info.json"

# Expose WT base URL to api.py (used by /map/image proxy)
shared.wt_base = WT_BASE

_map_size_m: float = 65536.0

# ----------------------------------------------------------------
# Coordinate conversion
# ----------------------------------------------------------------
_map_x_min: float = -32768.0
_map_x_max: float =  32768.0
_map_y_min: float = -32768.0
_map_y_max: float =  32768.0

MAP_CENTERS = {
    "avg_war":  (50.0,  30.0),
    "pacific":  (25.0, 135.0),
    "normandy": (49.3,  -0.7),
    "tunisia":  (36.8,  10.2),
    "korea":    (37.5, 127.0),
    "vietnam":  (21.0, 105.8),
}
DEFAULT_CENTER = (48.0, 15.0)
_anchor_lat: float = DEFAULT_CENTER[0]
_anchor_lon: float = DEFAULT_CENTER[1]


def xy_to_latlon(x_norm: float, y_norm: float) -> tuple[float, float]:
    x_m = _map_x_min + x_norm * (_map_x_max - _map_x_min)
    y_m = _map_y_min + y_norm * (_map_y_max - _map_y_min)
    lat = _anchor_lat + (y_m - (_map_y_max - _map_y_min) / 2) / 111320
    lon = _anchor_lon + (x_m - (_map_x_max - _map_x_min) / 2) / (111320 * math.cos(math.radians(_anchor_lat)))
    return lat, lon


def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000.0
    dl = math.radians(lat2 - lat1)
    dL = math.radians(lon2 - lon1)
    a = math.sin(dl/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dL/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ----------------------------------------------------------------
# Coalition detection
# ----------------------------------------------------------------
_COALITION_MAP: dict[str, int] = {
    "#185aff": 1,
    "#145cff": 1,
    "#4d7aff": 1,
    "#24d921": 1,
    "#fa3200": 2,
    "#f01e00": 2,
    "#ff3200": 2,
}


def _color_to_coalition(color_hex: str) -> int:
    return _COALITION_MAP.get(color_hex.lower().strip(), 0)


# ----------------------------------------------------------------
# Icon -> Category mapping
# ----------------------------------------------------------------
_AIR_ICONS = {
    "aircraft", "fighter", "bomber", "attacker", "aviation",
    "helicopter", "heli", "plane", "jet", "torpedo_bomber",
    "assault",
}
_NAVAL_ICONS = {
    "ship", "destroyer", "cruiser", "carrier", "boat",
    "naval", "frigate", "submarine",
}
_STATIC_ICONS = {
    "airfield", "airbase", "spawn", "spawnpoint", "base",
    "capture_zone", "capture", "zone", "objective",
    "airdrome",
}


def _icon_to_category(icon: str) -> str:
    key = icon.lower().strip()
    if key in _AIR_ICONS:
        return "Air"
    if key in _NAVAL_ICONS:
        return "Naval"
    if key in _STATIC_ICONS:
        return "Static"
    for air in _AIR_ICONS:
        if air in key:
            return "Air"
    for nav in _NAVAL_ICONS:
        if nav in key:
            return "Naval"
    for st in _STATIC_ICONS:
        if st in key:
            return "Static"
    return "Ground"


# ----------------------------------------------------------------
# Sliding-window speed estimator (N=5 samples)
# ----------------------------------------------------------------
SPEED_WINDOW = 5

_speed_windows: dict[str, deque] = {}
_prev_positions: dict[str, tuple[float, float, float]] = {}

_MIN_SPEED_KMH = 0.0


def _smoothed_speed_ms(uid: str, lat: float, lon: float, now: float) -> float | None:
    """Returns None on first observation (no delta yet)."""
    prev = _prev_positions.get(uid)
    if prev is None:
        _prev_positions[uid] = (lat, lon, now)
        _speed_windows.setdefault(uid, deque(maxlen=SPEED_WINDOW))
        return None

    p_lat, p_lon, p_ts = prev
    dt = now - p_ts
    instant = haversine(p_lat, p_lon, lat, lon) / dt if dt > 0 else 0.0

    _prev_positions[uid] = (lat, lon, now)

    win = _speed_windows.setdefault(uid, deque(maxlen=SPEED_WINDOW))
    win.append(instant)

    return sum(win) / len(win)


# ----------------------------------------------------------------
# Contacts ingestion
# ----------------------------------------------------------------
def _ingest_contacts(raw_objects: list, player_lat: float, player_lon: float):
    new_contacts: dict = {}
    new_statics: list = []
    now = time.time()

    for i, obj in enumerate(raw_objects):
        try:
            icon = obj.get("icon", "")
            if icon in ("Player", "Waypoint"):
                continue

            coalition = _color_to_coalition(obj.get("color", ""))

            ox = obj.get("x", 0.0)
            oy = obj.get("y", 0.0)
            lat, lon = xy_to_latlon(ox, oy)

            dx = float(obj.get("dx", 0.0))
            dy = float(obj.get("dy", 0.0))
            heading_deg = math.degrees(math.atan2(dx, -dy)) % 360

            dist_m = haversine(player_lat, player_lon, lat, lon) if (player_lat or player_lon) else 0.0

            obj_id = obj.get("id")
            if obj_id is not None:
                uid = str(obj_id)
            else:
                obj_type  = obj.get("type", "?")
                obj_color = obj.get("color", "?")
                uid = f"{obj_type}_{obj_color}_{i}"

            category = _icon_to_category(icon)

            # --- fix #9: separate static objects from dynamic contacts ---
            if category == "Static":
                new_statics.append({
                    "id":       uid,
                    "name":     obj.get("btype", obj.get("type", "unknown")),
                    "type":     obj.get("btype", obj.get("type", "unknown")),
                    "icon":     icon,
                    "coalition": coalition,
                    "lat":      round(lat, 5),
                    "lon":      round(lon, 5),
                    "x_norm":   ox,
                    "y_norm":   oy,
                })
                continue

            speed_result = _smoothed_speed_ms(uid, lat, lon, now)

            if speed_result is None:
                speed_ms = 0.0
            else:
                speed_ms = speed_result
                if _MIN_SPEED_KMH > 0 and speed_ms * 3.6 < _MIN_SPEED_KMH:
                    continue

            speed_kts = speed_ms * 1.94384

            contact = ContactState(
                id=uid,
                name=obj.get("btype", obj.get("type", "unknown")),
                type=obj.get("btype", obj.get("type", "unknown")),
                category=category,
                lat=lat,
                lon=lon,
                alt_msl_m=float(obj.get("alt", 0)) * 0.3048,
                heading_deg=heading_deg,
                speed_ms=round(speed_ms, 2),
                speed_kts=round(speed_kts, 1),
                coalition=coalition,
                dist_m=dist_m,
            )
            new_contacts[uid] = contact
        except Exception as e:
            log.debug(f"[contacts] parse error: {e} | {obj}")

    # Evict stale tracking data (absent > 30 s)
    active_uids = set(new_contacts.keys())
    stale = [
        k for k in _prev_positions
        if k not in active_uids and (now - _prev_positions[k][2]) > 30
    ]
    for k in stale:
        _prev_positions.pop(k, None)
        _speed_windows.pop(k, None)

    shared.contacts = new_contacts
    shared.contacts_timestamp = time.time()
    shared.static_objects = new_statics  # fix #9

    entry = {
        "received_at": datetime.datetime.now().strftime("%H:%M:%S"),
        "ts":          round(time.time(), 2),
        "count":       len(new_contacts),
        "contacts": [
            {
                "id":          c.id,
                "name":        c.name,
                "type":        c.type,
                "category":    c.category,
                "coalition":   c.coalition,
                "lat":         round(c.lat, 5),
                "lon":         round(c.lon, 5),
                "alt_msl_m":   round(c.alt_msl_m, 1),
                "heading_deg": round(c.heading_deg, 1),
                "speed_kmh":   round(c.speed_ms * 3.6, 1),
                "speed_kts":   round(c.speed_kts, 1),
                "dist_m":      round(c.dist_m, 0),
            }
            for c in new_contacts.values()
        ],
    }
    shared.contacts_log.append(entry)


# ----------------------------------------------------------------
# Extract player position from map_obj Player marker
# ----------------------------------------------------------------
def _extract_player_pos(raw_objects: list) -> tuple[float, float] | None:
    for obj in raw_objects:
        if obj.get("icon") == "Player":
            ox = obj.get("x", None)
            oy = obj.get("y", None)
            if ox is not None and oy is not None:
                return xy_to_latlon(float(ox), float(oy))
    return None


# ----------------------------------------------------------------
# WT state -> AircraftState
# ----------------------------------------------------------------
def _parse_state(data: dict, lat: float = 0.0, lon: float = 0.0) -> AircraftState:
    ias_ms   = float(data.get("IAS, km/h", 0)) / 3.6
    tas_ms   = float(data.get("TAS, km/h", 0)) / 3.6
    alt_m    = float(data.get("altitude_10", data.get("altitude", 0)))
    vvi_ms   = float(data.get("vario", 0))
    hdg      = float(data.get("compass", 0))
    pitch    = float(data.get("pitch", 0))
    bank     = float(data.get("roll", 0))
    aoa      = float(data.get("AoA, deg", 0))
    g_load   = float(data.get("Ny", 1.0))
    throttle = float(data.get("throttle_1", 0))
    rpm_1    = float(data.get("rpm_throttle_1", 0)) * 100
    rpm_2    = float(data.get("rpm_throttle_2", 0)) * 100
    fuel     = float(data.get("fuel_kg", data.get("Mfuel", 0)))

    return AircraftState(
        timestamp=time.time(),
        aircraft=data.get("type", "unknown"),
        lat=lat,
        lon=lon,
        alt_msl_m=alt_m,
        ias_ms=ias_ms,
        tas_ms=tas_ms,
        mach=tas_ms / 340.0,
        vvi_ms=vvi_ms,
        heading_deg=hdg,
        pitch_deg=pitch,
        bank_deg=bank,
        aoa_deg=aoa,
        g_load=g_load,
        throttle=throttle,
        rpm_1=rpm_1,
        rpm_2=rpm_2,
        fuel_kg=fuel,
    )


# ----------------------------------------------------------------
# fix #7 — map_info re-fetch helper
# ----------------------------------------------------------------
async def _fetch_map_info(session: aiohttp.ClientSession) -> str | None:
    """Fetches map_info.json, updates global map bounds/anchor.
    Returns the map_name string, or None on failure."""
    global _map_x_min, _map_x_max, _map_y_min, _map_y_max, _anchor_lat, _anchor_lon
    try:
        async with session.get(URL_MAP_INFO) as r:
            if r.status != 200:
                return None
            info = await r.json(content_type=None)
            _map_x_min = float(info.get("map_min", ["-32768.0", "-32768.0"])[0])
            _map_y_min = float(info.get("map_min", ["-32768.0", "-32768.0"])[1])
            _map_x_max = float(info.get("map_max", ["32768.0",  "32768.0"])[0])
            _map_y_max = float(info.get("map_max", ["32768.0",  "32768.0"])[1])
            map_name   = info.get("map_name", "").lower()
            center     = MAP_CENTERS.get(map_name, DEFAULT_CENTER)
            _anchor_lat, _anchor_lon = center

            # Expose current map info to api.py
            shared.map_info = {
                "map_name":   map_name,
                "map_generation": info.get("map_generation"),
                "map_min":    [_map_x_min, _map_y_min],
                "map_max":    [_map_x_max, _map_y_max],
                "anchor_lat": _anchor_lat,
                "anchor_lon": _anchor_lon,
            }

            log.info(
                f"[wt-iox] Map: {map_name}  gen={info.get('map_generation')}  "
                f"bounds: X[{_map_x_min:.0f}, {_map_x_max:.0f}]  "
                f"Y[{_map_y_min:.0f}, {_map_y_max:.0f}]"
            )
            return map_name
    except Exception as e:
        log.warning(f"[wt-iox] Could not fetch map_info: {e}")
        return None


# ----------------------------------------------------------------
# Main poller loop
# ----------------------------------------------------------------
async def poll_warthunder():
    interval = 1.0 / POLL_HZ
    log.info(f"[wt-iox] Polling {WT_BASE} at {POLL_HZ}Hz  (speed window: {SPEED_WINDOW} samples)")

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=1.0),
        connector=aiohttp.TCPConnector(limit=4)
    ) as session:

        # Initial map_info fetch
        await _fetch_map_info(session)
        last_generation = (shared.map_info or {}).get("map_generation")

        while True:
            t0 = time.time()
            shared.poll_count += 1

            # fix #7: re-fetch map_info when map_generation changes
            try:
                async with session.get(URL_MAP_INFO) as r:
                    if r.status == 200:
                        info_check = await r.json(content_type=None)
                        cur_gen = info_check.get("map_generation")
                        if cur_gen != last_generation:
                            log.info(f"[wt-iox] map_generation changed {last_generation} -> {cur_gen}, re-fetching map_info")
                            await _fetch_map_info(session)
                            last_generation = cur_gen
                            # Clear stale tracking data on map change
                            _prev_positions.clear()
                            _speed_windows.clear()
                            shared.contacts.clear()
                            shared.static_objects = []
            except Exception:
                pass

            raw_objs: list = []
            try:
                async with session.get(URL_MAP_OBJ) as r:
                    if r.status == 200:
                        raw_objs = await r.json(content_type=None)
                        shared.raw_map_obj = raw_objs
            except Exception:
                pass

            player_pos = _extract_player_pos(raw_objs)
            if player_pos:
                player_lat, player_lon = player_pos
            elif shared.latest_state:
                player_lat = shared.latest_state.lat
                player_lon = shared.latest_state.lon
            else:
                player_lat, player_lon = 0.0, 0.0

            try:
                async with session.get(URL_STATE) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        shared.latest_state = _parse_state(data, lat=player_lat, lon=player_lon)
                        shared.wt_connected = True
                    else:
                        shared.wt_connected = False
            except Exception:
                shared.wt_connected = False

            if raw_objs:
                _ingest_contacts(raw_objs, player_lat, player_lon)

            elapsed = time.time() - t0
            await asyncio.sleep(max(0, interval - elapsed))


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------
async def main():
    shared.start_time = time.time()

    poller = asyncio.create_task(poll_warthunder())

    config = uvicorn.Config(
        "server.api:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None

    try:
        await server.serve()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutdown signal received")
    finally:
        poller.cancel()
        log.info("[wt-iox] Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
