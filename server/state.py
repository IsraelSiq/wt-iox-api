# server/state.py
# Shared in-process state
import time
from collections import deque
from typing import Optional, Dict, List
from server.models import AircraftState, ContactState

start_time: float = time.time()
latest_state: Optional[AircraftState] = None
contacts: Dict[str, ContactState] = {}
contacts_timestamp: float = 0.0
poll_count: int = 0
wt_connected: bool = False
log_buffer: deque = deque(maxlen=200)
contacts_log: deque = deque(maxlen=500)

# Raw map_obj data from WT (last poll) — used by /debug/map_obj
raw_map_obj: List[dict] = []
