# server/state.py
# Shared mutable state — imported by both main.py and api.py
from collections import deque
from server.models import AircraftState, ContactState

latest_state:       AircraftState | None = None
contacts:           dict[str, ContactState] = {}
contacts_timestamp: float = 0.0
contacts_log:       deque  = deque(maxlen=500)
raw_map_obj:        list   = []
static_objects:     list   = []   # fix #9 — airfields, spawn points, zones
map_info:           dict | None = None   # fix #7 — current map metadata
wt_base:            str    = "http://127.0.0.1:8111"   # fix #8 — WT host for proxy
wt_connected:       bool   = False
start_time:         float  = 0.0
poll_count:         int    = 0
log_buffer:         deque  = deque(maxlen=500)
