"""Flugdatenquellen: eigener Empfänger (Primär) und OpenSky (Abdeckung), Spez. 4.

Einheiten werden hier umgerechnet, nicht später: intern gilt durchgängig SI,
Zeitstempel sind zeitzonenbewusstes UTC.
"""

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import NamedTuple

FEET = 0.3048
KNOTS = 1852 / 3600
FEET_PER_MINUTE = FEET / 60

LOG = logging.getLogger(__name__)


class Position(NamedTuple):
    icao24: str
    observed: datetime          # Abstrahlzeit
    latitude: float
    longitude: float
    geom_altitude_m: float | None   # ellipsoidisch (WGS84)
    baro_altitude_m: float | None   # Druckhöhe
    ground_speed_ms: float | None
    track_deg: float | None
    vertical_rate_ms: float | None
    callsign: str | None
    on_ground: bool
    source: str


def _get_json(url, timeout, headers=None, data=None):
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


# --- dump1090 --------------------------------------------------------------

def parse_dump1090(payload):
    """aircraft.json in Positionsmeldungen übersetzen."""
    now = payload.get("now") or time.time()
    out = []
    for entry in payload.get("aircraft", []):
        if entry.get("lat") is None or entry.get("lon") is None:
            continue
        baro = entry.get("alt_baro")
        on_ground = baro == "ground"
        vrate = entry.get("geom_rate")
        if vrate is None:
            vrate = entry.get("baro_rate")
        callsign = (entry.get("flight") or "").strip() or None
        out.append(
            Position(
                icao24=entry["hex"].strip().lower(),
                observed=datetime.fromtimestamp(now - entry.get("seen_pos", 0.0), UTC),
                latitude=entry["lat"],
                longitude=entry["lon"],
                geom_altitude_m=_scale(entry.get("alt_geom"), FEET),
                baro_altitude_m=None if on_ground else _scale(baro, FEET),
                ground_speed_ms=_scale(entry.get("gs"), KNOTS),
                track_deg=entry.get("track"),
                vertical_rate_ms=_scale(vrate, FEET_PER_MINUTE),
                callsign=callsign,
                on_ground=on_ground,
                source="adsb",
            )
        )
    return out


def read_dump1090(url, timeout):
    return parse_dump1090(_get_json(url, timeout))


def _scale(value, factor):
    return None if not isinstance(value, (int, float)) else value * factor


# --- OpenSky ---------------------------------------------------------------

class OpenSky:
    """Client für /states/all mit OAuth2 Client Credentials.

    HTTP Basic Auth ist von OpenSky abgekündigt (Spez. 4). Ausfälle sind
    normal und werden nicht als Fehler behandelt, solange die Primärquelle
    liefert – der Aufrufer bekommt eine leere Liste.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._token = None
        self._token_expiry = 0.0

    def _access_token(self):
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        data = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.cfg["client_id"],
                "client_secret": self.cfg["client_secret"],
            }
        ).encode()
        payload = _get_json(
            self.cfg["token_url"],
            self.cfg["timeout_seconds"],
            {"Content-Type": "application/x-www-form-urlencoded"},
            data,
        )
        self._token = payload["access_token"]
        # 60 s Sicherheitsabstand, damit kein Aufruf mit gerade abgelaufenem Token geht.
        self._token_expiry = time.monotonic() + payload.get("expires_in", 1800) - 60
        return self._token

    def states(self, box):
        """Positionsmeldungen in der Bounding Box; bei Störung eine leere Liste."""
        lamin, lamax, lomin, lomax = box
        query = urllib.parse.urlencode(
            {"lamin": lamin, "lamax": lamax, "lomin": lomin, "lomax": lomax}
        )
        try:
            payload = _get_json(
                f"{self.cfg['states_url']}?{query}",
                self.cfg["timeout_seconds"],
                {"Authorization": f"Bearer {self._access_token()}"},
            )
        except Exception as error:      # Netz, Kontingent, Wartung – alles gleich behandelt
            LOG.debug("OpenSky nicht erreichbar: %s", error)
            self._token = None
            raise
        return parse_states(payload)


def parse_states(payload):
    """OpenSky-Zustandsvektoren in Positionsmeldungen. Einheiten sind bereits SI."""
    out = []
    for state in payload.get("states") or []:
        icao24, callsign, _, time_position, last_contact, lon, lat = state[:7]
        if lat is None or lon is None:
            continue
        out.append(
            Position(
                icao24=icao24.strip().lower(),
                observed=datetime.fromtimestamp(time_position or last_contact, UTC),
                latitude=lat,
                longitude=lon,
                geom_altitude_m=state[13],
                baro_altitude_m=state[7],
                ground_speed_ms=state[9],
                track_deg=state[10],
                vertical_rate_ms=state[11],
                callsign=(callsign or "").strip() or None,
                on_ground=bool(state[8]),
                source="opensky",
            )
        )
    return out
