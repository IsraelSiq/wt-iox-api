# server/models.py
from pydantic import BaseModel, Field, model_validator
from typing import Optional, List
import time

COALITION_MAP = {"neutral": 0, "allies": 1, "enemies": 2}


class AircraftState(BaseModel):
    timestamp:    float = Field(0.0)
    aircraft:     str   = Field("unknown")
    received_at:  float = Field(default_factory=time.time)
    lat:          float = Field(0.0)
    lon:          float = Field(0.0)
    alt_msl_m:    float = Field(0.0)
    alt_agl_m:    float = Field(0.0)
    ias_ms:       float = Field(0.0)
    tas_ms:       float = Field(0.0)
    mach:         float = Field(0.0)
    vvi_ms:       float = Field(0.0)
    heading_deg:  float = Field(0.0)
    pitch_deg:    float = Field(0.0)
    bank_deg:     float = Field(0.0)
    aoa_deg:      float = Field(0.0)
    fuel_kg:      float = Field(0.0)
    rpm_1:        float = Field(0.0)
    rpm_2:        float = Field(0.0)
    g_load:       float = Field(1.0)
    throttle:     float = Field(0.0)

    class Config:
        extra = "allow"


class ContactState(BaseModel):
    id:           str   = Field(...)
    name:         str   = Field("")
    type:         str   = Field("unknown")
    category:     str   = Field("Air")
    lat:          float = Field(0.0)
    lon:          float = Field(0.0)
    alt_msl_m:    float = Field(0.0)
    heading_deg:  float = Field(0.0)
    speed_ms:     float = Field(0.0)
    speed_kts:    float = Field(0.0)
    coalition:    int   = Field(0)   # 0=neutral 1=allies 2=enemies
    dist_m:       float = Field(0.0)
    received_at:  float = Field(default_factory=time.time)

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: dict) -> dict:
        coal = data.get("coalition", 0)
        if isinstance(coal, str):
            data["coalition"] = COALITION_MAP.get(coal.lower(), 0)
        if "id" in data and not isinstance(data["id"], str):
            data["id"] = str(data["id"])
        return data

    class Config:
        extra = "allow"


class ContactsPacket(BaseModel):
    timestamp: float
    count:     int
    contacts:  List[ContactState] = []
