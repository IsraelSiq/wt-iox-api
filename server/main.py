# server/main.py
# Polls War Thunder localhost:8111 API and serves data via FastAPI
# WT API docs: https://wiki.warthunder.com/Localhost_API
import asyncio
import logging
import math
import os
import time
import datetime
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

_map_size_m: float = 65536.0

# ----------------------------------------------------------------
# Coordinate conversion
# ----------------------------------------------------------------
_map_x_min: float = 0.0
_map_x_max: float = 65536.0
_map_y_min: float = 0.0
_map_y_max: float = 65536.0

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
# Icon -> Category mapping
# Based on actual WT map_obj.json icon values observed in-game.
# WT sends icon as a string; values are case-insensitive matched.
#
# Air icons  : aircraft, fighter, bomber, attacker, aviation,
#              helicopter, heli, plane, jet
# Ground icons: tank, car, truck, spaa, aaa, artillery,
#               armoured, vehicle, ground
# Naval icons : ship, destroyer, cruiser, carrier, boat, naval,
#               frigate, submarine
# ----------------------------------------------------------------
_AIR_ICONS = {
    "aircraft", "fighter", "bomber", "attacker", "aviation",
    "helicopter", "heli", "plane", "jet", "torpedo_bomber",
}
_GROUND_ICONS = {
    "tank", "car", "truck", "spaa", "aaa", "artillery",
    "armoured", "vehicle", "ground", "air_defence",
}
_NAVAL_ICONS = {
    "ship", "destroyer", "cruiser", "carrier", "boat",
    "naval", "frigate", "submarine",
}


def _icon_to_category(icon: str) -> str:
    """Map WT icon string to Air / Ground / Naval. Defaults to Ground."""
    key = icon.lower().strip()
    if key in _AIR_ICONS:
        return "Air"
    if key in _NAVAL_ICONS:
        return "Naval"
    # Partial-match fallback for compound icon names (e.g. "light_tank")
    for air in _AIR_ICONS:
        if air in key:
            return "Air"
    for nav in _NAVAL_ICONS:
        if nav in key:
            return "Naval"
    return "Ground"


# ----------------------------------------------------------------
# Contacts ingestion
# ----------------------------------------------------------------
def _ingest_contacts(raw_objects: list, player_lat: float, player_lon: float):
    new_contacts: dict = {}
    for obj in raw_objects:
        try:
            icon = obj.get("icon", "")
            if icon in ("Player", "Waypoint"):
                continue

            ox = obj.get("x", 0.0)
            oy = obj.get("y", 0.0)
            lat, lon = xy_to_latlon(ox, oy)

            coalition_str = obj.get("color", "neutral").lower()
            coalition = {"blue": 1, "allies": 1, "red": 2, "enemies": 2}.get(coalition_str, 0)

            hdg = float(obj.get("dx", 0.0))
            ddy = float(obj.get("dy", 0.0))
            heading_deg = math.degrees(math.atan2(hdg, ddy)) % 360

            category = _icon_to_category(icon)

            dist_m = haversine(player_lat, player_lon, lat, lon) if (player_lat or player_lon) else 0.0

            uid = str(obj.get("id", f"{ox:.4f}_{oy:.4f}"))
            contact = ContactState(
                id=uid,
                name=obj.get("btype", obj.get("type", "unknown")),
                type=obj.get("btype", obj.get("type", "unknown")),
                category=category,
                lat=lat,
                lon=lon,
                alt_msl_m=float(obj.get("alt", 0)) * 0.3048,
                heading_deg=heading_deg,
                speed_ms=0.0,
                speed_kts=0.0,
                coalition=coalition,
                dist_m=dist_m,
            )
            new_contacts[uid] = contact
        except Exception as e:
            log.debug(f"[contacts] parse error: {e} | {obj}")

    shared.contacts = new_contacts
    shared.contacts_timestamp = time.time()

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
# Main poller loop
# ----------------------------------------------------------------
async def poll_warthunder():
    global _map_x_min, _map_x_max, _map_y_min, _map_y_max, _anchor_lat, _anchor_lon

    interval = 1.0 / POLL_HZ
    log.info(f"[wt-iox] Polling {WT_BASE} at {POLL_HZ}Hz")

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=1.0),
        connector=aiohttp.TCPConnector(limit=4)
    ) as session:

        try:
            async with session.get(URL_MAP_INFO) as r:
                if r.status == 200:
                    info = await r.json(content_type=None)
                    _map_x_min = float(info.get("map_min", [0, 0])[0])
                    _map_y_min = float(info.get("map_min", [0, 0])[1])
                    _map_x_max = float(info.get("map_max", [65536, 65536])[0])
                    _map_y_max = float(info.get("map_max", [65536, 65536])[1])
                    map_name   = info.get("map_name", "").lower()
                    center     = MAP_CENTERS.get(map_name, DEFAULT_CENTER)
                    _anchor_lat, _anchor_lon = center
                    log.info(f"[wt-iox] Map: {map_name}  bounds: {_map_x_min:.0f}-{_map_x_max:.0f} x {_map_y_min:.0f}-{_map_y_max:.0f}")
        except Exception as e:
            log.warning(f"[wt-iox] Could not fetch map_info: {e}")

        while True:
            t0 = time.time()
            shared.poll_count += 1

            raw_objs: list = []
            try:
                async with session.get(URL_MAP_OBJ) as r:
                    if r.status == 200:
                        raw_objs = await r.json(content_type=None)
                        shared.raw_map_obj = raw_objs   # store for /debug/map_obj
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
